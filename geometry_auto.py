# SPDX-License-Identifier: GPL-3.0-only
"""Auto-straighten and guided upright for the lens pane.

Finds straight lines in a neutral preview and solves the *parametric* optics
model that :func:`edits.apply_manual_optics` already implements, so the answer
is a plain optics patch the preview cache can apply like any other edit.  The
model is a rotation plus two keystone shears plus radial distortion; it is not
a full homography, and the UI should say so.

Coordinates
-----------
Everything here works in the same normalised frame as
:func:`edits.apply_manual_optics`: the origin is the image centre and one unit
is half the *short* side, so the short axis spans -1..1 and the long axis
spans a little further.  ``y`` grows downwards, as in pixel space.

Angles are reported in that frame too, which is why a horizon that runs down
to the right has a positive angle and needs a positive ``rotate``.  The
roadmap states the same rule with a y-up sign convention ("rotate is -angle").
"""
from __future__ import annotations

import math

import numpy as np
from scipy.optimize import minimize_scalar
from skimage.feature import canny
from skimage.transform import probabilistic_hough_line, resize

import edits


MODES = ("level", "vertical", "full")

ROTATE_LIMIT = 15.0            # matches the clamp in edits.clean_optics
KEYSTONE_LIMIT = 1.0
MIN_SCALE, MAX_SCALE = 1.0, 1.6

_CANNY_SIGMA = 2.0
_HOUGH_THRESHOLD = 10
_HOUGH_RNG = 20260902          # fixed, so the same preview always answers alike
# 0.1 degree steps.  The 0.5 degree default quantises segment endpoints badly
# enough to bias a recovered horizon by well over a tenth of a degree, and the
# finer grid costs about 0.2 s on a 1100 px preview.
_HOUGH_THETA = np.linspace(-np.pi / 2.0, np.pi / 2.0, 1800, endpoint=False)
_LINE_LENGTH = 0.15            # shortest usable segment, as a share of the
_LINE_GAP = 0.02               # short side, and the gap it may bridge
_CLUSTER_DEG = 30.0            # within this of an axis to join that cluster
_AGREE_DEG = 2.0               # residual that still counts as agreement
_CONFIDENT_LINES = 4.0         # segment count at which the count factor is 1
_MIN_DETECTED = 2              # fewer usable segments than this means no answer
_MIN_KEYSTONE = 2              # a keystone needs two lines to compare
_DEFAULT_ASPECT = 1.5          # width / height assumed when none is supplied
# The bilinear tap in edits.apply_manual_optics needs a neighbour, and the
# normalised half-extent of a real pixel grid is a hair under the ideal one
# ((width - 1) / 2 rather than width / 2), so keep about a pixel of headroom.
_EDGE_MARGIN = 0.006


# --------------------------------------------------------------------------
# The parametric model.  These two functions MIRROR edits.apply_manual_optics.
# That function is written as an inverse map: for every output pixel it
# computes the source position to sample.  _source_from_output() is that exact
# chain (scale, rotation, keystone, radial) applied to points instead of a
# pixel grid, and _output_from_source() is its numerical inverse, i.e. where a
# piece of image content ends up once the warp is applied.
# If the model in edits.apply_manual_optics() ever changes, both of these must
# change with it or every solve below silently drifts.
# --------------------------------------------------------------------------
def _terms(optics) -> dict:
    clean = edits.clean_optics(optics)
    return {key: clean[key] for key in
            ("distortion", "vertical", "horizontal", "rotate", "scale")}


def _away_from_zero(values: np.ndarray, floor: float = 1e-6) -> np.ndarray:
    return np.where(values >= 0.0,
                    np.maximum(values, floor), np.minimum(values, -floor))


