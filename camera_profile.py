# SPDX-License-Identifier: GPL-3.0-only
"""DNG camera-profile (``.dcp``) reading and an explicitly approximate look.

A ``.dcp`` file is a TIFF container whose first IFD holds only metadata: no
image data, no strips, no tiles. This module parses that IFD with
``tifffile`` and applies the parts of a profile that are cheap and safe to
apply to the LibRaw ProPhoto output: the hue/saturation map, the look table,
and the profile tone curve.

Honesty, stated once here and repeated in every ``profile_summary`` result so
the interface can repeat it to the user:

**This is an approximate application of a camera profile, not a DNG-accurate
render.** Specifically:

* No dual-illuminant interpolation. A profile carries two calibrations (for
  example Standard Light A and D65) that a correct renderer blends by the
  white balance of the frame. Only illuminant 1 is used here;
  ``ProfileHueSatMapData2``, ``ColorMatrix2`` and ``ForwardMatrix2`` are read
  and reported but never applied.
* No forward-matrix or colour-matrix chromatic adaptation. The matrices map
  camera space to XYZ; our input is already demosaiced ProPhoto RGB from
  LibRaw, so applying them would double-count the conversion. They are parsed
  for display only.
* The look table is applied *before* the tone curve. The DNG reference
  pipeline applies the hue/sat map first, then the tone curve, then the look
  table in the tone-mapped domain. Our order is the one the Film Lab render
  path asks for; the visible difference is small but real for strong looks.
* ``ProfileHueSatMapEncoding`` and ``ProfileLookTableEncoding`` (DNG 1.4) are
  ignored: the tables are interpolated in the domain the pixels arrive in.
* ``BaselineExposureOffset`` and ``DefaultBlackRender`` are ignored.

Everything the module *does* do is exact within float precision: the grid
interpolation is the standard trilinear one over (hue, saturation, value)
with a wrapping hue axis, and the tone curve is the DNG hue-preserving RGB
tone rather than three independent channel curves.

Nothing is bundled. The user points at their own profile folder and no
network access happens anywhere in this module.

Tag numbers follow the DNG specification (cross-checked against
``tifffile.TIFF.TAGS``): 50937 is ``ProfileHueSatMapDims``, 50941 is
``ProfileEmbedPolicy``, 50932 is ``ProfileCalibrationSignature``, 50981 is
``ProfileLookTableDims`` and 50982 is ``ProfileLookTableData``.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import tifffile


class ProfileError(Exception):
    """A ``.dcp`` file could not be read, or a table could not be applied."""


# --- Tags -----------------------------------------------------------------

TAG_COLOR_MATRIX_1 = 50721
TAG_COLOR_MATRIX_2 = 50722
TAG_CALIBRATION_ILLUMINANT_1 = 50778
TAG_CALIBRATION_ILLUMINANT_2 = 50779
TAG_PROFILE_CALIBRATION_SIGNATURE = 50932
TAG_PROFILE_NAME = 50936
TAG_PROFILE_HUE_SAT_MAP_DIMS = 50937
TAG_PROFILE_HUE_SAT_MAP_DATA_1 = 50938
TAG_PROFILE_HUE_SAT_MAP_DATA_2 = 50939
TAG_PROFILE_TONE_CURVE = 50940
TAG_PROFILE_EMBED_POLICY = 50941
TAG_PROFILE_COPYRIGHT = 50942
TAG_FORWARD_MATRIX_1 = 50964
TAG_FORWARD_MATRIX_2 = 50965
TAG_PROFILE_LOOK_TABLE_DIMS = 50981
TAG_PROFILE_LOOK_TABLE_DATA = 50982
TAG_PROFILE_HUE_SAT_MAP_ENCODING = 51107
TAG_PROFILE_LOOK_TABLE_ENCODING = 51108
TAG_BASELINE_EXPOSURE_OFFSET = 51109
TAG_DEFAULT_BLACK_RENDER = 51110

_RATIONAL_TYPES = (5, 10)

PROFILE_EXTENSION = ".dcp"

# A profile is metadata only; the largest published ones are a few megabytes.
# The cap keeps a mistargeted folder from loading a RAW file into memory.
MAX_PROFILE_BYTES = 64 * 1024 * 1024

# ``list_profiles`` stops here so a folder of thousands of files cannot stall
# the interface. A caller can detect the cap with ``len(result) ==
# MAX_PROFILES``.
MAX_PROFILES = 500

# EXIF LightSource codes, as used by CalibrationIlluminant1/2.
ILLUMINANT_NAMES = {
    0: "Unknown",
    1: "Daylight",
    2: "Fluorescent",
    3: "Tungsten",
    4: "Flash",
    9: "Fine weather",
    10: "Cloudy",
    11: "Shade",
    12: "Daylight fluorescent",
    13: "Day white fluorescent",
    14: "Cool white fluorescent",
    15: "White fluorescent",
    17: "Standard light A",
    18: "Standard light B",
    19: "Standard light C",
    20: "D55",
    21: "D65",
    22: "D75",
    23: "D50",
    24: "ISO studio tungsten",
    255: "Other",
}

# Standard illuminant correlated color temperatures (Kelvin) per DNG specification.
ILLUMINANT_TEMPERATURES = {
    1: 5500.0,   # Daylight
    2: 4230.0,   # Fluorescent
    3: 3200.0,   # Tungsten
    4: 5500.0,   # Flash
    9: 5500.0,   # Fine weather
    10: 6500.0,  # Cloudy
    11: 7500.0,  # Shade
    12: 6500.0,  # Daylight fluorescent
    13: 5000.0,  # Day white fluorescent
    14: 4200.0,  # Cool white fluorescent
    15: 3450.0,  # White fluorescent
    17: 2856.0,  # Standard light A
    18: 4874.0,  # Standard light B
    19: 6774.0,  # Standard light C
    20: 5503.0,  # D55
    21: 6504.0,  # D65
    22: 7504.0,  # D75
    23: 5003.0,  # D50
    24: 3200.0,  # ISO studio tungsten
    255: 5500.0, # Other
}

EMBED_POLICY_NAMES = {
    0: "Allow copying",
    1: "Embed if used",
    2: "Embed never",
    3: "No restrictions",
}

APPROXIMATION_NOTE = (
    "Approximate camera-profile look: the hue/saturation map (with dual-illuminant "
    "temperature interpolation when available), look table, and tone curve. "
    "No forward-matrix or colour-matrix chromatic adaptation."
)

_BASE_UNSUPPORTED = (
    "dual-illuminant matrix adaptation",
    "forward-matrix chromatic adaptation",
)


def illuminant_temperature(illuminant_code: int | None) -> float:
    """Return the correlated color temperature (Kelvin) for a calibration illuminant."""
    if illuminant_code is None:
        return 5500.0
    return ILLUMINANT_TEMPERATURES.get(int(illuminant_code), 5500.0)


def interpolate_hue_sat_maps(map1: np.ndarray, map2: np.ndarray,
                            illuminant1: int, illuminant2: int,
                            temperature: float) -> np.ndarray:
    """Interpolate two ProfileHueSatMap tables using DNG 1.4 1/T weighting.

    ``map1`` corresponds to ``illuminant1``, ``map2`` to ``illuminant2``.
    ``temperature`` is the capture white-balance CCT in Kelvin.
    """
    t1 = illuminant_temperature(illuminant1)
    t2 = illuminant_temperature(illuminant2)
    m1 = np.asarray(map1, dtype=np.float32)
    m2 = np.asarray(map2, dtype=np.float32)

    if abs(t1 - t2) < 1e-4:
        return m1.copy()

    # Ensure t1 < t2 for the 1/T interval
    if t1 > t2:
        t1, t2 = t2, t1
        m1, m2 = m2, m1

    t = float(np.clip(temperature, 1000.0, 25000.0))
    if t <= t1:
        return m1.copy()
    if t >= t2:
        return m2.copy()

    # DNG 1.4 section: interpolation weight g in 1/T space
    inv_t = 1.0 / t
    inv_t1 = 1.0 / t1
    inv_t2 = 1.0 / t2
    g = (inv_t - inv_t2) / (inv_t1 - inv_t2)
    w1 = float(np.clip(g, 0.0, 1.0))
    w2 = 1.0 - w1

    # Shortest-arc angular interpolation for hue shift (degrees)
    hue1 = m1[..., 0]
    hue2 = m2[..., 0]
    hue_delta = ((hue2 - hue1 + 180.0) % 360.0) - 180.0
    interp_hue = (hue1 + w2 * hue_delta) % 360.0

    # Linear interpolation for saturation and value scaling factors
    interp_sat = w1 * m1[..., 1] + w2 * m2[..., 1]
    interp_val = w1 * m1[..., 2] + w2 * m2[..., 2]

    return np.stack((interp_hue, interp_sat, interp_val), axis=-1).astype(m1.dtype)



# --- tifffile plumbing ----------------------------------------------------


class _ImagelessPageFilter(logging.Filter):
    """Drop the two errors tifffile logs for an IFD that carries no image.

    A camera profile legitimately has neither StripOffsets nor
    StripByteCounts, so those two messages are noise on every read. Anything
    else tifffile logs still gets through.
    """

    _BENIGN = (
        "missing data offset tag",
        "missing data ByteCounts tag",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(text in message for text in self._BENIGN)


@contextmanager
def _quiet_imageless_page():
    logger = logging.getLogger("tifffile")
    noise = _ImagelessPageFilter()
    logger.addFilter(noise)
    try:
        yield
    finally:
        logger.removeFilter(noise)


def _tag_floats(tag) -> np.ndarray | None:
    """Return a tag's numeric payload as a flat float64 array."""
    value = tag.value
    if value is None:
        return None
    if isinstance(value, np.ndarray):
        data = np.asarray(value, dtype=np.float64).ravel()
    elif isinstance(value, (tuple, list)):
        data = np.asarray(value, dtype=np.float64).ravel()
    elif isinstance(value, (int, float, np.integer, np.floating)):
        data = np.asarray([value], dtype=np.float64)
    else:
        return None
    count = int(getattr(tag, "count", 0) or 0)
    if int(tag.dtype) in _RATIONAL_TYPES and count and data.size == 2 * count:
        pairs = data.reshape(-1, 2)
        denominator = np.where(pairs[:, 1] == 0.0, 1.0, pairs[:, 1])
        data = pairs[:, 0] / denominator
    return data


