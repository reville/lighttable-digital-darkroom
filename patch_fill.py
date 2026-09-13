# SPDX-License-Identifier: GPL-3.0-only
"""Exemplar-based hole filling for the Remove tool.

A coarse-to-fine PatchMatch search finds, for every patch that overlaps the
hole, a similar patch made only of known pixels. Each hole pixel is then
rebuilt from the patches that cover it. Every value is copied from the same
photograph; nothing is generated. The random search is seeded, so a saved
correction renders the same pixels each time at a given resolution.
"""
from __future__ import annotations

import numpy as np

PATCH = 7
_RADIUS = PATCH // 2
# Bound the temporary (targets x patch x channels) gathers.
_CHUNK = 24_000
_EPSILON = 1e-6


def _downsample(image: np.ndarray) -> np.ndarray:
    height, width = image.shape[:2]
    half_h, half_w = max(1, height // 2), max(1, width // 2)
    cropped = image[:half_h * 2, :half_w * 2]
    return cropped.reshape(half_h, 2, half_w, 2, -1).mean(axis=(1, 3)).astype(np.float32)


def _downsample_mask(mask: np.ndarray) -> np.ndarray:
    height, width = mask.shape
    half_h, half_w = max(1, height // 2), max(1, width // 2)
    return mask[:half_h * 2, :half_w * 2].reshape(half_h, 2, half_w, 2).any(axis=(1, 3))


def _upsample(coarse: np.ndarray, shape: tuple[int, ...]) -> np.ndarray:
    height, width = shape[:2]
    upsampled = np.repeat(np.repeat(coarse, 2, axis=0), 2, axis=1)
    pad_h = max(0, height - upsampled.shape[0])
    pad_w = max(0, width - upsampled.shape[1])
    if pad_h or pad_w:
        widths = [(0, pad_h), (0, pad_w)] + [(0, 0)] * (upsampled.ndim - 2)
        upsampled = np.pad(upsampled, widths, mode="edge")
    return upsampled[:height, :width]


def _nearest_fill(image: np.ndarray, hole: np.ndarray) -> np.ndarray:
    from scipy import ndimage

    _, (rows, columns) = ndimage.distance_transform_edt(hole, return_indices=True)
    filled = image.copy()
    filled[hole] = image[rows[hole], columns[hole]]
    return filled


def _chunks(count: int):
    for start in range(0, count, _CHUNK):
        yield slice(start, min(count, start + _CHUNK))


class _Level:
    """One pyramid level: the targets, the legal sources, and their patches."""

    def __init__(self, estimate: np.ndarray, hole: np.ndarray):
        from scipy import ndimage

        self.hole = hole
        self.height, self.width = hole.shape
        footprint = np.ones((PATCH, PATCH), dtype=bool)
        band = ndimage.binary_dilation(hole, structure=footprint)
        self.target_y, self.target_x = (axis.astype(np.int64) for axis in np.nonzero(band))
        index = np.full(hole.shape, -1, dtype=np.int64)
        index[self.target_y, self.target_x] = np.arange(self.target_y.size)
        self.index = index
        valid = ~band
        valid[:_RADIUS, :] = False
        valid[max(0, self.height - _RADIUS):, :] = False
        valid[:, :_RADIUS] = False
        valid[:, max(0, self.width - _RADIUS):] = False
        self.valid = valid
        self.sources = np.argwhere(valid).astype(np.int64)
        offsets_y, offsets_x = np.mgrid[-_RADIUS:_RADIUS + 1, -_RADIUS:_RADIUS + 1]
        self.offset_y = offsets_y.ravel().astype(np.int64)
        self.offset_x = offsets_x.ravel().astype(np.int64)
        self.set_estimate(estimate)

    def set_estimate(self, estimate: np.ndarray) -> None:
        self.estimate = estimate
        self.padded = np.pad(estimate, ((_RADIUS, _RADIUS), (_RADIUS, _RADIUS), (0, 0)),
                             mode="reflect")

    def patches(self, rows: np.ndarray, columns: np.ndarray) -> np.ndarray:
        return self.padded[rows[:, None] + _RADIUS + self.offset_y,
                           columns[:, None] + _RADIUS + self.offset_x]

    def legal(self, rows: np.ndarray, columns: np.ndarray) -> np.ndarray:
        inside = (rows >= 0) & (rows < self.height) & (columns >= 0) & (columns < self.width)
        result = np.zeros(rows.shape, dtype=bool)
        result[inside] = self.valid[rows[inside], columns[inside]]
        return result

    def random_sources(self, count: int, rng: np.random.Generator):
        picks = self.sources[rng.integers(0, len(self.sources), size=count)]
        return picks[:, 0].copy(), picks[:, 1].copy()


def _initial_field(level: _Level, seed_field, rng):
    count = level.target_y.size
    rows, columns = level.random_sources(count, rng)
    if seed_field is not None:
        coarse_h, coarse_w = seed_field.shape[:2]
        parent_y = np.minimum(level.target_y // 2, coarse_h - 1)
        parent_x = np.minimum(level.target_x // 2, coarse_w - 1)
        parent = seed_field[parent_y, parent_x]
        known = parent[:, 0] >= 0
        seeded_y = parent[:, 0] * 2 + level.target_y % 2
        seeded_x = parent[:, 1] * 2 + level.target_x % 2
        usable = known & level.legal(seeded_y, seeded_x)
        rows[usable] = seeded_y[usable]
        columns[usable] = seeded_x[usable]
    return rows, columns


def _costs(level: _Level, targets: np.ndarray, rows: np.ndarray,
           columns: np.ndarray, target_patches: np.ndarray) -> np.ndarray:
    source_patches = level.patches(rows, columns)
    return ((target_patches - source_patches) ** 2).sum(axis=(1, 2))


def _search(level: _Level, rows, columns, rng, iteration: int):
    """One Jacobi pass of propagation and random search over every target."""
    count = level.target_y.size
    new_rows, new_columns = rows.copy(), columns.copy()
    best = np.empty(count, dtype=np.float32)
    step = 1 if iteration % 2 == 0 else -1
    neighbours = ((0, step), (step, 0))
    search_radius = max(level.height, level.width)
    for part in _chunks(count):
        targets = np.arange(part.start, part.stop)
        ty, tx = level.target_y[part], level.target_x[part]
        target_patches = level.patches(ty, tx)
        best_rows, best_columns = rows[part].copy(), columns[part].copy()
        scores = _costs(level, targets, best_rows, best_columns, target_patches)

        def consider(candidate_rows, candidate_columns):
            nonlocal scores
            legal = level.legal(candidate_rows, candidate_columns)
            if not legal.any():
                return
            which = np.flatnonzero(legal)
            trial = _costs(level, targets[which], candidate_rows[which],
                           candidate_columns[which], target_patches[which])
            better = trial < scores[which]
            chosen = which[better]
            best_rows[chosen] = candidate_rows[which][better]
            best_columns[chosen] = candidate_columns[which][better]
            scores[chosen] = trial[better]

        for dy, dx in neighbours:
            ny, nx = ty - dy, tx - dx
            inside = (ny >= 0) & (ny < level.height) & (nx >= 0) & (nx < level.width)
            neighbour = np.full(ty.shape, -1, dtype=np.int64)
            neighbour[inside] = level.index[ny[inside], nx[inside]]
            present = neighbour >= 0
            if not present.any():
                continue
            candidate_rows = np.where(present, rows[np.maximum(neighbour, 0)] + dy, -1)
            candidate_columns = np.where(present, columns[np.maximum(neighbour, 0)] + dx, -1)
            consider(candidate_rows, candidate_columns)
        radius = search_radius
        while radius >= 1:
            jitter = rng.integers(-radius, radius + 1, size=(ty.size, 2))
            consider(best_rows + jitter[:, 0], best_columns + jitter[:, 1])
            radius //= 2
        new_rows[part], new_columns[part] = best_rows, best_columns
        best[part] = scores
    return new_rows, new_columns, best


def _vote(level: _Level, rows, columns, costs) -> np.ndarray:
    """Rebuild each hole pixel from every patch that covers it."""
    patch_area = PATCH * PATCH
    per_pixel = costs / patch_area
    scale = max(float(np.median(per_pixel)), _EPSILON)
    weights = np.exp(-per_pixel / (2.0 * scale)).astype(np.float64)
    flat_size = level.height * level.width
    accumulated = np.zeros((flat_size, 3), dtype=np.float64)
    total = np.zeros(flat_size, dtype=np.float64)
    for part in _chunks(level.target_y.size):
        pixel_y = level.target_y[part, None] + level.offset_y
        pixel_x = level.target_x[part, None] + level.offset_x
        inside = ((pixel_y >= 0) & (pixel_y < level.height)
                  & (pixel_x >= 0) & (pixel_x < level.width))
        keep = inside.copy()
        keep[inside] = level.hole[pixel_y[inside], pixel_x[inside]]
        if not keep.any():
            continue
        values = level.estimate[rows[part, None] + level.offset_y,
                                columns[part, None] + level.offset_x]
        flat = (pixel_y * level.width + pixel_x)[keep]
        weight = np.broadcast_to(weights[part, None], keep.shape)[keep]
        for channel in range(3):
            accumulated[:, channel] += np.bincount(
                flat, weights=values[..., channel][keep] * weight, minlength=flat_size)
        total += np.bincount(flat, weights=weight, minlength=flat_size)
    result = level.estimate.copy()
    covered = (total > _EPSILON).reshape(level.height, level.width) & level.hole
    rebuilt = (accumulated / np.maximum(total, _EPSILON)[:, None]).reshape(
        level.height, level.width, 3).astype(np.float32)
    result[covered] = rebuilt[covered]
    return result


def fill(image: np.ndarray, hole: np.ndarray, *, seed: int = 0) -> np.ndarray:
    """Return ``image`` with its ``hole`` pixels rebuilt from known pixels.

    ``image`` is float RGB in [0, 1] and ``hole`` a boolean mask of the same
    height and width. When no legal source patch exists, the nearest known
    pixel is used instead, so the result is always finite.
    """
    source = np.clip(np.asarray(image, dtype=np.float32)[..., :3], 0.0, 1.0)
    hole = np.asarray(hole, dtype=bool)
    if source.ndim != 3 or hole.shape != source.shape[:2] or not hole.any() or hole.all():
        return source.copy()
    from scipy import ndimage

    rng = np.random.default_rng(int(seed) & 0xFFFFFFFF)
    pyramid = [(source, hole)]
    while True:
        level_image, level_hole = pyramid[-1]
        depth = float(ndimage.distance_transform_edt(level_hole).max())
        if depth <= _RADIUS + 1 or min(level_hole.shape) // 2 < 3 * PATCH:
            break
        pyramid.append((_downsample(level_image), _downsample_mask(level_hole)))

    estimate = None
    field = None
    for depth_index in reversed(range(len(pyramid))):
        level_image, level_hole = pyramid[depth_index]
        if estimate is None:
            current = _nearest_fill(level_image, level_hole)
        else:
            current = level_image.copy()
            current[level_hole] = _upsample(estimate, level_image.shape)[level_hole]
        level = _Level(current, level_hole)
        if len(level.sources) < 4:
            estimate, field = current, None
            continue
        rows, columns = _initial_field(level, field, rng)
        finest = depth_index == 0
        large = level.target_y.size > 400_000
        iterations = 2 if (finest and large) else (3 if finest else 5)
        costs = None
        for iteration in range(iterations):
            rows, columns, costs = _search(level, rows, columns, rng, iteration)
            level.set_estimate(_vote(level, rows, columns, costs))
        estimate = level.estimate
        field = np.full((level.height, level.width, 2), -1, dtype=np.int64)
        field[level.target_y, level.target_x, 0] = rows
        field[level.target_y, level.target_x, 1] = columns

    output = source.copy()
    output[hole] = estimate[hole]
    return np.clip(output, 0.0, 1.0).astype(np.float32)