def _source_from_output(points, optics) -> np.ndarray:
    """Normalised output coordinates -> the source position sampled there."""
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    term = _terms(optics)
    angle = math.radians(term["rotate"])
    cosine, sine = math.cos(angle), math.sin(angle)
    nx = pts[:, 0] / term["scale"]
    ny = pts[:, 1] / term["scale"]
    rx = cosine * nx - sine * ny
    ry = sine * nx + cosine * ny
    px = rx * (1.0 + term["vertical"] * 0.45 * ry)
    py = ry * (1.0 + term["horizontal"] * 0.45 * rx)
    factor = 1.0 + term["distortion"] * 0.18 * (px * px + py * py)
    return np.stack((px * factor, py * factor), axis=1)


def _output_from_source(points, optics) -> np.ndarray:
    """Where source content lands in the output: the inverse of the above.

    Radial distortion is undone with Newton iterations on the radius and the
    two keystone shears with a fixed point iteration, which converges quickly
    because ``|keystone * 0.45 * r| < 0.5`` over the whole legal range.
    """
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    term = _terms(optics)
    px = pts[:, 0].astype(np.float64)
    py = pts[:, 1].astype(np.float64)
    distortion = term["distortion"]
    if distortion:
        target = np.hypot(px, py)
        radius = target.copy()
        for _ in range(12):
            residual = radius + 0.18 * distortion * radius ** 3 - target
            slope = 1.0 + 0.54 * distortion * radius * radius
            radius = radius - residual / _away_from_zero(slope)
        factor = _away_from_zero(1.0 + 0.18 * distortion * radius * radius)
        px, py = px / factor, py / factor
    rx, ry = px.copy(), py.copy()
    if term["vertical"] or term["horizontal"]:
        for _ in range(24):
            rx = px / _away_from_zero(1.0 + term["vertical"] * 0.45 * ry)
            ry = py / _away_from_zero(1.0 + term["horizontal"] * 0.45 * rx)
    angle = math.radians(term["rotate"])
    cosine, sine = math.cos(angle), math.sin(angle)
    nx = (cosine * rx + sine * ry) * term["scale"]
    ny = (-sine * rx + cosine * ry) * term["scale"]
    return np.stack((nx, ny), axis=1)


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------
def _greyscale(image, max_width: int):
    source = np.asarray(image)
    array = source
    if array.ndim == 3:
        array = array[..., :3]
        array = (array[..., 0] if array.shape[2] < 3 else
                 0.2126 * array[..., 0] + 0.7152 * array[..., 1]
                 + 0.0722 * array[..., 2])
    if array.ndim != 2 or array.size == 0:
        return None
    array = array.astype(np.float64, copy=False)
    if np.issubdtype(source.dtype, np.integer):
        array = array / float(np.iinfo(source.dtype).max)
    if not np.isfinite(array).all():
        array = np.nan_to_num(array, nan=0.0, posinf=1.0, neginf=0.0)
    array = np.clip(array, 0.0, 1.0)
    height, width = array.shape
    if min(height, width) < 24:
        return None
    longest = max(height, width)
    if longest > max_width:
        ratio = max_width / float(longest)
        array = resize(array, (max(24, int(round(height * ratio))),
                               max(24, int(round(width * ratio)))),
                       anti_aliasing=True, preserve_range=True)
    return array


def _normalise(x, y, width: int, height: int):
    half = max(min(width, height) / 2.0, 1.0)
    return ((x - (width - 1) / 2.0) / half, (y - (height - 1) / 2.0) / half)