def _tag_ints(tag) -> tuple[int, ...] | None:
    data = _tag_floats(tag)
    if data is None or data.size == 0:
        return None
    return tuple(int(round(item)) for item in data.tolist())


def _tag_text(tag) -> str | None:
    """Return a string tag, whether it arrives as text, bytes or bytes-as-ints.

    Most profiles store ProfileName and friends as ASCII, but some write them
    as BYTE, which tifffile hands back as integers.
    """
    value = tag.value
    if isinstance(value, np.ndarray):
        value = value.ravel().tolist()
    if isinstance(value, (tuple, list)):
        if all(isinstance(item, (int, np.integer)) for item in value):
            value = bytes(int(item) & 0xFF for item in value)
        else:
            value = "".join(str(item) for item in value)
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    if not isinstance(value, str):
        return None
    text = value.replace("\x00", "").strip()
    return text or None


def _read_matrix(page, code: int) -> np.ndarray | None:
    tag = page.tags.get(code)
    if tag is None:
        return None
    data = _tag_floats(tag)
    if data is None or data.size != 9:
        return None
    return data.reshape(3, 3)


def _read_dims(page, code: int) -> tuple[int, int, int] | None:
    tag = page.tags.get(code)
    if tag is None:
        return None
    dims = _tag_ints(tag)
    if dims is None or len(dims) != 3:
        return None
    if any(item < 1 for item in dims):
        return None
    return (dims[0], dims[1], dims[2])


