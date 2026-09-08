"""Local HDR, panorama, and focus-stack merging."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from scipy import ndimage
from scipy.spatial.distance import cdist
from skimage import color, feature, transform
from skimage.measure import ransac
from skimage.registration import phase_cross_correlation


ALIGNMENT_MAX_EDGE = 1200
MAX_ALIGNMENT_FEATURES = 4096
DESCRIPTOR_MATCH_BLOCK = 256
PANORAMA_TILE_EDGE = 1024
Progress = Callable[[str, int, int], None]
ImageInput = np.ndarray | Callable[[], np.ndarray]


@dataclass(frozen=True)
class _FeatureSet:
    gray: np.ndarray
    scale: float
    original_shape: tuple[int, int]
    keypoints: np.ndarray | None
    descriptors: np.ndarray | None


def _float_rgb(image) -> np.ndarray:
    value = np.asarray(image)
    if value.ndim != 3 or value.shape[2] < 3:
        raise ValueError("merge inputs must be RGB images")
    value = value[..., :3]
    if value.dtype == np.float32 and value.flags.c_contiguous:
        minimum, maximum = float(np.min(value)), float(np.max(value))
        if np.isfinite(minimum) and np.isfinite(maximum) \
                and minimum >= 0.0 and maximum <= 1.0:
            return value
    return np.clip(np.asarray(value, dtype=np.float32), 0.0, 1.0)


def _notify(progress: Progress | None, phase: str, done: int, total: int) -> None:
    if progress is not None:
        progress(phase, int(done), int(total))


def _load_input(value: ImageInput) -> np.ndarray:
    return _float_rgb(value() if callable(value) else value)


def _srgb_to_linear(value):
    return np.where(value <= 0.04045, value / 12.92,
                    ((value + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(value):
    value = np.clip(value, 0.0, None)
    return np.where(value <= 0.0031308, value * 12.92,
                    1.055 * value ** (1.0 / 2.4) - 0.055)


def _gray(image):
    return color.rgb2gray(np.clip(image, 0.0, 1.0)).astype(np.float32)


def _alignment_gray(image):
    """Exposure-insensitive edge image for bracket registration."""
    return ndimage.gaussian_gradient_magnitude(_gray(image), 1.0).astype(np.float32)


def _align_exposures(values: list[ImageInput], *,
                     progress: Progress | None = None) -> tuple[np.ndarray, np.ndarray]:
    reference_index = len(values) // 2
    reference = _load_input(values[reference_index])
    height, width = reference.shape[:2]
    scale = min(1.0, 1400.0 / max(height, width))
    ref_small = transform.resize(_alignment_gray(reference),
                                 (max(8, round(height * scale)),
                                  max(8, round(width * scale))),
                                 anti_aliasing=True, preserve_range=True)
    aligned = np.empty((len(values), height, width, 3), dtype=np.float32)
    valid = np.empty((len(values), height, width), dtype=np.float32)
    for index, value in enumerate(values):
        image = reference if index == reference_index else _load_input(value)
        if image.shape[:2] != (height, width):
            raise ValueError("HDR inputs must have matching dimensions")
        small = transform.resize(_alignment_gray(image), ref_small.shape,
                                 anti_aliasing=True, preserve_range=True)
        if index == reference_index or not np.any(ref_small) or not np.any(small):
            # The reference is already aligned. A featureless exposure has no
            # translation evidence; phase correlation otherwise invents a
            # subpixel shift and creates black borders even in identical frames.
            shift = np.zeros(2, dtype=np.float64)
        else:
            shift, _, _ = phase_cross_correlation(ref_small, small,
                                                  upsample_factor=10)
        full_shift = np.asarray(shift) / scale
        if np.any(np.abs(full_shift) > np.asarray((height, width)) * 0.2):
            # A featureless bracket can make phase correlation wrap to a false
            # distant peak. Same-sized frames are still safely mergeable as a
            # tripod sequence; deghosting suppresses local motion.
            full_shift = np.zeros(2, dtype=np.float64)
        moved = ndimage.shift(image, (*full_shift, 0), order=1,
                              mode="constant", cval=0.0, prefilter=False)
        mask = ndimage.shift(np.ones((height, width), dtype=np.float32),
                             full_shift, order=0, mode="constant", cval=0.0,
                             prefilter=False)
        aligned[index] = moved
        valid[index] = mask
        _notify(progress, "aligning", index + 1, len(values))
    return aligned, valid


def hdr_merge(values: list[ImageInput], *, progress: Progress | None = None) -> np.ndarray:
    """Align and exposure-fuse 2-9 bracketed, display-referred frames."""
    if not 2 <= len(values) <= 9:
        raise ValueError("HDR merge needs 2 to 9 photos")
    stack, valid = _align_exposures(values, progress=progress)
    median = np.median(stack, axis=0).astype(np.float32)
    numerator = np.zeros_like(stack[0])
    denominator = np.zeros(stack.shape[1:3], dtype=np.float32)
    for index, (image, mask) in enumerate(zip(stack, valid)):
        luminance = _gray(image)
        contrast = np.abs(ndimage.laplace(luminance))
        saturation = np.std(image, axis=2)
        well_exposed = np.exp(-np.sum((image - 0.5) ** 2, axis=2) /
                              (2.0 * 0.24 ** 2))
        motion = np.mean(np.abs(image - median), axis=2)
        deghost = np.exp(-(motion / 0.18) ** 2)
        weight = ((1e-5 + well_exposed + contrast * 0.35 +
                   saturation * 0.15) * deghost * mask).astype(np.float32)
        numerator += _srgb_to_linear(image) * weight[..., None]
        denominator += weight
        _notify(progress, "fusing", index + 1, len(values))
    fused = numerator / np.maximum(denominator[..., None], 1e-6)
    return np.clip(_linear_to_srgb(fused), 0.0, 1.0).astype(np.float32)


def _feature_image(image: np.ndarray,
                   maximum=ALIGNMENT_MAX_EDGE) -> tuple[np.ndarray, float]:
    gray = _gray(image)
    scale = min(1.0, maximum / max(gray.shape))
    if scale < 1.0:
        gray = transform.resize(gray, (round(gray.shape[0] * scale),
                                       round(gray.shape[1] * scale)),
                                anti_aliasing=True, preserve_range=True)
    return gray.astype(np.float32), scale


def _bounded_feature_indices(keypoints: np.ndarray, sigmas: np.ndarray,
                             shape: tuple[int, int], maximum: int) -> np.ndarray:
    """Keep a deterministic, spatially distributed feature budget."""
    count = len(keypoints)
    if count <= maximum:
        return np.arange(count, dtype=np.intp)
    grid = max(1, int(np.ceil(np.sqrt(maximum))))
    height, width = shape
    rows = np.clip((keypoints[:, 0] * grid / max(height, 1)).astype(int),
                   0, grid - 1)
    columns = np.clip((keypoints[:, 1] * grid / max(width, 1)).astype(int),
                      0, grid - 1)
    buckets: dict[int, list[int]] = {}
    for index, cell in enumerate(rows * grid + columns):
        buckets.setdefault(int(cell), []).append(index)
    ordered = []
    for cell in sorted(buckets):
        ordered.append(sorted(buckets[cell], key=lambda index: (-sigmas[index], index)))
    selected: list[int] = []
    depth = 0
    while len(selected) < maximum:
        added = False
        for bucket in ordered:
            if depth < len(bucket):
                selected.append(bucket[depth])
                added = True
                if len(selected) == maximum:
                    break
        if not added:
            break
        depth += 1
    return np.asarray(selected, dtype=np.intp)


def _extract_features(image: np.ndarray) -> _FeatureSet:
    gray, scale = _feature_image(image)
    detector = feature.SIFT(upsampling=1)
    try:
        detector.detect_and_extract(gray)
    except RuntimeError:
        return _FeatureSet(gray, scale, image.shape[:2], None, None)
    indices = _bounded_feature_indices(
        detector.keypoints, detector.sigmas, gray.shape,
        MAX_ALIGNMENT_FEATURES)
    return _FeatureSet(
        gray, scale, image.shape[:2], detector.keypoints[indices],
        detector.descriptors[indices])


def _match_descriptors_bounded(descriptors1: np.ndarray,
                               descriptors2: np.ndarray, *,
                               max_ratio: float = 0.78,
                               block_size: int = DESCRIPTOR_MATCH_BLOCK) -> np.ndarray:
    """Ratio- and cross-checked matching without an all-pairs allocation."""
    count1, count2 = len(descriptors1), len(descriptors2)
    if count1 == 0 or count2 < 2:
        return np.empty((0, 2), dtype=np.intp)
    nearest = np.empty(count1, dtype=np.intp)
    nearest_distance = np.empty(count1, dtype=np.float64)
    second_distance = np.empty(count1, dtype=np.float64)
    reverse_nearest = np.full(count2, -1, dtype=np.intp)
    reverse_distance = np.full(count2, np.inf, dtype=np.float64)
    for start in range(0, count1, max(1, int(block_size))):
        stop = min(count1, start + max(1, int(block_size)))
        distances = cdist(descriptors1[start:stop], descriptors2,
                          metric="euclidean")
        candidates = np.argpartition(distances, 1, axis=1)[:, :2]
        candidate_distances = np.take_along_axis(distances, candidates, axis=1)
        order = np.argsort(candidate_distances, axis=1)
        candidates = np.take_along_axis(candidates, order, axis=1)
        candidate_distances = np.take_along_axis(
            candidate_distances, order, axis=1)
        nearest[start:stop] = candidates[:, 0]
        nearest_distance[start:stop] = candidate_distances[:, 0]
        second_distance[start:stop] = candidate_distances[:, 1]
        local_rows = np.argmin(distances, axis=0)
        local_distance = distances[local_rows, np.arange(count2)]
        improved = local_distance < reverse_distance
        reverse_distance[improved] = local_distance[improved]
        reverse_nearest[improved] = start + local_rows[improved]
    safe_second = np.maximum(second_distance, np.finfo(np.float64).eps)
    indices1 = np.arange(count1, dtype=np.intp)
    accepted = ((nearest_distance / safe_second) < max_ratio)
    accepted &= reverse_nearest[nearest] == indices1
    return np.column_stack((indices1[accepted], nearest[accepted]))


def _focus_scale_transform_gray(previous_gray: np.ndarray,
                                current_gray: np.ndarray) -> np.ndarray:
    """Estimate the small, centred magnification change from focus breathing.

    Focus rails can move the sharp plane far enough that local feature matches
    come from different bands of the image.  A heavily blurred comparison
    retains scene geometry while suppressing those focus-dependent edges.  A
    lower-quartile residual further ignores the part of the frame whose focus
    actually changed.
    """
    scale = min(1.0, 256.0 / max(previous_gray.shape))
    if scale < 1.0:
        shape = (round(previous_gray.shape[0] * scale),
                 round(previous_gray.shape[1] * scale))
        previous_small = transform.resize(
            previous_gray, shape, anti_aliasing=True, preserve_range=True)
        current_small = transform.resize(
            current_gray, shape, anti_aliasing=True, preserve_range=True)
    else:
        previous_small, current_small = previous_gray, current_gray
    if (float(previous_small.std()) < 1e-8
            and float(current_small.std()) < 1e-8):
        return np.eye(3, dtype=np.float64)
    height, width = previous_small.shape
    sigma = max(2.0, max(height, width) / 28.0)
    reference = ndimage.gaussian_filter(previous_small, sigma)
    margin = max(4, min(height, width) // 8)
    crop = (slice(margin, height - margin),
            slice(margin, width - margin))
    center_x, center_y = (width - 1) / 2.0, (height - 1) / 2.0
    best_score, best_scale = np.inf, 1.0

    def search(scales) -> None:
        nonlocal best_score, best_scale
        for magnification in scales:
            matrix = np.array([
                [magnification, 0.0, center_x * (1.0 - magnification)],
                [0.0, magnification, center_y * (1.0 - magnification)],
                [0.0, 0.0, 1.0],
            ])
            warped = transform.warp(
                current_small,
                inverse_map=transform.ProjectiveTransform(matrix).inverse,
                output_shape=(height, width), order=1, preserve_range=True)
            residual = (reference - ndimage.gaussian_filter(warped, sigma))[crop]
            score = float(np.quantile(residual * residual, 0.25))
            if (score < best_score - 1e-12
                    or (abs(score - best_score) <= 1e-12
                        and abs(magnification - 1.0)
                        < abs(best_scale - 1.0))):
                best_score, best_scale = score, float(magnification)

    # A coarse-to-fine search retains sub-0.1% scale resolution while avoiding
    # 61 full warps for every low-detail adjacent pair.
    search(np.linspace(0.97, 1.03, 13))
    search(np.linspace(max(0.97, best_scale - 0.003),
                       min(1.03, best_scale + 0.003), 13))
    small_scale = np.array([
        [best_scale, 0.0, center_x * (1.0 - best_scale)],
        [0.0, best_scale, center_y * (1.0 - best_scale)],
        [0.0, 0.0, 1.0],
    ])
    scaled = transform.warp(
        current_small,
        inverse_map=transform.ProjectiveTransform(small_scale).inverse,
        output_shape=(height, width), order=1, preserve_range=True)
    scaled = ndimage.gaussian_filter(scaled, sigma)
    shift, _, _ = phase_cross_correlation(
        reference, scaled, upsample_factor=10, normalization=None)
    accepted_shift = np.zeros(2, dtype=np.float64)
    if (np.all(np.isfinite(shift))
            and np.all(np.abs(shift) <= np.asarray((height, width)) * 0.2)):
        shifted = ndimage.shift(scaled, shift, order=1, mode="nearest",
                                prefilter=False)
        residual = (reference - shifted)[crop]
        shifted_score = float(np.quantile(residual * residual, 0.25))
        if shifted_score < best_score * 0.9:
            accepted_shift = np.asarray(shift, dtype=np.float64)
    full_height, full_width = previous_gray.shape
    center_x, center_y = (full_width - 1) / 2.0, (full_height - 1) / 2.0
    full_scale = np.array([
        [best_scale, 0.0, center_x * (1.0 - best_scale)],
        [0.0, best_scale, center_y * (1.0 - best_scale)],
        [0.0, 0.0, 1.0],
    ], dtype=np.float64)
    full_shift = np.array([
        [1.0, 0.0, accepted_shift[1] / scale],
        [0.0, 1.0, accepted_shift[0] / scale],
        [0.0, 0.0, 1.0],
    ], dtype=np.float64)
    return full_shift @ full_scale


def _focus_scale_transform(previous: np.ndarray,
                           current: np.ndarray) -> np.ndarray:
    return _focus_scale_transform_gray(_gray(previous), _gray(current))


def _pair_transform_from_features(previous: np.ndarray | None,
                                  current: np.ndarray | None,
                                  previous_features: _FeatureSet,
                                  current_features: _FeatureSet, *,
                                  focus_sequence: bool = False,
                                  ) -> tuple[np.ndarray, int]:
    try:
        if (previous_features.descriptors is None
                or current_features.descriptors is None):
            raise ValueError("not enough shared detail")
        matches = _match_descriptors_bounded(
            current_features.descriptors, previous_features.descriptors)
        if len(matches) < 6:
            raise ValueError("not enough shared detail")
        source = current_features.keypoints[matches[:, 0]][:, ::-1]
        destination = previous_features.keypoints[matches[:, 1]][:, ::-1]
        model, inliers = ransac((source, destination),
                                transform.ProjectiveTransform,
                                min_samples=4, residual_threshold=2.5,
                                max_trials=1200)
        if model is None or int(np.sum(inliers)) < 6:
            raise ValueError("not enough consistent feature matches")
        to_small = np.diag([current_features.scale,
                            current_features.scale, 1.0])
        from_small = np.diag([1.0 / previous_features.scale,
                              1.0 / previous_features.scale, 1.0])
        matrix = from_small @ model.params @ to_small
        if focus_sequence:
            # Repeating sharp bands can produce a high-inlier transform one
            # whole band away. A focus rail can breathe slightly, but it does
            # not jump a quarter-frame between adjacent captures.
            scale_x = float(np.linalg.norm(matrix[:2, 0]))
            scale_y = float(np.linalg.norm(matrix[:2, 1]))
            height, width = previous_features.original_shape
            if (not 0.85 <= scale_x <= 1.15
                    or not 0.85 <= scale_y <= 1.15
                    or abs(float(matrix[0, 2])) > width * 0.2
                    or abs(float(matrix[1, 2])) > height * 0.2
                    or np.max(np.abs(matrix[2, :2])) > 0.001):
                raise ValueError("implausible transform for adjacent focus frames")
        return matrix, int(np.sum(inliers))
    except (RuntimeError, ValueError):
        # Translation-only fallback handles low-detail, tripod panoramas.
        if previous_features.original_shape != current_features.original_shape:
            raise ValueError("panorama frames need more shared visual detail")
        if focus_sequence:
            if previous is not None and current is not None:
                return _focus_scale_transform(previous, current), 0
            small_matrix = _focus_scale_transform_gray(
                previous_features.gray, current_features.gray)
            to_small = np.diag([current_features.scale,
                                current_features.scale, 1.0])
            from_small = np.diag([1.0 / previous_features.scale,
                                  1.0 / previous_features.scale, 1.0])
            return from_small @ small_matrix @ to_small, 0
        previous_gray = previous_features.gray
        current_gray = current_features.gray
        if float(previous_gray.std()) < 1e-8 and float(current_gray.std()) < 1e-8:
            shift = np.zeros(2, dtype=np.float64)
        else:
            shift, _, _ = phase_cross_correlation(
                previous_gray, current_gray, upsample_factor=5)
            if not np.all(np.isfinite(shift)):
                shift = np.zeros(2, dtype=np.float64)
        full_shift = np.asarray(shift) / previous_features.scale
        limit = 0.8
        if np.any(np.abs(full_shift)
                  > np.asarray(previous_features.original_shape) * limit):
            raise ValueError("panorama frames do not overlap enough")
        return (np.array([[1.0, 0.0, full_shift[1]],
                          [0.0, 1.0, full_shift[0]],
                          [0.0, 0.0, 1.0]], dtype=np.float64), 0)


def _pair_transform_details(previous: np.ndarray,
                            current: np.ndarray, *,
                            focus_sequence: bool = False) -> tuple[np.ndarray, int]:
    return _pair_transform_from_features(
        previous, current, _extract_features(previous),
        _extract_features(current), focus_sequence=focus_sequence)


def _pair_transform(previous: np.ndarray, current: np.ndarray) -> np.ndarray:
    return _pair_transform_details(previous, current)[0]


def _source_feather(shape: tuple[int, int]) -> np.ndarray:
    height, width = shape
    rows = np.minimum(np.arange(height) + 1,
                      np.arange(height, 0, -1))[:, None]
    columns = np.minimum(np.arange(width) + 1,
                         np.arange(width, 0, -1))[None, :]
    return np.minimum(rows, columns).astype(np.float32)


def panorama_merge(values: list[ImageInput], *, progress: Progress | None = None,
                   tile_edge: int = PANORAMA_TILE_EDGE) -> np.ndarray:
    """Feature-align and feather 2-20 overlapping frames into one panorama."""
    if not 2 <= len(values) <= 20:
        raise ValueError("panorama merge needs 2 to 20 photos")
    inputs = list(values)
    lazy = any(callable(value) for value in inputs)
    images: list[np.ndarray] | None = [] if not lazy else None
    feature_sets = []
    alignment_total = len(inputs) * 2 - 1
    for index, value in enumerate(inputs):
        image = _load_input(value)
        feature_sets.append(_extract_features(image))
        if images is not None:
            images.append(image)
        _notify(progress, "aligning", index + 1, alignment_total)
    matrices = [np.eye(3)]
    for index in range(len(inputs) - 1):
        matrix, _ = _pair_transform_from_features(
            None, None, feature_sets[index], feature_sets[index + 1])
        matrices.append(matrices[-1] @ matrix)
        _notify(progress, "aligning", len(inputs) + index + 1,
                alignment_total)

    projected = []
    for features, matrix in zip(feature_sets, matrices):
        height, width = features.original_shape
        corners = np.array([[0, 0], [width, 0], [width, height], [0, height]])
        projected.append(transform.ProjectiveTransform(matrix)(corners))
    all_corners = np.concatenate(projected)
    minimum = np.floor(all_corners.min(axis=0))
    maximum = np.ceil(all_corners.max(axis=0))
    output_width, output_height = (maximum - minimum).astype(int)
    if output_width < 2 or output_height < 2 or output_width * output_height > 120_000_000:
        raise ValueError("panorama canvas is outside the supported size")
    offset = np.array([[1.0, 0.0, -minimum[0]],
                       [0.0, 1.0, -minimum[1]],
                       [0.0, 0.0, 1.0]])
    tile_edge = max(128, int(tile_edge))
    tile_columns = (output_width + tile_edge - 1) // tile_edge
    tile_rows = (output_height + tile_edge - 1) // tile_edge
    tile_total = tile_columns * tile_rows
    occupied_rows = np.zeros(output_height, dtype=bool)
    occupied_columns = np.zeros(output_width, dtype=bool)
    if images is None:
        # The server supplies reloadable inputs. Keep only one full source in
        # memory and bound every warped intermediate to one tile.
        accumulated = np.zeros(
            (output_height, output_width, 3), dtype=np.float32)
        total_weight = np.zeros(
            (output_height, output_width), dtype=np.float32)
        blend_step = 0
        for value, matrix in zip(inputs, matrices):
            image = _load_input(value)
            feather = _source_feather(image.shape[:2])
            for top in range(0, output_height, tile_edge):
                bottom = min(output_height, top + tile_edge)
                for left in range(0, output_width, tile_edge):
                    right = min(output_width, left + tile_edge)
                    tile_shape = (bottom - top, right - left)
                    tile_offset = np.array([[1.0, 0.0, -left],
                                            [0.0, 1.0, -top],
                                            [0.0, 0.0, 1.0]])
                    mapping = transform.ProjectiveTransform(
                        tile_offset @ offset @ matrix)
                    warped = transform.warp(
                        image, inverse_map=mapping.inverse,
                        output_shape=tile_shape, order=1,
                        preserve_range=True).astype(np.float32)
                    weight = transform.warp(
                        feather, inverse_map=mapping.inverse,
                        output_shape=tile_shape, order=1,
                        preserve_range=True).astype(np.float32)
                    accumulated[top:bottom, left:right] += (
                        warped * weight[..., None])
                    total_weight[top:bottom, left:right] += weight
                    occupied = weight > 1e-6
                    occupied_rows[top:bottom] |= np.any(occupied, axis=1)
                    occupied_columns[left:right] |= np.any(occupied, axis=0)
                    blend_step += 1
                    _notify(progress, "blending", blend_step,
                            tile_total * len(inputs))
        np.maximum(total_weight, 1e-6, out=total_weight)
        accumulated /= total_weight[..., None]
        result = accumulated
    else:
        # Array callers already own their sources. Tile-major traversal avoids
        # full-canvas warped RGB, mask, and distance-transform allocations.
        result = np.empty(
            (output_height, output_width, 3), dtype=np.float32)
        feathers: dict[tuple[int, int], np.ndarray] = {}
        tile_index = 0
        for top in range(0, output_height, tile_edge):
            bottom = min(output_height, top + tile_edge)
            for left in range(0, output_width, tile_edge):
                right = min(output_width, left + tile_edge)
                tile_shape = (bottom - top, right - left)
                accumulated = np.zeros((*tile_shape, 3), dtype=np.float32)
                total_weight = np.zeros(tile_shape, dtype=np.float32)
                tile_offset = np.array([[1.0, 0.0, -left],
                                        [0.0, 1.0, -top],
                                        [0.0, 0.0, 1.0]])
                for image, matrix in zip(images, matrices):
                    mapping = transform.ProjectiveTransform(
                        tile_offset @ offset @ matrix)
                    warped = transform.warp(
                        image, inverse_map=mapping.inverse,
                        output_shape=tile_shape, order=1,
                        preserve_range=True).astype(np.float32)
                    feather = feathers.get(image.shape[:2])
                    if feather is None:
                        feather = _source_feather(image.shape[:2])
                        feathers[image.shape[:2]] = feather
                    weight = transform.warp(
                        feather, inverse_map=mapping.inverse,
                        output_shape=tile_shape, order=1,
                        preserve_range=True).astype(np.float32)
                    accumulated += warped * weight[..., None]
                    total_weight += weight
                result[top:bottom, left:right] = accumulated / np.maximum(
                    total_weight[..., None], 1e-6)
                occupied = total_weight > 1e-6
                occupied_rows[top:bottom] |= np.any(occupied, axis=1)
                occupied_columns[left:right] |= np.any(occupied, axis=0)
                tile_index += 1
                _notify(progress, "blending", tile_index, tile_total)
    if np.any(occupied_rows) and np.any(occupied_columns):
        rows = np.flatnonzero(occupied_rows)
        columns = np.flatnonzero(occupied_columns)
        result = result[rows[0]:rows[-1] + 1, columns[0]:columns[-1] + 1]
    np.clip(result, 0.0, 1.0, out=result)
    return result.astype(np.float32, copy=False)


def _focus_alignment(values: list[ImageInput],
                     feature_sets: list[_FeatureSet],
                     images: list[np.ndarray] | None,
                     alignment=None, *,
                     progress: Progress | None = None) -> tuple[list[np.ndarray], tuple]:
    """Map a focus rail sequence into its middle frame and common crop."""
    height, width = feature_sets[len(feature_sets) // 2].original_shape
    if any(item.original_shape != (height, width) for item in feature_sets):
        raise ValueError("focus-stack inputs must have matching dimensions")
    alignment_total = len(values) * 2 - 1
    pairwise = []
    minimum_inliers = None
    for index in range(len(values) - 1):
        previous = images[index] if images is not None else None
        current = images[index + 1] if images is not None else None
        matrix, inliers = _pair_transform_from_features(
            previous, current, feature_sets[index], feature_sets[index + 1],
            focus_sequence=True)
        pairwise.append(matrix)
        minimum_inliers = (inliers if minimum_inliers is None
                           else min(minimum_inliers, inliers))
        if alignment is not None:
            alignment(int(minimum_inliers))
        _notify(progress, "aligning", len(values) + index + 1,
                alignment_total)
    to_first = [np.eye(3)]
    for matrix in pairwise:
        to_first.append(to_first[-1] @ matrix)
    reference = len(values) // 2
    from_first = np.linalg.inv(to_first[reference])
    matrices = [from_first @ matrix for matrix in to_first]
    common = np.ones((height, width), dtype=bool)
    for item, matrix in zip(feature_sets, matrices):
        mapping = transform.ProjectiveTransform(matrix)
        valid = transform.warp(
            np.ones(item.original_shape, dtype=np.float32),
            inverse_map=mapping.inverse, output_shape=(height, width),
            order=0, preserve_range=True) > 0.5
        common &= valid
    if not np.any(common):
        raise ValueError("focus-stack frames have no common aligned area")
    rows, columns = np.where(common)
    crop = (slice(rows.min(), rows.max() + 1),
            slice(columns.min(), columns.max() + 1))
    return matrices, crop


def _warp_focus(image: np.ndarray, matrix: np.ndarray,
                shape: tuple[int, int], crop: tuple) -> np.ndarray:
    mapping = transform.ProjectiveTransform(matrix)
    return transform.warp(
        image, inverse_map=mapping.inverse, output_shape=shape,
        order=3, preserve_range=True)[crop].astype(np.float32)


def _sharpness(image: np.ndarray) -> np.ndarray:
    gray = ndimage.gaussian_filter(_gray(image), 1.0)
    return ndimage.gaussian_filter(np.abs(ndimage.laplace(gray)), 2.5).astype(
        np.float32)


def _pyramid(value: np.ndarray, levels: int, *, laplacian: bool) -> list[np.ndarray]:
    gaussian = [value.astype(np.float32)]
    for _ in range(levels - 1):
        current = gaussian[-1]
        blurred = ndimage.gaussian_filter(
            current, (1.0, 1.0, 0) if current.ndim == 3 else 1.0)
        gaussian.append(blurred[::2, ::2].astype(np.float32))
    if not laplacian:
        return gaussian
    result = []
    for current, smaller in zip(gaussian, gaussian[1:]):
        expanded = transform.resize(
            smaller, current.shape, order=1, preserve_range=True,
            anti_aliasing=False).astype(np.float32)
        result.append(current - expanded)
    result.append(gaussian[-1])
    return result


def _reconstruct_pyramid(levels: list[np.ndarray]) -> np.ndarray:
    result = levels[-1]
    for level in reversed(levels[:-1]):
        result = transform.resize(
            result, level.shape, order=1, preserve_range=True,
            anti_aliasing=False).astype(np.float32) + level
    return result


def focus_merge(values: list[ImageInput], *, alignment=None,
                progress: Progress | None = None) -> np.ndarray:
    """Align and sharpness-fuse 2-60 focus-bracketed frames.

    Alignment and focus scores are recomputed for the blend pass. Reloadable
    inputs are streamed one at a time, and only running pyramid sums are
    retained, so resident input memory does not grow with the number of frames.
    """
    if not 2 <= len(values) <= 60:
        raise ValueError("focus merge needs 2 to 60 photos")
    inputs = list(values)
    lazy = any(callable(value) for value in inputs)
    images: list[np.ndarray] | None = [] if not lazy else None
    feature_sets = []
    alignment_total = len(inputs) * 2 - 1
    for index, value in enumerate(inputs):
        image = _load_input(value)
        feature_sets.append(_extract_features(image))
        if images is not None:
            images.append(image)
        _notify(progress, "aligning", index + 1, alignment_total)
    shape = feature_sets[len(feature_sets) // 2].original_shape
    matrices, crop = _focus_alignment(
        inputs, feature_sets, images, alignment, progress=progress)
    maximum = None
    for index, matrix in enumerate(matrices):
        image = images[index] if images is not None else _load_input(inputs[index])
        score = _sharpness(_warp_focus(image, matrix, shape, crop))
        maximum = score if maximum is None else np.maximum(maximum, score)
        _notify(progress, "analyzing", index + 1, len(inputs))
    temperature = max(float(np.mean(maximum)) * 0.15, 1e-6)
    numerator = denominator = None
    for frame_index, matrix in enumerate(matrices):
        image = (images[frame_index] if images is not None
                 else _load_input(inputs[frame_index]))
        aligned = _warp_focus(image, matrix, shape, crop)
        score = _sharpness(aligned)
        weight = np.exp(np.clip((score - maximum) / temperature, -40.0, 0.0))
        image_levels = _pyramid(_srgb_to_linear(aligned), 5, laplacian=True)
        weight_levels = _pyramid(weight, 5, laplacian=False)
        if numerator is None:
            numerator = [np.zeros_like(level) for level in image_levels]
            denominator = [np.zeros((*level.shape[:2], 1), dtype=np.float32)
                           for level in image_levels]
        for index, (level, level_weight) in enumerate(
                zip(image_levels, weight_levels)):
            expanded_weight = level_weight[..., None]
            numerator[index] += level * expanded_weight
            denominator[index] += expanded_weight
        _notify(progress, "blending", frame_index + 1, len(inputs))
    blended = [value / np.maximum(weight, 1e-6)
               for value, weight in zip(numerator, denominator)]
    return np.clip(_linear_to_srgb(_reconstruct_pyramid(blended)),
                   0.0, 1.0).astype(np.float32)


def safe_output_name(value: str, mode: str) -> str:
    prefix = {"hdr": "HDR", "panorama": "Panorama", "focus": "Focus"}.get(
        mode, "Merge")
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", str(value or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")[:100]
    return f"{cleaned or prefix}.tif"


def collision_path(path: Path) -> Path:
    def occupied(candidate: Path) -> bool:
        return candidate.exists() or Path(
            str(candidate) + ".lighttable.json").exists()

    if not occupied(path):
        return path
    for index in range(2, 10000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not occupied(candidate):
            return candidate
    raise ValueError("could not choose a free merge filename")