def detect_lines(image: np.ndarray, *, max_width: int = 1100) -> list[tuple]:
    """Downscale, Canny, probabilistic Hough; return normalised segments.

    The frame is reduced until its longer side is at most ``max_width``, so a
    tall portrait preview is cut down as much as a landscape one.

    Each entry is ``((x0, y0), (x1, y1))`` in the centred -1..1 frame
    described in the module docstring.  Segments shorter than 15% of the short
    side are discarded: their endpoints land on whole pixels, so a short one
    cannot pin its own angle down to the tenth of a degree this needs.
    """
    grey = _greyscale(image, max_width)
    if grey is None:
        return []
    height, width = grey.shape
    short = min(height, width)
    edge = canny(grey, sigma=_CANNY_SIGMA)
    if not edge.any():
        return []
    minimum = max(16, int(round(short * _LINE_LENGTH)))
    raw = probabilistic_hough_line(
        edge, threshold=_HOUGH_THRESHOLD, line_length=minimum,
        line_gap=max(4, int(round(short * _LINE_GAP))), rng=_HOUGH_RNG,
        theta=_HOUGH_THETA,
    )
    lines = []
    for (x0, y0), (x1, y1) in raw:
        if math.hypot(x1 - x0, y1 - y0) < minimum:
            continue
        lines.append((_normalise(x0, y0, width, height),
                      _normalise(x1, y1, width, height)))
    return lines


def _as_array(lines) -> np.ndarray:
    """(n, 4) array of [x0, y0, x1, y1], dropping anything not finite."""
    rows = []
    for line in lines or []:
        try:
            (x0, y0), (x1, y1) = line
            row = [float(x0), float(y0), float(x1), float(y1)]
        except (TypeError, ValueError):
            continue
        if all(math.isfinite(value) for value in row):
            rows.append(row)
    if not rows:
        return np.zeros((0, 4), dtype=np.float64)
    return np.asarray(rows, dtype=np.float64)


def _fold(degrees: np.ndarray) -> np.ndarray:
    """Fold an angle into [-90, 90), where 0 means aligned with the axis."""
    return (degrees + 90.0) % 180.0 - 90.0


def _lengths(segments: np.ndarray) -> np.ndarray:
    return np.hypot(segments[:, 2] - segments[:, 0],
                    segments[:, 3] - segments[:, 1])


def _axis_angles(segments: np.ndarray, axis: str) -> np.ndarray:
    """Signed deviation in degrees from the horizontal or vertical axis."""
    dx = segments[:, 2] - segments[:, 0]
    dy = segments[:, 3] - segments[:, 1]
    if axis == "horizontal":
        return _fold(np.degrees(np.arctan2(dy, dx)))
    return _fold(np.degrees(np.arctan2(dx, dy)))


def _cluster(segments: np.ndarray) -> tuple:
    """Split into (near-horizontal, near-vertical); the rest is discarded."""
    if not len(segments):
        return segments, segments
    horizontal = np.abs(_axis_angles(segments, "horizontal")) <= _CLUSTER_DEG
    vertical = np.abs(_axis_angles(segments, "vertical")) <= _CLUSTER_DEG
    return segments[horizontal], segments[vertical & ~horizontal]


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    order = np.argsort(values)
    values, weights = values[order], weights[order]
    total = float(weights.sum())
    if total <= 0.0:
        return float(np.median(values)) if len(values) else 0.0
    cumulative = np.cumsum(weights) / total
    return float(values[int(np.searchsorted(cumulative, 0.5, side="left"))])


# --------------------------------------------------------------------------
# Solving
# --------------------------------------------------------------------------
def _mapped_deviations(segments: np.ndarray, optics: dict,
                       axis: str) -> np.ndarray:
    """Per-segment deviation from ``axis`` after the warp is applied."""
    points = segments.reshape(-1, 2)
    mapped = _output_from_source(points, optics).reshape(-1, 4)
    return _axis_angles(mapped, axis)


def _weighted_variance(values: np.ndarray, weights: np.ndarray) -> float:
    total = float(weights.sum())
    if total <= 0.0:
        return 0.0
    mean = float((values * weights).sum() / total)
    return float((weights * (values - mean) ** 2).sum() / total)


