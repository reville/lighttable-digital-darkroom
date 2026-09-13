# SPDX-License-Identifier: GPL-3.0-only
"""Camera-to-scene colour from a DNG camera profile, per the DNG specification.

A ``.dcp`` file carries the camera's colour calibration: ``ColorMatrix1``/``2``
(XYZ to camera, one per calibration illuminant) and optionally
``ForwardMatrix1``/``2`` (white-balanced camera to XYZ D50). This module turns
those tags, plus the white-balance multipliers the decoder applied, into one
3x3 matrix from white-balanced camera RGB to linear ProPhoto RGB, following
chapter 6 of the DNG 1.6 specification:

1. The capture illuminant's correlated colour temperature is found from the
   camera neutral by the specification's fixed-point iteration: guess a
   temperature, interpolate the colour matrix at it, map the neutral through
   the inverse matrix to XYZ, read the temperature back from its chromaticity
   and repeat until it settles.
2. The two calibrations are blended in reciprocal temperature (1/T), the
   weighting the specification prescribes and the same one
   ``camera_profile.interpolate_hue_sat_maps`` uses for the tables.
3. With a forward matrix the white-balanced camera values go straight to XYZ
   D50 (``ForwardMatrix`` is defined against the reference neutral, which is
   what a white-balanced decode already is). Without one, the inverse colour
   matrix gives XYZ under the scene illuminant and a Bradford transform
   adapts that to D50.
4. XYZ D50 goes to ProPhoto RGB, whose white point is D50, through the ROMM
   matrix; the result is scaled so the camera neutral lands on equal RGB.

Everything here is plain numpy over 3x3 matrices; the per-pixel work is a
single matrix multiply in :func:`convert_camera_native`. The chromaticity to
temperature step uses McCamy's cubic approximation, which is within a few
kelvin of the Robertson tables across the 2000..12500 K range camera
profiles are calibrated for; both calibration illuminants are far from that
error.

What is approximated, stated once here and in ``docs/camera-profiles.md``:

* ``AnalogBalance`` and ``ReductionMatrix`` are DNG *file* tags, not profile
  tags, and LibRaw does not expose them; they are taken as identity.
* For automatic white balance the decoder's chosen multipliers are not
  exposed, so the as-shot neutral stands in for the temperature estimate.
  The pixels are still balanced by the automatic multipliers.
* The calibration illuminants are taken at their nominal temperatures from
  ``camera_profile.ILLUMINANT_TEMPERATURES``.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np

import camera_profile

# Bumped whenever the rendering of a selected profile changes, so every cache
# keyed on the profile identity re-renders rather than serving stale pixels.
PIPELINE_VERSION = "dng-2"

# The temperature the tables blend at when no capture illuminant is known:
# the same daylight default the RAW white-balance control starts from.
DEFAULT_CAPTURE_TEMPERATURE = 5500.0

# ROMM RGB (ProPhoto) primaries to XYZ, D50 white, from the ROMM RGB
# specification (ISO 22028-2), rounded as published.
PROPHOTO_TO_XYZ_D50 = np.array([
    [0.7976749, 0.1351917, 0.0313534],
    [0.2880402, 0.7118741, 0.0000857],
    [0.0000000, 0.0000000, 0.8252100],
], dtype=np.float64)
XYZ_D50_TO_PROPHOTO = np.linalg.inv(PROPHOTO_TO_XYZ_D50)

# D50 as the ROMM matrix itself defines it (the XYZ of ProPhoto white), so
# an adapted neutral lands on exactly equal RGB. It differs from the ICC's
# rounded D50 (0.9642, 1.0, 0.8249) in the fourth decimal.
D50_XYZ = PROPHOTO_TO_XYZ_D50 @ np.ones(3)

# Bradford cone response matrix, as used by the DNG SDK for adaptation.
BRADFORD = np.array([
    [0.8951, 0.2664, -0.1614],
    [-0.7502, 1.7135, 0.0367],
    [0.0389, -0.0685, 1.0296],
], dtype=np.float64)

MIN_TEMPERATURE = 1000.0
MAX_TEMPERATURE = 25000.0


class CalibrationError(camera_profile.ProfileError):
    """A profile's matrices cannot be turned into a usable transform."""


# --- Small colour helpers -------------------------------------------------