def _read_grid(page, code: int, dims, label: str) -> np.ndarray | None:
    """Read a hue/sat-style table and reshape it to (value, hue, sat, 3)."""
    tag = page.tags.get(code)
    if tag is None:
        return None
    data = _tag_floats(tag)
    if data is None or data.size == 0:
        return None
    if dims is None:
        raise ProfileError(f"{label} has no matching dimensions tag")
    return _shape_grid(data, dims, label)


def _first_int(page, code: int) -> int | None:
    """Read a tag's first integer, for the small scalar profile tags."""
    tag = page.tags.get(code)
    if tag is None:
        return None
    values = _tag_ints(tag)
    if not values:
        return None
    return values[0]


# --- Reading --------------------------------------------------------------


def read_profile(path) -> dict:
    """Parse a ``.dcp`` file into a plain dict.

    Raises ``ProfileError`` — and nothing else — for a file that is missing,
    unreadable, not a TIFF, structurally corrupt, or carries no camera-profile
    tags at all. Tables that are absent come back as ``None``; tables that are
    present come back as numpy arrays, so the result is *not* JSON
    serialisable. Use ``profile_summary`` for that.

    Keys: ``name``, ``copyright``, ``calibrationSignature``, ``hueSatDims``,
    ``hueSatMap1``, ``hueSatMap2``, ``lookDims``, ``lookTable``,
    ``toneCurve``, ``forwardMatrix1``, ``forwardMatrix2``, ``colorMatrix1``,
    ``colorMatrix2``, ``illuminant1``, ``illuminant2``, ``embedPolicy``,
    ``path``, ``approximate`` and ``unsupported``.
    """
    try:
        file_path = Path(path)
    except TypeError as exc:
        raise ProfileError(
            f"a camera profile path must be a path, got "
            f"{type(path).__name__}") from exc
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise ProfileError(f"cannot open {file_path.name}: {exc}") from exc
    if size == 0:
        raise ProfileError(f"{file_path.name} is empty")
    if size > MAX_PROFILE_BYTES:
        raise ProfileError(
            f"{file_path.name} is {size / 1e6:.0f} MB; a camera profile is "
            f"metadata only and should be far smaller"
        )

    try:
        with _quiet_imageless_page():
            with tifffile.TiffFile(str(file_path)) as document:
                pages = document.pages
                if not len(pages):
                    raise ProfileError(
                        f"{file_path.name} has no TIFF directory")
                profile = _parse_page(pages[0], file_path)
    except ProfileError:
        raise
    except Exception as exc:  # tifffile raises a wide range of its own
        raise ProfileError(
            f"{file_path.name} is not a readable camera profile: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return profile


def _parse_page(page, file_path: Path) -> dict:
    tags = page.tags

    name_tag = tags.get(TAG_PROFILE_NAME)
    copyright_tag = tags.get(TAG_PROFILE_COPYRIGHT)
    signature_tag = tags.get(TAG_PROFILE_CALIBRATION_SIGNATURE)
    policy_tag = tags.get(TAG_PROFILE_EMBED_POLICY)

    hue_sat_dims = _read_dims(page, TAG_PROFILE_HUE_SAT_MAP_DIMS)
    look_dims = _read_dims(page, TAG_PROFILE_LOOK_TABLE_DIMS)

    hue_sat_map_1 = _read_grid(
        page, TAG_PROFILE_HUE_SAT_MAP_DATA_1, hue_sat_dims,
        "ProfileHueSatMapData1")
    hue_sat_map_2 = _read_grid(
        page, TAG_PROFILE_HUE_SAT_MAP_DATA_2, hue_sat_dims,
        "ProfileHueSatMapData2")
    look_table = _read_grid(
        page, TAG_PROFILE_LOOK_TABLE_DATA, look_dims, "ProfileLookTableData")

    tone_tag = tags.get(TAG_PROFILE_TONE_CURVE)
    tone_curve = None
    if tone_tag is not None:
        tone_data = _tag_floats(tone_tag)
        if tone_data is not None and tone_data.size >= 4:
            if tone_data.size % 2:
                raise ProfileError(
                    "ProfileToneCurve has an odd number of values; it must be "
                    "interleaved x,y pairs")
            tone_curve = _shape_curve(tone_data)

    profile = {
        "path": str(file_path),
        "name": _tag_text(name_tag) if name_tag is not None else None,
        "copyright": (
            _tag_text(copyright_tag) if copyright_tag is not None else None),
        "calibrationSignature": (
            _tag_text(signature_tag) if signature_tag is not None else None),
        "hueSatDims": hue_sat_dims,
        "hueSatMap1": hue_sat_map_1,
        "hueSatMap2": hue_sat_map_2,
        "lookDims": look_dims,
        "lookTable": look_table,
        "toneCurve": tone_curve,
        "forwardMatrix1": _read_matrix(page, TAG_FORWARD_MATRIX_1),
        "forwardMatrix2": _read_matrix(page, TAG_FORWARD_MATRIX_2),
        "colorMatrix1": _read_matrix(page, TAG_COLOR_MATRIX_1),
        "colorMatrix2": _read_matrix(page, TAG_COLOR_MATRIX_2),
        "illuminant1": _first_int(page, TAG_CALIBRATION_ILLUMINANT_1),
        "illuminant2": _first_int(page, TAG_CALIBRATION_ILLUMINANT_2),
        "embedPolicy": (
            (_tag_ints(policy_tag) or (None,))[0]
            if policy_tag is not None else None),
        "hueSatMapEncoding": _first_int(
            page, TAG_PROFILE_HUE_SAT_MAP_ENCODING),
        "lookTableEncoding": _first_int(
            page, TAG_PROFILE_LOOK_TABLE_ENCODING),
        "baselineExposureOffset": _matrix_scalar(
            page, TAG_BASELINE_EXPOSURE_OFFSET),
        "defaultBlackRender": _first_int(
            page, TAG_DEFAULT_BLACK_RENDER),
    }

    if not _has_profile_content(profile):
        raise ProfileError(
            f"{file_path.name} is a TIFF but carries no camera-profile tags")

    profile["approximate"] = True
    profile["unsupported"] = _unsupported_for(profile)
    profile["notes"] = APPROXIMATION_NOTE
    return profile


def _matrix_scalar(page, code: int) -> float | None:
    tag = page.tags.get(code)
    if tag is None:
        return None
    data = _tag_floats(tag)
    if data is None or data.size == 0:
        return None
    return float(data[0])


def _has_profile_content(profile: dict) -> bool:
    keys = (
        "name", "copyright", "calibrationSignature", "hueSatDims",
        "hueSatMap1", "lookDims", "lookTable", "toneCurve", "forwardMatrix1",
        "colorMatrix1", "illuminant1", "embedPolicy",
    )
    return any(profile.get(key) is not None for key in keys)


def _unsupported_for(profile: dict) -> list[str]:
    """Name what this file carries that the approximation skips."""
    skipped: list[str] = []
    dual = any(
        profile.get(key) is not None
        for key in (
            "hueSatMap2", "colorMatrix2", "forwardMatrix2", "illuminant2")
    )
    if dual:
        skipped.append(
            "dual-illuminant matrix adaptation (hue/sat map interpolation is supported, matrices are not)")
    if profile.get("forwardMatrix1") is not None:
        skipped.append("forward-matrix chromatic adaptation")
    if profile.get("colorMatrix1") is not None:
        skipped.append("colour-matrix camera-to-XYZ conversion")
    if profile.get("lookTable") is not None:
        skipped.append(
            "look-table ordering (applied before the tone curve, not after)")
    if profile.get("hueSatMapEncoding"):
        skipped.append("ProfileHueSatMapEncoding")
    if profile.get("lookTableEncoding"):
        skipped.append("ProfileLookTableEncoding")
    if profile.get("baselineExposureOffset"):
        skipped.append("BaselineExposureOffset")
    if profile.get("defaultBlackRender"):
        skipped.append("DefaultBlackRender")
    return skipped


def profile_summary(path) -> dict:
    """A JSON-safe description of a profile, including what is approximated.

    Every summary states ``approximate: True`` and lists, in ``unsupported``,
    the operations this specific file carries that the approximation skips.
    """
    profile = read_profile(path)
    file_path = Path(profile["path"])
    hue_sat_dims = profile["hueSatDims"]
    look_dims = profile["lookDims"]
    tone_curve = profile["toneCurve"]
    return {
        "path": profile["path"],
        "file": file_path.name,
        "name": profile["name"] or file_path.stem,
        "copyright": profile["copyright"],
        "calibrationSignature": profile["calibrationSignature"],
        "illuminant1": profile["illuminant1"],
        "illuminant1Name": _illuminant_name(profile["illuminant1"]),
        "illuminant2": profile["illuminant2"],
        "illuminant2Name": _illuminant_name(profile["illuminant2"]),
        "dualIlluminant": profile["illuminant2"] is not None,
        "hueSatDims": hue_sat_dims,
        "lookDims": look_dims,
        "hasHueSatMap1": profile["hueSatMap1"] is not None,
        "hasHueSatMap2": profile["hueSatMap2"] is not None,
        "hasLookTable": profile["lookTable"] is not None,
        "hasToneCurve": tone_curve is not None,
        "toneCurvePoints": 0 if tone_curve is None else int(len(tone_curve)),
        "hasForwardMatrix1": profile["forwardMatrix1"] is not None,
        "hasForwardMatrix2": profile["forwardMatrix2"] is not None,
        "hasColorMatrix1": profile["colorMatrix1"] is not None,
        "hasColorMatrix2": profile["colorMatrix2"] is not None,
        "embedPolicy": profile["embedPolicy"],
        "embedPolicyName": EMBED_POLICY_NAMES.get(
            profile["embedPolicy"], "Unknown"),
        "approximate": True,
        "unsupported": list(profile["unsupported"]),
        "notes": APPROXIMATION_NOTE,
        "ok": True,
    }


def _illuminant_name(code) -> str | None:
    if code is None:
        return None
    return ILLUMINANT_NAMES.get(int(code), f"Illuminant {int(code)}")


def list_profiles(folder) -> list[dict]:
    """Summarise the ``.dcp`` files in a user-chosen folder.

    Scans the folder itself and one level of subfolders — the shape Adobe and
    most profile packs ship — never deeper. Files that cannot be parsed are
    skipped as profiles but still reported, as ``{"ok": False, "error": ...}``
    entries, so the interface can say what it ignored and why. At most
    ``MAX_PROFILES`` entries are returned.
    """
    root = Path(folder)
    if not root.is_dir():
        raise ProfileError(f"{root} is not a folder")

    candidates: list[Path] = []
    for entry in _sorted_entries(root):
        if entry.is_dir():
            candidates.extend(
                child for child in _sorted_entries(entry)
                if _is_profile_file(child))
        elif _is_profile_file(entry):
            candidates.append(entry)

    results: list[dict] = []
    for candidate in candidates:
        if len(results) >= MAX_PROFILES:
            break
        try:
            results.append(profile_summary(candidate))
        except ProfileError as exc:
            results.append({
                "path": str(candidate),
                "file": candidate.name,
                "name": candidate.stem,
                "ok": False,
                "error": str(exc),
            })
    return results


def _sorted_entries(folder: Path) -> list[Path]:
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    entries = [entry for entry in entries if not entry.name.startswith(".")]
    return sorted(entries, key=lambda item: item.name.lower())


def _is_profile_file(path: Path) -> bool:
    if path.name.startswith("."):
        return False
    if path.suffix.lower() != PROFILE_EXTENSION:
        return False
    try:
        return path.is_file()
    except OSError:
        return False


# --- Table shaping --------------------------------------------------------


def _shape_grid(table, dims, label: str = "table") -> np.ndarray:
    """Normalise a hue/sat grid to (value, hue, sat, 3) float32.

    Accepts a flat array of triples, an (n, 3) array, or an already-shaped
    (value, hue, sat, 3) array. The DNG layout is value slowest, then hue,
    then saturation fastest.
    """
    dims = _shape_dims(dims, label)
    hue_divisions, sat_divisions, value_divisions = dims
    expected = hue_divisions * sat_divisions * value_divisions
    data = np.asarray(table, dtype=np.float32)
    if data.size != expected * 3:
        raise ProfileError(
            f"{label} has {data.size} values but dimensions "
            f"{hue_divisions}x{sat_divisions}x{value_divisions} need "
            f"{expected * 3}")
    return data.reshape(value_divisions, hue_divisions, sat_divisions, 3)


def _shape_dims(dims, label: str = "table") -> tuple[int, int, int]:
    try:
        values = tuple(int(item) for item in np.asarray(dims).ravel().tolist())
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"{label} dimensions are not numeric") from exc
    if len(values) != 3:
        raise ProfileError(
            f"{label} dimensions must be (hue, saturation, value), got "
            f"{values}")
    if any(item < 1 for item in values):
        raise ProfileError(
            f"{label} dimensions must all be at least 1, got {values}")
    return values