def _solve_keystone(segments: np.ndarray, weights: np.ndarray, base: dict,
                    key: str, axis: str) -> float:
    """Bounded 1-D search for the keystone value that makes lines parallel."""
    def objective(value: float) -> float:
        optics = dict(base)
        optics[key] = float(value)
        deviations = _mapped_deviations(segments, optics, axis)
        if not np.isfinite(deviations).all():
            return 1e6
        return _weighted_variance(deviations, weights)

    if _weighted_variance(_axis_angles(segments, axis), weights) < 1e-9:
        return 0.0
    result = minimize_scalar(objective, bounds=(-KEYSTONE_LIMIT,
                                                KEYSTONE_LIMIT),
                             method="bounded",
                             options={"xatol": 1e-4})
    value = float(getattr(result, "x", 0.0))
    if not math.isfinite(value):
        return 0.0
    return max(-KEYSTONE_LIMIT, min(KEYSTONE_LIMIT, value))


def _rotate_from(horizontal: np.ndarray, vertical: np.ndarray) -> tuple:
    """(angle in degrees, used vertical lines) for the level solve."""
    if len(horizontal):
        return (_weighted_median(_axis_angles(horizontal, "horizontal"),
                                 _lengths(horizontal)), False)
    if len(vertical):
        # A frame with only verticals still fixes the roll: a line tilted t
        # degrees off vertical is levelled by rotating -t.
        return (-_weighted_median(_axis_angles(vertical, "vertical"),
                                  _lengths(vertical)), True)
    return (0.0, False)


# --------------------------------------------------------------------------
# Scale
# --------------------------------------------------------------------------
def _frame_extents(aspect: float) -> tuple:
    aspect = aspect if math.isfinite(aspect) and aspect > 0 else _DEFAULT_ASPECT
    return (max(1.0, aspect), max(1.0, 1.0 / aspect))


def _border(extent_x: float, extent_y: float, samples: int = 48) -> np.ndarray:
    span = np.linspace(-1.0, 1.0, samples)
    xs = span * extent_x
    ys = span * extent_y
    top = np.stack((xs, np.full_like(xs, -extent_y)), axis=1)
    bottom = np.stack((xs, np.full_like(xs, extent_y)), axis=1)
    left = np.stack((np.full_like(ys, -extent_x), ys), axis=1)
    right = np.stack((np.full_like(ys, extent_x), ys), axis=1)
    return np.concatenate((top, bottom, left, right), axis=0)


def _fits(border: np.ndarray, optics: dict, scale: float,
          extent_x: float, extent_y: float) -> bool:
    probe = dict(optics)
    probe["scale"] = scale
    sampled = _source_from_output(border, probe)
    if not np.isfinite(sampled).all():
        return False
    return bool((np.abs(sampled[:, 0]) <= extent_x - _EDGE_MARGIN).all()
                and (np.abs(sampled[:, 1]) <= extent_y - _EDGE_MARGIN).all())


def solve_scale(optics: dict, *, aspect: float = _DEFAULT_ASPECT) -> float:
    """Smallest scale in [1.0, 1.6] that leaves no empty corner.

    The whole output border is walked through the same inverse map that
    :func:`edits.apply_manual_optics` uses; the smallest scale whose sampled
    region still lies inside the source is found by bisection, rounded *up* to
    three decimals so the rounding can never reopen a corner, then clamped.
    ``aspect`` is width / height; the default assumes a 3:2 frame because
    over-cropping is harmless while under-cropping shows black corners.
    """
    scale, _ = _scale_and_fit(optics, aspect)
    return scale


def _scale_and_fit(optics, aspect: float) -> tuple:
    base = dict(_terms(optics))
    if not any(base[key] for key in
               ("distortion", "vertical", "horizontal", "rotate")):
        # No warp at all, so edits.apply_manual_optics returns the frame
        # untouched and there is nothing to crop into.
        return (MIN_SCALE, True)
    extent_x, extent_y = _frame_extents(aspect)
    border = _border(extent_x, extent_y)
    if _fits(border, base, MIN_SCALE, extent_x, extent_y):
        return (MIN_SCALE, True)
    if not _fits(border, base, MAX_SCALE, extent_x, extent_y):
        return (MAX_SCALE, False)
    low, high = MIN_SCALE, MAX_SCALE
    for _ in range(40):
        middle = (low + high) / 2.0
        if _fits(border, base, middle, extent_x, extent_y):
            high = middle
        else:
            low = middle
    value = min(MAX_SCALE, math.ceil(high * 1000.0) / 1000.0)
    return (max(MIN_SCALE, value), True)