def xyz_to_xy(xyz) -> tuple[float, float]:
    x, y, z = (float(v) for v in np.asarray(xyz, dtype=np.float64).ravel()[:3])
    total = x + y + z
    if not np.isfinite(total) or total <= 0.0:
        raise CalibrationError("a neutral mapped to a non-positive XYZ")
    return x / total, y / total


def xy_to_cct(x: float, y: float) -> float:
    """McCamy's cubic approximation of correlated colour temperature."""
    denominator = 0.1858 - float(y)
    if abs(denominator) < 1e-9:
        return MAX_TEMPERATURE
    n = (float(x) - 0.3320) / denominator
    cct = 449.0 * n ** 3 + 3525.0 * n ** 2 + 6823.3 * n + 5520.33
    if not np.isfinite(cct):
        return DEFAULT_CAPTURE_TEMPERATURE
    return float(np.clip(cct, MIN_TEMPERATURE, MAX_TEMPERATURE))


def bradford_adaptation(source_white_xyz, target_white_xyz=D50_XYZ) -> np.ndarray:
    """The 3x3 XYZ transform taking ``source_white_xyz`` onto the target."""
    source = BRADFORD @ np.asarray(source_white_xyz, dtype=np.float64).ravel()[:3]
    target = BRADFORD @ np.asarray(target_white_xyz, dtype=np.float64).ravel()[:3]
    if np.any(source <= 0.0):
        raise CalibrationError("the capture white is outside the Bradford domain")
    return np.linalg.inv(BRADFORD) @ np.diag(target / source) @ BRADFORD


def _matrix(value, label: str) -> np.ndarray:
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        raise CalibrationError(f"{label} is not a finite 3x3 matrix")
    return matrix


def _invert(matrix: np.ndarray, label: str) -> np.ndarray:
    try:
        return np.linalg.inv(matrix)
    except np.linalg.LinAlgError as exc:
        raise CalibrationError(f"{label} is singular") from exc


# --- Illuminant blending --------------------------------------------------


def illuminant_weight(illuminant1, illuminant2, temperature: float) -> float:
    """The weight of calibration 1 at ``temperature``, per DNG 1/T blending.

    Returns 1.0 at or below the cooler calibration's temperature and 0.0 at or
    above the warmer one, whichever order the two illuminants are tagged in.
    Mirrors ``camera_profile.interpolate_hue_sat_maps`` exactly so matrices
    and tables blend with the same weight.
    """
    t1 = camera_profile.illuminant_temperature(illuminant1)
    t2 = camera_profile.illuminant_temperature(illuminant2)
    if abs(t1 - t2) < 1e-4:
        return 1.0
    swapped = t1 > t2
    if swapped:
        t1, t2 = t2, t1
    t = float(np.clip(temperature, MIN_TEMPERATURE, MAX_TEMPERATURE))
    if t <= t1:
        weight_low = 1.0
    elif t >= t2:
        weight_low = 0.0
    else:
        weight_low = float(np.clip(
            (1.0 / t - 1.0 / t2) / (1.0 / t1 - 1.0 / t2), 0.0, 1.0))
    return 1.0 - weight_low if swapped else weight_low


def interpolate_matrices(matrix1, matrix2, illuminant1, illuminant2,
                         temperature: float) -> np.ndarray:
    """Blend two calibration matrices entry-wise in 1/T, as the spec does."""
    m1 = _matrix(matrix1, "matrix 1")
    if matrix2 is None or illuminant2 is None:
        return m1.copy()
    m2 = _matrix(matrix2, "matrix 2")
    w1 = illuminant_weight(illuminant1, illuminant2, temperature)
    return w1 * m1 + (1.0 - w1) * m2


def has_calibration(profile: dict | None) -> bool:
    """True when the profile can replace the decoder's own colour matrix."""
    return bool(profile) and profile.get("colorMatrix1") is not None


def _color_matrix_at(profile: dict, temperature: float) -> np.ndarray:
    return interpolate_matrices(
        profile["colorMatrix1"], profile.get("colorMatrix2"),
        profile.get("illuminant1"), profile.get("illuminant2"), temperature)


def _forward_matrix_at(profile: dict, temperature: float) -> np.ndarray | None:
    forward1 = profile.get("forwardMatrix1")
    if forward1 is None:
        return None
    forward2 = profile.get("forwardMatrix2")
    return interpolate_matrices(
        forward1, forward2 if forward2 is not None else None,
        profile.get("illuminant1"), profile.get("illuminant2"), temperature)