def _shape_curve(curve) -> np.ndarray:
    """Normalise a tone curve to an (n, 2) float32 array sorted by x."""
    data = np.asarray(curve, dtype=np.float32)
    if data.ndim == 1:
        if data.size % 2:
            raise ProfileError(
                "a tone curve must be interleaved x,y pairs; got an odd "
                f"count of {data.size}")
        data = data.reshape(-1, 2)
    elif data.ndim != 2 or data.shape[1] != 2:
        raise ProfileError(
            f"a tone curve must be (n, 2) x,y pairs; got shape {data.shape}")
    if data.shape[0] == 0:
        raise ProfileError("a tone curve must have at least one point")
    order = np.argsort(data[:, 0], kind="stable")
    return np.ascontiguousarray(data[order])


# --- Colour conversion ----------------------------------------------------


def _as_rgb(rgb) -> tuple[np.ndarray, np.dtype]:
    """Normalise an RGB array to contiguous float in 0..1, as this module
    expects. Integer input is scaled by its dtype maximum, matching
    ``color_pipeline.as_float_rgb``, rather than silently clipping to white.
    """
    array = np.asarray(rgb)
    if array.ndim < 1 or array.shape[-1] != 3:
        raise ProfileError(
            f"expected an RGB array with a final axis of 3, got shape "
            f"{array.shape}")
    if np.issubdtype(array.dtype, np.integer):
        maximum = float(np.iinfo(array.dtype).max)
        return (
            np.ascontiguousarray(array, dtype=np.float32) / maximum,
            np.float32,
        )
    if not np.issubdtype(array.dtype, np.floating):
        raise ProfileError(
            f"expected a float or integer RGB array, got dtype {array.dtype}")
    work = np.float64 if array.dtype == np.float64 else np.float32
    return np.ascontiguousarray(array, dtype=work), work