# --------------------------------------------------------------------------
# Guides
# --------------------------------------------------------------------------
def _from_guides(guides) -> tuple:
    """Split drawn guides into (horizontal, vertical) segment arrays."""
    horizontal, vertical = [], []
    for guide in guides or []:
        if not isinstance(guide, dict):
            continue
        points = guide.get("points")
        if not isinstance(points, (list, tuple)) or len(points) < 2:
            continue
        try:
            (x0, y0), (x1, y1) = points[0], points[1]
            row = [float(x0), float(y0), float(x1), float(y1)]
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in row):
            continue
        if math.hypot(row[2] - row[0], row[3] - row[1]) < 1e-3:
            continue
        kind = str(guide.get("kind", "")).lower()
        if kind == "horizontal":
            horizontal.append(row)
        elif kind == "vertical":
            vertical.append(row)
    return (np.asarray(horizontal, dtype=np.float64).reshape(-1, 4),
            np.asarray(vertical, dtype=np.float64).reshape(-1, 4))


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------
def _agreement(clusters, optics: dict, guided: bool) -> float:
    """Length share of the used lines that end up where they should.

    Horizontal lines are asked to come out level, vertical lines to come out
    parallel to one another.  Sparse evidence is discounted until the solve
    has ``_CONFIDENT_LINES`` segments behind it.
    """
    agreeing = 0.0
    total = 0.0
    count = 0
    for segments, axis in clusters:
        if not len(segments):
            continue
        weights = _lengths(segments)
        deviations = _mapped_deviations(segments, optics, axis)
        if not np.isfinite(deviations).all():
            continue
        if axis == "vertical":
            centre = float((deviations * weights).sum() / max(weights.sum(),
                                                              1e-9))
            deviations = deviations - centre
        agreeing += float(weights[np.abs(deviations) <= _AGREE_DEG].sum())
        total += float(weights.sum())
        count += len(segments)
    if total <= 0.0:
        return 0.0
    share = agreeing / total
    if not guided:
        share *= min(1.0, count / _CONFIDENT_LINES)
    return round(max(0.0, min(1.0, share)), 3)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def _patch(values: dict) -> dict:
    """Clamp, round, and drop anything that is not a real optics key."""
    limits = {"rotate": ROTATE_LIMIT, "vertical": KEYSTONE_LIMIT,
              "horizontal": KEYSTONE_LIMIT}
    result = {}
    for key, value in values.items():
        if key not in edits.OPTICS_DEFAULTS:
            continue
        if not math.isfinite(value):
            continue
        if key == "scale":
            result[key] = round(max(MIN_SCALE, min(MAX_SCALE, value)), 3)
            continue
        limit = limits.get(key)
        if limit is None:
            continue
        digits = 3 if key == "rotate" else 4
        result[key] = round(max(-limit, min(limit, value)), digits)
    return result


def analyze(image: np.ndarray, mode: str = "full",
            guides: list | None = None) -> dict:
    """Solve an optics patch that levels and uprights the frame.

    Returns ``{"optics": patch, "confidence": 0..1, "lines": int,
    "notes": [str]}``.  ``patch`` only ever holds keys from
    ``edits.OPTICS_DEFAULTS`` and always carries a ``scale`` that keeps the
    corners filled.  Drawn ``guides`` replace detection entirely.  Nothing in
    here raises: an unreadable frame comes back as an empty patch with
    confidence 0.0 and a note saying why.
    """
    try:
        return _analyze(image, mode, guides)
    except Exception as error:                       # never break a preview
        return {"optics": {}, "confidence": 0.0, "lines": 0,
                "notes": [f"Geometry analysis failed: {error}"]}