# --- Capture illuminant ---------------------------------------------------


def neutral_from_multipliers(multipliers, fallback=None) -> np.ndarray:
    """The camera-space neutral (``AsShotNeutral``) behind WB multipliers.

    LibRaw reports the multipliers it applied as ``(r, g, b[, g2])``; the
    neutral is their reciprocal, scaled so green is 1. A missing or zero
    multiplier (some files carry no as-shot balance) falls back to the
    daylight multipliers, and failing that to an equal-energy neutral.
    """
    for candidate in (multipliers, fallback):
        if candidate is None:
            continue
        values = np.asarray(list(candidate), dtype=np.float64).ravel()[:3]
        if values.size == 3 and np.all(np.isfinite(values)) and np.all(values > 0.0):
            neutral = values[1] / values
            return neutral
    return np.ones(3, dtype=np.float64)


def estimate_capture_temperature(profile: dict, neutral,
                                 *, iterations: int = 8) -> float:
    """The capture illuminant's temperature from a camera neutral (DNG 6.x).

    The colour matrix itself depends on the temperature being estimated, so
    the specification iterates: start at daylight, interpolate the matrix,
    map the neutral to XYZ, read the temperature from its chromaticity and
    repeat. Eight rounds settle to well under a kelvin for real profiles; a
    single-illuminant profile needs one.
    """
    if not has_calibration(profile):
        return DEFAULT_CAPTURE_TEMPERATURE
    neutral = np.asarray(neutral, dtype=np.float64).ravel()[:3]
    if neutral.size != 3 or not np.all(np.isfinite(neutral)) or np.any(neutral <= 0.0):
        raise CalibrationError("a camera neutral must be three positive values")
    dual = (profile.get("colorMatrix2") is not None
            and profile.get("illuminant2") is not None)
    temperature = DEFAULT_CAPTURE_TEMPERATURE
    for _ in range(max(1, iterations) if dual else 1):
        matrix = _color_matrix_at(profile, temperature)
        xyz = _invert(matrix, "ColorMatrix") @ neutral
        updated = xy_to_cct(*xyz_to_xy(xyz))
        converged = abs(updated - temperature) < 0.5
        temperature = updated
        if converged:
            break
    return float(temperature)


# --- Camera to ProPhoto ---------------------------------------------------


def camera_to_prophoto(profile: dict, neutral, temperature: float) -> np.ndarray:
    """The 3x3 matrix from white-balanced camera RGB to linear ProPhoto RGB.

    ``neutral`` is the camera-space neutral the decoder balanced by (so the
    input pixels already have that neutral at equal RGB) and ``temperature``
    the capture illuminant the matrices blend at. The result maps the
    neutral to exactly ``(1, 1, 1)``.
    """
    if not has_calibration(profile):
        raise CalibrationError("the profile carries no ColorMatrix1")
    neutral = np.asarray(neutral, dtype=np.float64).ravel()[:3]
    if neutral.size != 3 or np.any(neutral <= 0.0) or not np.all(np.isfinite(neutral)):
        raise CalibrationError("a camera neutral must be three positive values")
    forward = _forward_matrix_at(profile, temperature)
    if forward is not None:
        # DNG: XYZ_D50 = FM . D . cam, with D = inverse(diag(ReferenceNeutral)).
        # White-balanced input is D . cam already.
        camera_to_xyz = _matrix(forward, "ForwardMatrix")
    else:
        color = _color_matrix_at(profile, temperature)
        xyz_from_camera = _invert(color, "ColorMatrix")
        white_xyz = xyz_from_camera @ neutral
        # Undo the balance to reach camera values, take them to XYZ under the
        # scene illuminant, then adapt that illuminant to D50.
        camera_to_xyz = (bradford_adaptation(white_xyz, D50_XYZ)
                         @ xyz_from_camera @ np.diag(neutral))
    matrix = XYZ_D50_TO_PROPHOTO @ camera_to_xyz
    neutral_out = matrix @ np.ones(3)
    if not np.all(np.isfinite(neutral_out)) or neutral_out[1] <= 0.0:
        raise CalibrationError("the profile maps the camera neutral to a non-positive green")
    return matrix / neutral_out[1]