def _rgb_to_hsv(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vectorised RGB (0..1) to hue in degrees, saturation and value."""
    red = rgb[..., 0]
    green = rgb[..., 1]
    blue = rgb[..., 2]
    value = np.max(rgb, axis=-1)
    minimum = np.min(rgb, axis=-1)
    chroma = value - minimum

    zero = np.zeros_like(value)
    lit = chroma > 0
    red_max = lit & (value == red)
    green_max = lit & (value == green) & ~red_max
    blue_max = lit & ~red_max & ~green_max

    hue = np.zeros_like(value)
    safe = np.where(lit, chroma, 1.0)
    hue = np.where(red_max, ((green - blue) / safe) % 6.0, hue)
    hue = np.where(green_max, (blue - red) / safe + 2.0, hue)
    hue = np.where(blue_max, (red - green) / safe + 4.0, hue)
    hue = hue * 60.0

    saturation = np.where(value > 0, chroma / np.where(value > 0, value, 1.0),
                          zero)
    return hue, saturation, value


def _hsv_to_rgb(hue, saturation, value) -> np.ndarray:
    """Vectorised hue in degrees, saturation and value back to RGB."""
    hue = np.mod(hue, 360.0) / 60.0
    sector = np.floor(hue)
    fraction = hue - sector
    sector = sector.astype(np.int32) % 6

    chroma = value * saturation
    rising = value - chroma * (1.0 - fraction)
    falling = value - chroma * fraction
    base = value - chroma

    red = np.select(
        [sector == 0, sector == 1, sector == 2, sector == 3, sector == 4],
        [value, falling, base, base, rising],
        default=value,
    )
    green = np.select(
        [sector == 0, sector == 1, sector == 2, sector == 3, sector == 4],
        [rising, value, value, falling, base],
        default=base,
    )
    blue = np.select(
        [sector == 0, sector == 1, sector == 2, sector == 3, sector == 4],
        [base, base, rising, value, value],
        default=falling,
    )
    return np.stack((red, green, blue), axis=-1)


# --- Table application ----------------------------------------------------


def _axis_weights(position, divisions: int, wrap: bool):
    """Return (low index, high index, fraction) for one grid axis."""
    if wrap:
        scaled = position * (divisions / 360.0)
        low = np.floor(scaled)
        fraction = (scaled - low).astype(position.dtype, copy=False)
        low = low.astype(np.int64) % divisions
        high = (low + 1) % divisions
        return low, high, fraction
    if divisions < 2:
        zero = np.zeros(position.shape, dtype=np.int64)
        return zero, zero, np.zeros(position.shape, dtype=position.dtype)
    scaled = np.clip(position, 0.0, 1.0) * (divisions - 1)
    low = np.floor(scaled)
    fraction = (scaled - low).astype(position.dtype, copy=False)
    low = np.clip(low.astype(np.int64), 0, divisions - 2)
    return low, low + 1, fraction


def _apply_hsv_grid(rgb, table, dims, label: str) -> np.ndarray:
    """Trilinear interpolation of an HSV delta grid, then apply the deltas.

    ``table`` holds (hue shift in degrees, saturation scale, value scale)
    triples on a (hue, saturation, value) grid. The hue axis wraps — it has no
    repeated endpoint, so the last slice interpolates back into the first —
    while saturation and value clamp at their ends.
    """
    grid = _shape_grid(table, dims, label)
    hue_divisions, sat_divisions, value_divisions = _shape_dims(dims, label)
    source, work = _as_rgb(rgb)
    clipped = np.clip(source, 0.0, 1.0)

    hue, saturation, value = _rgb_to_hsv(clipped)

    hue_low, hue_high, hue_fraction = _axis_weights(hue, hue_divisions, True)
    sat_low, sat_high, sat_fraction = _axis_weights(
        saturation, sat_divisions, False)
    value_low, value_high, value_fraction = _axis_weights(
        value, value_divisions, False)

    flat = grid.reshape(-1, 3).astype(work, copy=False)
    hue_stride = sat_divisions
    value_stride = hue_divisions * sat_divisions

    deltas = np.zeros(clipped.shape, dtype=work)
    for value_index, value_weight in (
            (value_low, 1.0 - value_fraction), (value_high, value_fraction)):
        for hue_index, hue_weight in (
                (hue_low, 1.0 - hue_fraction), (hue_high, hue_fraction)):
            for sat_index, sat_weight in (
                    (sat_low, 1.0 - sat_fraction), (sat_high, sat_fraction)):
                weight = value_weight * hue_weight * sat_weight
                if not np.any(weight):
                    continue
                offset = (
                    value_index * value_stride
                    + hue_index * hue_stride
                    + sat_index)
                deltas += flat[offset] * weight[..., None]

    hue = np.mod(hue + deltas[..., 0], 360.0)
    saturation = np.clip(saturation * deltas[..., 1], 0.0, 1.0)
    value = np.clip(value * deltas[..., 2], 0.0, 1.0)
    return _hsv_to_rgb(hue, saturation, value).astype(work, copy=False)


def apply_hue_sat_map(rgb, table, dims) -> np.ndarray:
    """Apply a ProfileHueSatMap table to RGB in 0..1.

    ``dims`` is (hue divisions, saturation divisions, value divisions) and
    ``table`` is the matching grid of (hue shift in degrees, saturation scale,
    value scale) triples, flat or already shaped. Input outside 0..1 is
    clipped, which is what the DNG reference does at this stage.
    """
    return _apply_hsv_grid(rgb, table, dims, "ProfileHueSatMapData")


def apply_look_table(rgb, table, dims) -> np.ndarray:
    """Apply a ProfileLookTable to RGB in 0..1.

    Identical grid maths to ``apply_hue_sat_map``; the difference in DNG is
    where in the pipeline it lands. See the module docstring for the ordering
    this module uses and why it is approximate.
    """
    return _apply_hsv_grid(rgb, table, dims, "ProfileLookTableData")


def apply_tone_curve(rgb, curve) -> np.ndarray:
    """Apply a profile tone curve, preserving hue as the DNG reference does.

    ``curve`` is the profile's own curve as interleaved x,y pairs in 0..1 (or
    an (n, 2) array). The curve is applied to the brightest and darkest
    channel of each pixel and the middle channel is interpolated between them,
    which holds hue steady instead of the desaturation three independent
    channel curves would cause. A curve with fewer than two points is a
    no-op.
    """
    points = _shape_curve(curve)
    source, work = _as_rgb(rgb)
    if points.shape[0] < 2:
        return source.copy()
    xs = points[:, 0].astype(np.float64)
    ys = points[:, 1].astype(np.float64)

    order = np.argsort(source, axis=-1, kind="stable")
    ordered = np.take_along_axis(source, order, axis=-1)
    low = ordered[..., 0]
    middle = ordered[..., 1]
    high = ordered[..., 2]

    toned_low = np.interp(low, xs, ys).astype(work, copy=False)
    toned_high = np.interp(high, xs, ys).astype(work, copy=False)
    span = high - low
    ratio = np.where(span > 0, (middle - low) / np.where(span > 0, span, 1.0),
                     0.0)
    toned_middle = toned_low + (toned_high - toned_low) * ratio

    toned = np.stack((toned_low, toned_middle, toned_high), axis=-1)
    result = np.empty_like(source)
    np.put_along_axis(result, order, toned.astype(work, copy=False), axis=-1)
    return result


def apply_profile(rgb, profile, *, strength: float = 1.0,
                  temperature: float | None = None) -> np.ndarray:
    """Apply a profile's hue/sat map, look table and tone curve.

    ``profile`` is a dict from ``read_profile`` (a path is accepted too and is
    read on the spot). ``strength`` blends linearly between the input and the
    fully applied result and is clamped to 0..1, so ``strength=0`` returns the
    input unchanged.

    When both ``hueSatMap1`` and ``hueSatMap2`` are present, and ``temperature``
    is provided, dual-illuminant 1/T temperature interpolation is applied.
    Forward and colour matrices are not applied.
    """
    if isinstance(profile, (str, Path)):
        profile = read_profile(profile)
    if not isinstance(profile, dict):
        raise ProfileError(
            "apply_profile needs a dict from read_profile or a path to a "
            f".dcp file, got {type(profile).__name__}")

    source, work = _as_rgb(rgb)
    amount = float(np.clip(float(strength), 0.0, 1.0))
    if amount == 0.0:
        return source.copy()

    result = source
    hue_sat_map = profile.get("hueSatMap1")
    hue_sat_dims = profile.get("hueSatDims")
    hue_sat_map_2 = profile.get("hueSatMap2")
    illuminant1 = profile.get("illuminant1")
    illuminant2 = profile.get("illuminant2")

    if (hue_sat_map is not None and hue_sat_map_2 is not None
            and illuminant1 is not None and illuminant2 is not None
            and temperature is not None):
        hue_sat_map = interpolate_hue_sat_maps(
            hue_sat_map, hue_sat_map_2, illuminant1, illuminant2, temperature)

    if hue_sat_map is not None and hue_sat_dims is not None:
        result = apply_hue_sat_map(result, hue_sat_map, hue_sat_dims)

    look_table = profile.get("lookTable")
    look_dims = profile.get("lookDims")
    if look_table is not None and look_dims is not None:
        result = apply_look_table(result, look_table, look_dims)

    tone_curve = profile.get("toneCurve")
    if tone_curve is not None:
        result = apply_tone_curve(result, tone_curve)

    result = np.asarray(result, dtype=work)
    if amount >= 1.0:
        return result
    return (source + (result - source) * amount).astype(work, copy=False)
