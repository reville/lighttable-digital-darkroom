# SPDX-License-Identifier: GPL-3.0-only
"""Bounded GPU stages for merging, with the CPU workflow as reference fallback.

Registration is deliberately unchanged. Only numeric image stages run here;
no catalog state, output files or processing-quality settings are changed.
"""
from __future__ import annotations

import logging
import os

import numpy as np

_LOG = logging.getLogger(__name__)
_MIN_PIXELS = 128 * 128
_TILE_PIXELS = 1_000_000
_disabled = False


def enabled(pixels: int) -> bool:
    return (not _disabled and pixels >= _MIN_PIXELS
            and os.environ.get("LIGHTTABLE_MERGE_ACCELERATION", "gpu") != "cpu")


def _compute(operation, image, parameters, shape):
    global _disabled
    try:
        from gpu_compute import compute
        return compute(operation, np.ascontiguousarray(image, dtype=np.float32),
                       parameters, tuple(shape))
    except Exception as error:
        # A missing worker, device loss or rejected device limit must preserve
        # the established result. Do not respawn a broken GPU for every tile.
        _disabled = True
        _LOG.warning("Merge GPU unavailable; using CPU: %s", error)
        return None


def hdr_fuse(stack: np.ndarray, valid: np.ndarray):
    frames, height, width, _ = stack.shape
    if not enabled(height * width):
        return None
    # Include the adjacent rows used by the contrast Laplacian. Bound packed
    # input to 32 MiB even at nine frames; never upload the full HDR stack.
    rows = max(1, min(height, 8_000_000 // (frames * width * 4) - 2))
    result = np.empty((height, width, 3), dtype=np.float32)
    for top in range(0, height, rows):
        bottom = min(height, top + rows)
        start, stop = max(0, top - 1), min(height, bottom + 1)
        packed = np.empty((frames, stop - start, width, 4), dtype=np.float32)
        packed[..., :3] = stack[:, start:stop]
        packed[..., 3] = valid[:, start:stop]
        tile = _compute("merge_hdr_fuse", packed,
                        [width, stop - start, frames], (stop - start, width, 3))
        if tile is None:
            return None
        result[top:bottom] = tile[top - start:bottom - start]
    return result


def shift(image: np.ndarray, displacement):
    height, width = image.shape[:2]
    if not enabled(height * width) or image.nbytes > 64_000_000:
        return None
    dy, dx = displacement
    inverse = [1, 0, -float(dx), 0, 1, -float(dy), 0, 0, 1]
    return _compute("merge_hdr_shift", image,
                    [width, height, width, height, *inverse], (height, width, 4))


def panorama_warp(image: np.ndarray, matrix: np.ndarray, shape):
    height, width = shape
    if not enabled(height * width):
        return None
    # Tile-major panoramas must not re-upload an entire camera frame for each
    # tile. Crop to the tile's projected source footprint plus bilinear halo.
    inverse = np.linalg.inv(matrix)
    corners = np.array([[0, 0, 1], [width - 1, 0, 1],
                        [0, height - 1, 1], [width - 1, height - 1, 1]])
    mapped = corners @ inverse.T
    source_h, source_w = image.shape[:2]
    # Linear projective denominators attain their extrema at the corners.
    # Also leave a float32 rounding margin: a finite float64 mapping close to
    # the horizon can round to zero when uploaded to the device.
    margin = max(1e-9, float(np.max(np.abs(corners) @ np.abs(inverse[2]))) * 1e-6)
    if np.all(mapped[:, 2] > margin) or np.all(mapped[:, 2] < -margin):
        xy = mapped[:, :2] / mapped[:, 2:3]
        x0, y0 = np.maximum(np.floor(xy.min(axis=0)).astype(int) - 1, 0)
        x1, y1 = np.minimum(np.ceil(xy.max(axis=0)).astype(int) + 2,
                            (source_w, source_h))
        if x1 <= x0 or y1 <= y0:
            return np.zeros((height, width, 4), dtype=np.float32)
    else:
        return None
    source = image[y0:y1, x0:x1]
    if source.nbytes > 64_000_000:
        return None
    inverse = np.array([[1, 0, -x0], [0, 1, -y0], [0, 0, 1]]) @ inverse
    # The feather remains relative to the original frame, not the GPU crop.
    return _compute("merge_panorama_warp", source,
                    [x1-x0, y1-y0, width, height, *inverse.ravel(),
                     source_w, source_h, x0, y0], (height, width, 4))


def sharpness(image: np.ndarray):
    height, width = image.shape[:2]
    if not enabled(height * width):
        return None
    rows = max(1, _TILE_PIXELS // width)
    result = np.empty((height, width), dtype=np.float32)
    # Gaussian1 radius4 + Laplacian radius1 + Gaussian2.5 radius10.
    halo = 15
    for top in range(0, height, rows):
        bottom = min(height, top + rows)
        start, stop = max(0, top - halo), min(height, bottom + halo)
        tile = _compute("merge_focus_sharpness", image[start:stop],
                        [width, stop-start], (stop-start, width))
        if tile is None:
            return None
        result[top:bottom] = tile[top-start:bottom-start]
    return result


def pyramid_down(value: np.ndarray):
    height, width = value.shape[:2]
    if not enabled(height * width):
        return None
    channels = value.shape[2] if value.ndim == 3 else 1
    rows = max(2, (_TILE_PIXELS // width) // 2 * 2)
    result = np.empty(((height+1)//2, (width+1)//2, *value.shape[2:]), dtype=np.float32)
    for top in range(0, height, rows):
        bottom = min(height, top + rows)
        start, stop = max(0, top-4), min(height, bottom+4)
        shape = ((stop-start+1)//2, (width+1)//2, *value.shape[2:])
        tile = _compute("merge_pyramid_down", value[start:stop],
                        [width, stop-start, channels], shape)
        if tile is None:
            return None
        result[top//2:(bottom+1)//2] = tile[(top-start)//2:(bottom-start+1)//2]
    return result


def resize(value: np.ndarray, shape):
    height, width = value.shape[:2]
    if not enabled(height * width):
        return None
    channels = value.shape[2] if value.ndim == 3 else 1
    out_h, out_w = shape[:2]
    rows = max(1, _TILE_PIXELS // out_w)
    result = np.empty(shape, dtype=np.float32)
    scale = height / out_h
    for top in range(0, out_h, rows):
        bottom = min(out_h, top + rows)
        first = (top + 0.5) * scale - 0.5
        last = (bottom - 0.5) * scale - 0.5
        start = max(0, int(np.floor(first)) - 1)
        stop = min(height, int(np.ceil(last)) + 2)
        tile = _compute("merge_resize", value[start:stop],
                        [width, stop-start, channels, out_w, bottom-top,
                         scale, first-start], (bottom-top, out_w, *value.shape[2:]))
        if tile is None:
            return None
        result[top:bottom] = tile
    return result