def convert_camera_native(rgb: np.ndarray, matrix) -> np.ndarray:
    """Apply a camera-to-ProPhoto matrix to decoder output, clipping to range.

    ``rgb`` is the decoder's white-balanced camera-native output (any float
    or integer dtype); the result has the same dtype and range, with values
    outside the ProPhoto gamut clipped, which is what the decoder's own
    matrix path does too.
    """
    matrix = _matrix(matrix, "camera-to-ProPhoto matrix")
    array = np.asarray(rgb)
    if array.ndim < 1 or array.shape[-1] != 3:
        raise CalibrationError(
            f"expected camera RGB with a final axis of 3, got shape {array.shape}")
    if np.issubdtype(array.dtype, np.integer):
        limit = float(np.iinfo(array.dtype).max)
        work = array.astype(np.float32)
        converted = work @ matrix.T.astype(np.float32)
        return np.clip(converted + 0.5, 0.0, limit).astype(array.dtype)
    work = array.astype(np.float32, copy=False)
    converted = work @ matrix.T.astype(np.float32)
    return np.clip(converted, 0.0, 1.0).astype(array.dtype, copy=False)


# --- Capture temperature memory --------------------------------------------
#
# The decode learns the capture temperature while the RAW is open; the
# Develop stage, which blends the profile's tables at that temperature, sees
# only pixels. This bounded memory carries it across, keyed by the source
# identity, the white-balance basis and the profile.

_TEMPERATURES: OrderedDict[tuple, float] = OrderedDict()
_TEMPERATURES_LOCK = threading.Lock()
_TEMPERATURES_LIMIT = 256


def white_balance_basis(wb_mode: str) -> str:
    """Which multipliers the decoder balanced by for a white-balance mode."""
    return "as_shot" if str(wb_mode) in ("as_shot", "auto") else "daylight"


def remember_capture_temperature(key: tuple, temperature: float) -> None:
    with _TEMPERATURES_LOCK:
        _TEMPERATURES[key] = float(temperature)
        _TEMPERATURES.move_to_end(key)
        while len(_TEMPERATURES) > _TEMPERATURES_LIMIT:
            _TEMPERATURES.popitem(last=False)


def recall_capture_temperature(key: tuple) -> float | None:
    with _TEMPERATURES_LOCK:
        value = _TEMPERATURES.get(key)
        if value is not None:
            _TEMPERATURES.move_to_end(key)
        return value


def forget_capture_temperatures() -> None:
    with _TEMPERATURES_LOCK:
        _TEMPERATURES.clear()


def needs_capture_temperature(profile: dict | None) -> bool:
    """True when the profile blends anything by the capture illuminant."""
    if not profile:
        return False
    if profile.get("illuminant2") is None:
        return False
    return any(profile.get(key) is not None
               for key in ("hueSatMap2", "colorMatrix2", "forwardMatrix2"))


def read_multipliers(path) -> tuple[list[float] | None, list[float] | None]:
    """The as-shot and daylight multipliers from a RAW header, without a decode.

    Opens the file through rawpy's header path only; nothing is unpacked or
    demosaiced, so this is cheap enough to call when the decode cache has
    already forgotten a file.
    """
    import rawpy
    reader = rawpy.RawPy()
    try:
        reader.open_file(str(Path(path)))
        as_shot = [float(v) for v in reader.camera_whitebalance]
        daylight = [float(v) for v in reader.daylight_whitebalance]
    finally:
        reader.close()
    return as_shot, daylight


def capture_temperature_for(path, wb_mode: str, profile: dict | None,
                            key: tuple | None) -> float | None:
    """The capture temperature the decode used, or a fresh estimate.

    Returns None when the profile has nothing to blend (single illuminant)
    or cannot estimate one (no colour matrix), so callers fall back to
    :data:`DEFAULT_CAPTURE_TEMPERATURE` explicitly rather than by accident.
    """
    if not needs_capture_temperature(profile):
        return None
    if key is not None:
        remembered = recall_capture_temperature(key)
        if remembered is not None:
            return remembered
    if not has_calibration(profile):
        return None
    try:
        as_shot, daylight = read_multipliers(path)
    except Exception:  # noqa: BLE001 - a header that will not open renders at the default
        return None
    basis = white_balance_basis(wb_mode)
    neutral = (neutral_from_multipliers(as_shot, daylight) if basis == "as_shot"
               else neutral_from_multipliers(daylight, as_shot))
    try:
        temperature = estimate_capture_temperature(profile, neutral)
    except CalibrationError:
        return None
    if key is not None:
        remember_capture_temperature(key, temperature)
    return temperature
