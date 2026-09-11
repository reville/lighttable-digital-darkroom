# SPDX-License-Identifier: GPL-3.0-only
"""Portable preset import/export for LightTable.

The supported editors do not share a colour engine, so this module converts
only controls with a defensible analogue.  Every imported preset carries a
conversion report; camera profiles, masks, LUT payloads, and other proprietary
operations are named as skipped instead of being silently approximated.
"""
from __future__ import annotations

import base64
import html
import io
import json
import math
import re
import uuid
import zipfile
from pathlib import Path

import numpy as np


MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_MEMBER_BYTES = 3 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 250

GRADE_SCALARS = {
    "exposure", "contrast", "highlights", "shadows", "whites", "blacks",
    "temp", "tint", "vibrance", "saturation", "texture", "clarity",
    "dehaze", "vignette", "vignetteSize", "vignetteFeather", "sharpness", "sharpenRadius", "sharpenDetail",
    "sharpenMasking", "luminanceNoise", "colorNoise",
    "chromaticAberrationRedCyan", "chromaticAberrationBlueYellow",
}
SPECIAL_GRADE_KEYS = {"curveL", "curveR", "curveG", "curveB", "hsl"}


def _number(value, default: float | None = None) -> float | None:
    try:
        number = float(str(value).strip().lstrip("+"))
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value: float | None, minimum: float, maximum: float,
           default: float = 0.0) -> float:
    number = default if value is None else value
    return round(max(minimum, min(maximum, number)), 4)


def _safe_name(value, fallback: str = "Imported Preset") -> str:
    name = " ".join(str(value or "").split()).strip()[:120]
    return name or fallback


def _curve_lut(points) -> list[float] | None:
    """Convert 0..255 point pairs to LightTable's 256-entry sampled curve."""
    cleaned = []
    for point in points or []:
        if isinstance(point, dict):
            x, y = _number(point.get("x")), _number(point.get("y"))
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            x, y = _number(point[0]), _number(point[1])
        else:
            continue
        if x is not None and y is not None:
            cleaned.append((_clamp(x, 0, 255), _clamp(y, 0, 255)))
    if len(cleaned) < 2:
        return None
    cleaned.sort(key=lambda item: item[0])
    deduped: dict[float, float] = {}
    for x, y in cleaned:
        deduped[x] = y
    xs = np.asarray(list(deduped), dtype=np.float64)
    ys = np.asarray(list(deduped.values()), dtype=np.float64)
    if len(xs) < 2:
        return None
    return [round(float(value), 6) for value in np.interp(
        np.arange(256, dtype=np.float64), xs, ys) / 255.0]


def _preset(name: str, source: str, grade: dict,
            *, preset_type: str = "style", ignored=None, notes=None) -> dict:
    included = [key for key in grade if key in GRADE_SCALARS | SPECIAL_GRADE_KEYS]
    return {
        "id": str(uuid.uuid4()),
        "name": _safe_name(name),
        "source": source,
        "presetType": "tool" if preset_type == "tool" else "style",
        "includeFilm": False,
        "recommendedFilmOff": True,
        "params": {},
        "grade": grade,
        "includedGrade": included,
        "conversion": {
            "mapped": len(included),
            "ignored": sorted(set(ignored or [])),
            "notes": list(notes or []),
        },
    }


def _xmp_attrs(content: str) -> dict[str, str]:
    return {
        match.group(1): html.unescape(match.group(2))
        for match in re.finditer(r"crs:([A-Za-z0-9]+)\s*=\s*['\"]([^'\"]*)['\"]",
                                 content)
    }


def _xmp_name(content: str, fallback: str) -> str:
    for pattern in (
        r"(?s)<crs:Name>.*?<rdf:li[^>]*>([^<]+)</rdf:li>.*?</crs:Name>",
        r"crs:Name\s*=\s*['\"]([^'\"]+)['\"]",
    ):
        match = re.search(pattern, content)
        if match:
            return _safe_name(html.unescape(match.group(1)), fallback)
    return _safe_name(Path(fallback).stem)