def _analyze(image, mode, guides) -> dict:
    mode = mode if mode in MODES else "full"
    notes: list[str] = []
    guided = bool(guides)
    if guided:
        horizontal, vertical = _from_guides(guides)
        drawn = len(horizontal) + len(vertical)
        if drawn:
            notes.append(f"Solved from {drawn} drawn "
                         f"{'guide' if drawn == 1 else 'guides'}.")
    else:
        horizontal, vertical = _cluster(_as_array(detect_lines(image)))

    aspect = _DEFAULT_ASPECT
    array = np.asarray(image) if image is not None else None
    if array is not None and array.ndim >= 2 and array.shape[0] > 0:
        aspect = float(array.shape[1]) / float(array.shape[0])

    wants_rotate = mode in ("level", "full")
    wants_vertical = mode in ("vertical", "full")
    wants_horizontal = mode == "full"

    # Which clusters this mode leans on.  Level falls back to the vertical
    # lines when a frame has no horizon at all, which is common in
    # architecture, so that cluster has to count towards the line budget too.
    use_horizontal = bool(len(horizontal)) and (wants_rotate or
                                                wants_horizontal)
    use_vertical = bool(len(vertical)) and (
        wants_vertical or (wants_rotate and not len(horizontal)))
    used = []
    if use_horizontal:
        used.append((horizontal, "horizontal"))
    if use_vertical:
        used.append((vertical, "vertical"))
    available = sum(len(segments) for segments, _ in used)
    minimum = 1 if guided else _MIN_DETECTED
    if available < minimum:
        notes.append("Not enough straight lines were found, so the geometry "
                     "was left alone.")
        return {"optics": {}, "confidence": 0.0, "lines": available,
                "notes": notes}

    solved: dict = {}
    if wants_rotate:
        angle, from_vertical = _rotate_from(horizontal, vertical)
        if from_vertical:
            notes.append("No horizon was found; the roll was taken from the "
                         "vertical lines instead.")
        if abs(angle) > ROTATE_LIMIT:
            notes.append(f"Rotation clamped to {ROTATE_LIMIT:g} degrees; the "
                         f"frame asked for {angle:.1f}.")
        solved["rotate"] = max(-ROTATE_LIMIT, min(ROTATE_LIMIT, angle))

    if wants_vertical:
        if len(vertical) >= _MIN_KEYSTONE:
            solved["vertical"] = _solve_keystone(
                vertical, _lengths(vertical), solved, "vertical", "vertical")
        else:
            notes.append("Too few vertical lines to solve the vertical "
                         "keystone.")

    if wants_horizontal:
        if len(horizontal) >= _MIN_KEYSTONE:
            solved["horizontal"] = _solve_keystone(
                horizontal, _lengths(horizontal), solved, "horizontal",
                "horizontal")
            # One refinement pass: the horizontal keystone shears the horizon
            # off level again, so re-level against the warped lines.
            residual = _weighted_median(
                _mapped_deviations(horizontal, solved, "horizontal"),
                _lengths(horizontal))
            if math.isfinite(residual):
                solved["rotate"] = max(
                    -ROTATE_LIMIT,
                    min(ROTATE_LIMIT, solved.get("rotate", 0.0) + residual))
        else:
            notes.append("Too few horizontal lines to solve the horizontal "
                         "keystone.")

    patch = _patch(solved)
    scale, fitted = _scale_and_fit(patch, aspect)
    patch["scale"] = scale
    if not fitted:
        notes.append(f"Scale capped at {MAX_SCALE:g}; a little of the frame "
                     "edge may still be empty.")
    confidence = _agreement(used, patch, guided)
    return {"optics": patch, "confidence": confidence, "lines": available,
            "notes": notes}