def _xmp_curve(content: str, key: str) -> list[float] | None:
    match = re.search(
        rf"(?s)<crs:{re.escape(key)}>\s*<rdf:Seq>(.*?)</rdf:Seq>\s*</crs:{re.escape(key)}>",
        content,
    )
    if not match:
        return None
    points = [
        (_number(item.group(1)), _number(item.group(2)))
        for item in re.finditer(
            r"<rdf:li[^>]*>\s*(-?[0-9.]+)\s*,\s*(-?[0-9.]+)\s*</rdf:li>",
            match.group(1),
        )
    ]
    return _curve_lut(points)


def _active_xmp_value(value: str | None) -> bool:
    """Return whether an unsupported XMP value represents an active edit."""
    if value is None:
        return False
    clean = str(value).strip()
    if not clean or clean.casefold() in {"false", "no", "none", "off"}:
        return False
    number = _number(clean)
    return abs(number) > 1e-9 if number is not None else True


def _active_xmp_keys(attrs: dict[str, str], keys) -> bool:
    return any(_active_xmp_value(attrs.get(key)) for key in keys)


def _legacy_template_as_xmp(content: str, fallback: str) -> str:
    """Expose legacy Lua-table develop settings through the XMP parser.

    Old desktop presets use a declarative Lua table rather than executable Lua.
    Restrict parsing to scalar, capitalised setting keys and numeric curve
    arrays so importing a preset never evaluates source text.
    """
    embedded = re.search(r'(?s)s\.xmp\s*=\s*"((?:\\.|[^"\\])*)"', content)
    if embedded:
        return embedded.group(1).replace(r'\"', '"').replace(r"\n", "\n")

    def unescape_string(value: str) -> str:
        return value.replace(r'\"', '"').replace(r"\\", "\\")

    title_match = re.search(
        r'(?m)^\s*(?:title|internalName)\s*=\s*"((?:\\.|[^"\\])*)"\s*,?',
        content,
    )
    title = _safe_name(
        unescape_string(title_match.group(1))
        if title_match else Path(fallback).stem,
        Path(fallback).stem,
    )
    attributes = []
    scalar_pattern = re.compile(
        r'(?m)^\s*([A-Z][A-Za-z0-9]+)\s*=\s*'
        r'(?:("(?:\\.|[^"\\])*")|([-+]?(?:\d+(?:\.\d*)?|\.\d+))|'
        r'(true|false))\s*,?'
    )
    for match in scalar_pattern.finditer(content):
        key = match.group(1)
        if match.group(2):
            value = unescape_string(match.group(2)[1:-1])
        else:
            value = match.group(3) or match.group(4)
        attributes.append(f'crs:{key}="{html.escape(value, quote=True)}"')

    curve_xml = []
    for key in ("ToneCurvePV2012", "ToneCurvePV2012Red",
                "ToneCurvePV2012Green", "ToneCurvePV2012Blue"):
        match = re.search(rf'(?s)\b{key}\s*=\s*\{{(.*?)\}}\s*,?', content)
        if not match:
            continue
        numbers = re.findall(r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)', match.group(1))
        points = "".join(
            f"<rdf:li>{numbers[index]}, {numbers[index + 1]}</rdf:li>"
            for index in range(0, len(numbers) - 1, 2)
        )
        if points:
            curve_xml.append(f"<crs:{key}><rdf:Seq>{points}</rdf:Seq></crs:{key}>")
    attrs = " ".join(attributes)
    safe_title = html.escape(title)
    return (f'<rdf:Description {attrs}><crs:Name><rdf:Alt>'
            f'<rdf:li>{safe_title}</rdf:li></rdf:Alt></crs:Name>'
            f'{"".join(curve_xml)}</rdf:Description>')


CRS_CURVE_KEYS = (
    "ToneCurvePV2012", "ToneCurvePV2012Red", "ToneCurvePV2012Green",
    "ToneCurvePV2012Blue", "ToneCurve", "ToneCurveRed", "ToneCurveGreen",
    "ToneCurveBlue",
)


def map_crs_settings(attrs: dict, curves: dict | None = None) -> dict:
    """Map Camera Raw crs settings to LightTable grade values.

    ``attrs`` is the flat ``crs:`` name to string mapping and ``curves``
    maps tone-curve names from :data:`CRS_CURVE_KEYS` to 256-entry LUTs.
    Presets and per-image sidecars share this mapping.

    Returns ``{"grade": {...}, "mapped": int, "ignored": [str],
    "notes": [str]}``.
    """
    attrs = attrs or {}
    curves = curves or {}
    grade: dict = {}
    scale_100 = {
        "Contrast2012": "contrast", "Highlights2012": "highlights",
        "Shadows2012": "shadows", "Whites2012": "whites",
        "Blacks2012": "blacks", "Texture": "texture",
        "Clarity2012": "clarity", "Dehaze": "dehaze",
        "Vibrance": "vibrance", "Saturation": "saturation",
        "SharpenDetail": "sharpenDetail",
        "SharpenEdgeMasking": "sharpenMasking",
        "LuminanceSmoothing": "luminanceNoise",
        "ColorNoiseReduction": "colorNoise",
        "ChromaticAberrationRedCyan": "chromaticAberrationRedCyan",
        "ChromaticAberrationBlueYellow": "chromaticAberrationBlueYellow",
    }
    for source, target in scale_100.items():
        if source in attrs:
            minimum = 0 if target in {"sharpenDetail", "sharpenMasking",
                                      "luminanceNoise", "colorNoise"} else -1
            grade[target] = _clamp((_number(attrs[source]) or 0) / 100.0,
                                   minimum, 1)
    exposure_key = "Exposure2012" if "Exposure2012" in attrs else "Exposure"
    if exposure_key in attrs:
        grade["exposure"] = _clamp(_number(attrs[exposure_key]), -3, 3)
    if "Sharpness" in attrs:
        grade["sharpness"] = _clamp((_number(attrs["Sharpness"]) or 0) / 150.0, 0, 1)
    if "SharpenRadius" in attrs:
        grade["sharpenRadius"] = _clamp(_number(attrs["SharpenRadius"]), .5, 3, 1)
    if "IncrementalTemperature" in attrs:
        grade["temp"] = _clamp(
            (_number(attrs["IncrementalTemperature"]) or 0) / 100.0, -1, 1)
    elif "Temperature" in attrs:
        adjusted = _number(attrs["Temperature"])
        as_shot = _number(attrs.get("AsShotTemperature"), 5500.0)
        if adjusted and as_shot and adjusted > 0 and as_shot > 0:
            mired_delta = 1_000_000.0 / adjusted - 1_000_000.0 / as_shot
            grade["temp"] = _clamp(-mired_delta / 150.0, -1, 1)
    if "IncrementalTint" in attrs:
        grade["tint"] = _clamp(
            (_number(attrs["IncrementalTint"]) or 0) / 100.0, -1, 1)
    elif "Tint" in attrs:
        grade["tint"] = _clamp((_number(attrs["Tint"]) or 0) / 150.0, -1, 1)
    if "PostCropVignetteAmount" in attrs:
        # Lightroom uses negative values for a dark vignette; LightTable uses
        # positive values for the same visual direction.
        grade["vignette"] = _clamp(-(_number(attrs["PostCropVignetteAmount"]) or 0) / 100.0,
                                   -1, 1)

    hsl = {}
    colors = {
        "Red": "red", "Orange": "orange", "Yellow": "yellow",
        "Green": "green", "Aqua": "aqua", "Blue": "blue",
        "Purple": "purple", "Magenta": "magenta",
    }
    for adobe, lighttable in colors.items():
        values = {}
        for prefix, short in (("HueAdjustment", "h"),
                              ("SaturationAdjustment", "s"),
                              ("LuminanceAdjustment", "l")):
            key = prefix + adobe
            if key in attrs:
                values[short] = _clamp((_number(attrs[key]) or 0) / 100.0, -1, 1)
        if any(values.values()):
            hsl[lighttable] = values
    if hsl:
        grade["hsl"] = hsl

    for source, target in (
        ("ToneCurvePV2012", "curveL"),
        ("ToneCurvePV2012Red", "curveR"),
        ("ToneCurvePV2012Green", "curveG"),
        ("ToneCurvePV2012Blue", "curveB"),
    ):
        curve = curves.get(source)
        if curve:
            grade[target] = curve

    # Process-version 1 and 2 presets use the un-suffixed curve fields.
    if "curveL" not in grade:
        curve = curves.get("ToneCurve")
        if curve:
            grade["curveL"] = curve
    for source, target in (
        ("ToneCurveRed", "curveR"),
        ("ToneCurveGreen", "curveG"),
        ("ToneCurveBlue", "curveB"),
    ):
        if target not in grade:
            curve = curves.get(source)
            if curve:
                grade[target] = curve

    known = set(scale_100) | {
        "Exposure", "Exposure2012", "Sharpness", "SharpenRadius",
        "Temperature", "AsShotTemperature", "IncrementalTemperature",
        "Tint", "IncrementalTint", "PostCropVignetteAmount",
    }
    for adobe in colors:
        known.update({f"{prefix}{adobe}" for prefix in (
            "HueAdjustment", "SaturationAdjustment", "LuminanceAdjustment")})
    metadata = {
        "Amount", "Baseline", "CameraModelRestriction", "Cluster",
        "ContactInfo", "Copyright", "HasSettings", "Name", "PresetType",
        "ISO", "ProcessVersion", "ShortName", "SortName", "Stubbed",
        "SupportsAmount", "SupportsColor", "SupportsHighDynamicRange",
        "SupportsMonochrome", "SupportsNormalDynamicRange",
        "SupportsOutputReferred", "SupportsSceneReferred", "UUID", "Version",
        "ToneCurveName", "ToneCurveName2012",
    }
    unsupported_groups = {
        "embedded profile or look": {
            "CameraProfile", "Look", "LookTable", "RGBTable",
        },
        "local masks": {"Masking", "MaskGroupBasedCorrections"},
        "lens profile": {
            "AutoLateralCA", "LensProfileEnable", "LensProfileName",
            "LensProfileSetup",
        },
        "geometry or crop": {
            "CropTop", "CropLeft", "CropBottom", "CropRight", "CropAngle",
            "CropConstrainToWarp", "UprightMode", "PerspectiveUpright",
        },
        "healing edits": {"RetouchAreas", "SpotRemoval"},
        "black and white mixer": {
            "ConvertToGrayscale", *{f"GrayMixer{color}" for color in colors},
        },
        "camera calibration": {
            "ShadowTint", "RedHue", "RedSaturation", "GreenHue",
            "GreenSaturation", "BlueHue", "BlueSaturation",
        },
        "parametric tone curve": {
            "ParametricShadows", "ParametricDarks", "ParametricLights",
            "ParametricHighlights",
        },
        "white balance mode": {"WhiteBalance"},
    }
    ignored = []
    grouped_keys = set()
    for label, keys in unsupported_groups.items():
        grouped_keys.update(keys)
        if _active_xmp_keys(attrs, keys):
            ignored.append(label)

    table_keys = {key for key in attrs if key.startswith("Table_")}
    grouped_keys.update(table_keys)
    if table_keys:
        ignored.append("embedded profile or look")

    grain_keys = {"GrainAmount", "GrainSize", "GrainFrequency"}
    grouped_keys.update(grain_keys)
    if "GrainAmount" in attrs:
        ignored.append("grain effect")

    split_toning_keys = {
        "SplitToningShadowHue", "SplitToningShadowSaturation",
        "SplitToningHighlightHue", "SplitToningHighlightSaturation",
        "SplitToningBalance",
    }
    grouped_keys.update(split_toning_keys)
    split_toning_active = _active_xmp_keys(attrs, {
        "SplitToningShadowSaturation", "SplitToningHighlightSaturation",
    })
    color_grading_keys = {key for key in attrs if key.startswith("ColorGrade")}
    grouped_keys.update(color_grading_keys)
    color_grading_active = _active_xmp_keys(attrs, {
        key for key in color_grading_keys
        if key.endswith(("Sat", "Lum"))
    })
    if split_toning_active or color_grading_active:
        ignored.append("color grading")

    parametric_split_keys = {
        "ParametricShadowSplit", "ParametricMidtoneSplit",
        "ParametricHighlightSplit",
    }
    grouped_keys.update(parametric_split_keys)

    vignette_shape_keys = {
        "PostCropVignetteStyle", "PostCropVignetteMidpoint",
        "PostCropVignetteFeather", "PostCropVignetteRoundness",
        "PostCropVignetteHighlightContrast", "OverrideLookVignette",
    }
    grouped_keys.update(vignette_shape_keys)
    if _active_xmp_value(attrs.get("PostCropVignetteAmount")) \
            and any(key in attrs for key in vignette_shape_keys):
        ignored.append("vignette shape")

    handled = known | metadata | grouped_keys | {
        f"{prefix}{adobe}"
        for adobe in colors
        for prefix in ("HueAdjustment", "SaturationAdjustment",
                       "LuminanceAdjustment")
    }
    ignored.extend(
        key for key, value in attrs.items()
        if key not in handled and _active_xmp_value(value)
    )
    notes = ["Control conversion is approximate because Adobe and LightTable use different render engines."]
    mapped = [key for key in grade if key in GRADE_SCALARS | SPECIAL_GRADE_KEYS]
    return {
        "grade": grade,
        "mapped": len(mapped),
        "ignored": sorted(set(ignored)),
        "notes": notes,
    }


def import_lightroom(content: str, filename: str) -> dict:
    if filename.lower().endswith(".lrtemplate"):
        content = _legacy_template_as_xmp(content, filename)
    attrs = _xmp_attrs(content)
    if not attrs and "<crs:" not in content:
        raise ValueError("not a Lightroom/Camera Raw preset")
    curves = {key: _xmp_curve(content, key) for key in CRS_CURVE_KEYS}
    converted = map_crs_settings(attrs, curves)
    return _preset(_xmp_name(content, filename), "lightroom",
                   converted["grade"], ignored=converted["ignored"],
                   notes=converted["notes"])


def import_capture_one(content: str, filename: str) -> dict:
    entries = {
        html.unescape(match.group(1)): html.unescape(match.group(2))
        for match in re.finditer(
            r"<E\s+[^>]*K\s*=\s*['\"]([^'\"]+)['\"][^>]*V\s*=\s*['\"]([^'\"]*)['\"][^>]*/?>",
            content,
        )
    }
    if not entries:
        raise ValueError("not a Capture One style")
    grade: dict = {}
    if "Exposure" in entries:
        grade["exposure"] = _clamp(_number(entries["Exposure"]), -3, 3)
    scale_100 = {
        "Contrast": "contrast", "Saturation": "saturation",
        "Clarity": "clarity", "Structure": "texture",
        "HdrHighlight": "highlights", "HDRHighlight": "highlights",
        "HdrShadow": "shadows", "HDRShadow": "shadows",
        "HdrWhite": "whites", "HDRWhite": "whites",
        "HdrBlack": "blacks", "HDRBlack": "blacks",
        "NrAmount": "luminanceNoise", "ColorNoiseReduction": "colorNoise",
    }
    for source, target in scale_100.items():
        if source in entries:
            value = (_number(entries[source]) or 0) / 100.0
            if target == "highlights":
                value = -value
            minimum = 0 if target in {"luminanceNoise", "colorNoise"} else -1
            grade[target] = _clamp(value, minimum, 1)
    if "Temperature" in entries:
        grade["temp"] = _clamp((_number(entries["Temperature"]) or 0) / 100.0, -1, 1)
    if "Tint" in entries:
        grade["tint"] = _clamp((_number(entries["Tint"]) or 0) / 100.0, -1, 1)
    if "UsmAmount" in entries:
        # Capture One's conventional starting point is commonly 150; scale
        # that to a moderate LightTable amount while retaining room above it.
        grade["sharpness"] = _clamp((_number(entries["UsmAmount"]) or 0) / 300.0, 0, 1)
    if "UsmRadius" in entries:
        grade["sharpenRadius"] = _clamp(_number(entries["UsmRadius"]), .5, 3, 1)
    if "UsmThreshold" in entries:
        grade["sharpenMasking"] = _clamp((_number(entries["UsmThreshold"]) or 0) / 12.0, 0, 1)
    if "Vignetting" in entries:
        grade["vignette"] = _clamp(-(_number(entries["Vignetting"]) or 0) / 100.0, -1, 1)

    curve_keys = {
        "Curve": "curveL", "CurveRGB": "curveL", "CurveRed": "curveR",
        "CurveGreen": "curveG", "CurveBlue": "curveB",
    }
    for source, target in curve_keys.items():
        if source not in entries:
            continue
        pairs = []
        for token in re.split(r"[;|]", entries[source]):
            nums = re.findall(r"-?[0-9.]+", token)
            if len(nums) >= 2:
                x, y = float(nums[0]), float(nums[1])
                # Some Capture One generations store normalised points.
                if max(abs(x), abs(y)) <= 1.01:
                    x *= 255; y *= 255
                pairs.append((x, y))
        curve = _curve_lut(pairs)
        if curve:
            grade[target] = curve

    known = set(scale_100) | set(curve_keys) | {
        "Name", "UUID", "StyleSource", "Exposure", "Temperature", "Tint",
        "UsmAmount", "UsmRadius", "UsmThreshold", "Vignetting",
    }
    ignored = [key for key in entries if key not in known]
    name = entries.get("Name") or Path(filename).stem
    return _preset(name, "capture-one", grade, ignored=ignored, notes=[
        "Capture One styles use engine-specific ranges; converted controls are approximate.",
    ])


def _decode_upload(upload: dict) -> bytes:
    if "base64" in upload:
        try:
            data = base64.b64decode(str(upload["base64"]), validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("invalid base64 upload") from error
    else:
        data = str(upload.get("text", "")).encode("utf-8")
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("preset file is larger than 25 MB")
    return data


def _import_bytes(filename: str, data: bytes) -> list[dict]:
    lower = filename.lower()
    if lower.endswith((".zip", ".costylepack")):
        out = []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("preset archive contains too many files")
            if sum(item.file_size for item in members) > MAX_ARCHIVE_BYTES:
                raise ValueError("expanded preset archive is larger than 25 MB")
            for item in members:
                if item.file_size > MAX_MEMBER_BYTES:
                    continue
                member_lower = item.filename.lower()
                if not member_lower.endswith((".xmp", ".lrtemplate", ".costyle",
                                               ".ltpreset", ".json")):
                    continue
                out.extend(_import_bytes(item.filename, archive.read(item)))
        if not out:
            raise ValueError("no supported presets found in archive")
        return out
    if len(data) > MAX_MEMBER_BYTES:
        raise ValueError("individual preset is larger than 3 MB")
    content = data.decode("utf-8-sig", errors="replace")
    if lower.endswith((".xmp", ".lrtemplate")):
        return [import_lightroom(content, filename)]
    if lower.endswith(".costyle"):
        return [import_capture_one(content, filename)]
    if lower.endswith((".json", ".ltpreset")):
        value = json.loads(content)
        if isinstance(value, dict) and value.get("format") == "LightTable Preset":
            from preset_library import validate_look
            file_version = value.get("version", 1)
            if type(file_version) is not int or file_version not in (1, 2, 3):
                raise ValueError("Update LightTable to import this preset format")
            presets = value.get("presets", [])
            if not isinstance(presets, list) or len(presets) > 250:
                raise ValueError("Invalid preset collection")
            if file_version == 3:
                for item in presets:
                    validate_look(item)
            imported = []
            for item in presets:
                if not isinstance(item, dict):
                    continue
                local = dict(item, id=str(uuid.uuid4()))
                origin = local.pop("community", None)
                if isinstance(origin, dict) and origin.get("id"):
                    local["parentId"] = origin["id"]
                local.pop("collection", None)
                imported.append(local)
            return imported
        raise ValueError("not a LightTable preset")
    raise ValueError("unsupported preset type")


def import_uploads(uploads) -> tuple[list[dict], list[dict]]:
    presets, failures = [], []
    for upload in uploads or []:
        filename = _safe_name((upload or {}).get("name"), "preset")
        try:
            presets.extend(_import_bytes(filename, _decode_upload(upload or {})))
        except Exception as error:  # noqa: BLE001 - one bad file must not block a batch
            failures.append({"file": filename, "error": str(error)[:240]})
    return presets, failures


def _curve_points(lut) -> list[dict]:
    if not isinstance(lut, list) or len(lut) != 256:
        return [{"x": 0, "y": 0}, {"x": 255, "y": 255}]
    indices = [0, 32, 64, 96, 128, 160, 192, 224, 255]
    return [{"x": index, "y": round(_clamp(_number(lut[index]), 0, 1) * 255, 3)}
            for index in indices]


def _xmp_attribute_lines(preset: dict) -> list[str]:
    grade = preset.get("grade") or {}
    included = set(preset.get("includedGrade") or grade)
    values = {"ProcessVersion": "15.4"}
    if "exposure" in included:
        values["Exposure2012"] = f"{float(grade.get('exposure', 0)):+.2f}"
    scale_100 = {
        "contrast": "Contrast2012", "highlights": "Highlights2012",
        "shadows": "Shadows2012", "whites": "Whites2012", "blacks": "Blacks2012",
        "texture": "Texture", "clarity": "Clarity2012", "dehaze": "Dehaze",
        "vibrance": "Vibrance", "saturation": "Saturation",
        "sharpenDetail": "SharpenDetail", "sharpenMasking": "SharpenEdgeMasking",
        "luminanceNoise": "LuminanceSmoothing", "colorNoise": "ColorNoiseReduction",
        "chromaticAberrationRedCyan": "ChromaticAberrationRedCyan",
        "chromaticAberrationBlueYellow": "ChromaticAberrationBlueYellow",
    }
    for source, target in scale_100.items():
        if source in included:
            values[target] = str(round(float(grade.get(source, 0)) * 100, 2))
    if "sharpness" in included:
        values["Sharpness"] = str(round(float(grade.get("sharpness", 0)) * 150, 2))
    if "sharpenRadius" in included:
        values["SharpenRadius"] = str(grade.get("sharpenRadius", 1))
    if "tint" in included:
        values["Tint"] = str(round(float(grade.get("tint", 0)) * 150, 2))
    if "vignette" in included:
        values["PostCropVignetteAmount"] = str(round(-float(grade.get("vignette", 0)) * 100, 2))
    if "hsl" in included:
        names = {"red": "Red", "orange": "Orange", "yellow": "Yellow",
                 "green": "Green", "aqua": "Aqua", "blue": "Blue",
                 "purple": "Purple", "magenta": "Magenta"}
        for band, entry in (grade.get("hsl") or {}).items():
            if band not in names:
                continue
            for prefix, short in (("HueAdjustment", "h"),
                                  ("SaturationAdjustment", "s"),
                                  ("LuminanceAdjustment", "l")):
                values[prefix + names[band]] = str(round(float(entry.get(short, 0)) * 100, 2))
    return [f'   crs:{key}="{html.escape(str(value), quote=True)}"'
            for key, value in values.items()]


def export_lightroom(preset: dict) -> tuple[str, str, str]:
    grade = preset.get("grade") or {}
    included = set(preset.get("includedGrade") or grade)
    attrs = "\n".join(_xmp_attribute_lines(preset))
    curves = []
    for source, target in (("curveL", "ToneCurvePV2012"),
                           ("curveR", "ToneCurvePV2012Red"),
                           ("curveG", "ToneCurvePV2012Green"),
                           ("curveB", "ToneCurvePV2012Blue")):
        if source not in included:
            continue
        points = _curve_points(grade.get(source))
        rows = "\n".join(
            f"      <rdf:li>{round(point['x'])}, {round(point['y'])}</rdf:li>"
            for point in points)
        curves.append(f"""   <crs:{target}><rdf:Seq>
{rows}
   </rdf:Seq></crs:{target}>""")
    name = html.escape(_safe_name(preset.get("name")))
    content = f"""<?xpacket begin='' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/'>
 <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
  <rdf:Description rdf:about='' xmlns:crs='http://ns.adobe.com/camera-raw-settings/1.0/'
{attrs}>
   <crs:Name><rdf:Alt><rdf:li xml:lang='x-default'>{name}</rdf:li></rdf:Alt></crs:Name>
{chr(10).join(curves)}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>
"""
    return (f"{_safe_filename(preset.get('name'))}.xmp", "application/rdf+xml", content)


def export_capture_one(preset: dict) -> tuple[str, str, str]:
    grade = preset.get("grade") or {}
    included = set(preset.get("includedGrade") or grade)
    entries = {"Name": _safe_name(preset.get("name")), "StyleSource": "Styles",
               "UUID": str(uuid.uuid4()).upper()}
    if "exposure" in included:
        entries["Exposure"] = grade.get("exposure", 0)
    scale_100 = {"contrast": "Contrast", "saturation": "Saturation",
                 "clarity": "Clarity", "texture": "Structure",
                 "shadows": "HdrShadow", "whites": "HdrWhite", "blacks": "HdrBlack",
                 "luminanceNoise": "NrAmount", "colorNoise": "ColorNoiseReduction",
                 "temp": "Temperature", "tint": "Tint"}
    for source, target in scale_100.items():
        if source in included:
            entries[target] = round(float(grade.get(source, 0)) * 100, 3)
    if "highlights" in included:
        entries["HdrHighlight"] = round(-float(grade.get("highlights", 0)) * 100, 3)
    if "sharpness" in included:
        entries["UsmAmount"] = round(float(grade.get("sharpness", 0)) * 300, 3)
    if "sharpenRadius" in included:
        entries["UsmRadius"] = grade.get("sharpenRadius", 1)
    if "sharpenMasking" in included:
        entries["UsmThreshold"] = round(float(grade.get("sharpenMasking", 0)) * 12, 3)
    if "vignette" in included:
        entries["Vignetting"] = round(-float(grade.get("vignette", 0)) * 100, 3)
    rows = "\n".join(
        f' <E K="{html.escape(str(key), quote=True)}" V="{html.escape(str(value), quote=True)}" />'
        for key, value in entries.items())
    content = f'<?xml version="1.0"?>\n<SL Engine="1600">\n{rows}\n</SL>\n'
    return (f"{_safe_filename(preset.get('name'))}.costyle", "application/xml", content)


def _safe_filename(value) -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", _safe_name(value))
    return name.strip(" .")[:100] or "LightTable Preset"


def export_preset(preset: dict, format_name: str) -> tuple[str, str, str]:
    if format_name == "lightroom":
        return export_lightroom(preset)
    if format_name == "capture-one":
        return export_capture_one(preset)
    content = json.dumps({
        "format": "LightTable Preset", "version": 3 if preset.get("scope") == "look" else 2, "presets": [preset],
    }, indent=2)
    return (f"{_safe_filename(preset.get('name'))}.ltpreset",
            "application/json", content)
