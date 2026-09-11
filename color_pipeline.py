# SPDX-License-Identifier: GPL-3.0-only
"""Colour-managed image helpers shared by preview input and export.

The film simulation and interactive grade produce display-referred sRGB.
Unadjusted Develop exports can retain wider source colors until final encode.
Exports stay floating point until the final encoder, then receive the ICC
profile that describes the selected output colour space.
"""
from __future__ import annotations

from server_localization import T

import io
import os
import subprocess
import sys
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image, ImageOps

import platform_image
from raw_decode_cache import DecodedRawCache, source_identity

# Leave CPU capacity for the second renderer and the UI during RAW decode.
os.environ.setdefault("OMP_NUM_THREADS", "8")

# RGB16 preserves LibRaw output exactly and uses half the space of renderer
# float32 inputs. The shared budget is independent of the two engine caches.
RAW_DEMOSAIC_CACHE = DecodedRawCache(int(os.environ.get(
    "LIGHTTABLE_RAW_DECODE_CACHE_BYTES", str(512 * 1024 * 1024))))


ICC_PROFILES = {
    space: platform_image.profile_path(Path(__file__).resolve().parent, space)
    for space in ("srgb", "display_p3", "prophoto")
}

COLOUR_SPACE_NAMES = {
    "srgb": "sRGB",
    "display_p3": "Display P3",
    "prophoto": "ProPhoto RGB",
}

SRGB_LIMITED_EXPORT_WARNING = (
    "This export uses sRGB-limited colors: film rendering and adjusted "
    "Develop images do not yet retain colors outside sRGB. "
    "The requested output profile is embedded.")


def srgb_limited_export_warning() -> str:
    return T("This export uses sRGB-limited colors: film rendering and adjusted "
             "Develop images do not yet retain colors outside sRGB. "
             "The requested output profile is embedded.")

RAW_WB_MODES = {
    "as_shot", "auto", "daylight", "cloudy", "shade", "tungsten", "fluorescent", "flash", "custom"
}
RAW_WB_PRESETS = {
    "daylight": (5500.0, 0.10),
    "cloudy": (6500.0, 0.10),
    "shade": (7500.0, 0.10),
    "tungsten": (2850.0, 0.0),
    "fluorescent": (3800.0, 0.10),
    "flash": (5500.0, 0.0),
}
RAW_PROFILES = {"camera", "detail", "smooth"}
RAW_HIGHLIGHT_MODES = {"off", "blend", "reconstruct"}
RAW_DENOISE_MODES = {"off", "light", "full"}

# The Film-off (Develop) rendering intent. "linear" is the original neutral
# render: a plain sRGB encode of the scene-linear decode. "standard" adds the
# fixed base curve below before that encode, which is what a newcomer expects
# a RAW converter to show. "soft" adds an extended filmic shoulder that rolls
# off highlights gently in high-contrast scenes. Anyone matching a scan selects "linear".
DEVELOP_PROFILES = {"linear", "standard", "soft"}
DEFAULT_DEVELOP_PROFILE = "standard"

# Base-curve design constants. The shape is specified on the sRGB code-value
# axis rather than in linear light because that is the axis a tone curve is
# read on, but the table it produces is scene-linear in and scene-linear out
# so it can be applied before the encode, where a camera profile belongs.
STANDARD_CURVE_SIZE = 256
# Slope of the curve at middle grey, in code values per code value: a 25%
# mid-tone contrast increase. Also the toe and shoulder exponent, so the
# curve is C1-continuous through the pivot.
STANDARD_CURVE_CONTRAST = 1.25
SOFT_CURVE_CONTRAST = 1.12
MIDDLE_GREY_LINEAR = 0.18
# Middle grey is a fixed point of the curve, so an 18% grey card still
# reproduces at the sRGB code value that encodes 0.18 linear: 0.4614, or
# 117.6/255. That is Zone V, the nominal print value of a grey card, and
# keeping it fixed means switching profiles changes contrast, not exposure.
MIDDLE_GREY_DISPLAY = 0.4614

# Linear sRGB (D65) to linear ProPhoto RGB (D50), including Bradford
# chromatic adaptation. This keeps the provisional embedded RAW preview on
# the same scene-linear input convention as the accurate demosaic that
# replaces it, without importing the much heavier colour-science stack.
_LINEAR_SRGB_TO_PROPHOTO = np.asarray([
    [0.5293884707, 0.3302312177, 0.1406271678],
    [0.0983050885, 0.8734839012, 0.0281460561],
    [0.0168510638, 0.1176968437, 0.8656732686],
], dtype=np.float32)


def normalise_output_space(output_space: str | None) -> str:
    value = str(output_space or "srgb").lower()
    return value if value in COLOUR_SPACE_NAMES else "srgb"


def as_float_rgb(image: np.ndarray) -> np.ndarray:
    """Normalise an RGB integer/float array to float32 in 0..1."""
    array = np.asarray(image)
    if array.ndim != 3 or array.shape[2] < 3:
        raise ValueError(T("expected an HxWx3 RGB image"))
    array = array[..., :3]
    if np.issubdtype(array.dtype, np.integer):
        maximum = float(np.iinfo(array.dtype).max)
        array = array.astype(np.float32) / maximum
    else:
        array = array.astype(np.float32)
    return np.clip(array, 0.0, 1.0)


def _srgb_encode(value: np.ndarray) -> np.ndarray:
    """sRGB opto-electronic transfer function (IEC 61966-2-1)."""
    value = np.asarray(value, dtype=np.float64)
    return np.where(
        value <= 0.0031308, value * 12.92,
        1.055 * np.power(np.maximum(value, 0.0), 1.0 / 2.4) - 0.055)


def _srgb_decode(value: np.ndarray) -> np.ndarray:
    """Inverse of :func:`_srgb_encode`."""
    value = np.asarray(value, dtype=np.float64)
    return np.where(value <= 0.04045, value / 12.92,
                    np.power((np.maximum(value, 0.0) + 0.055) / 1.055, 2.4))


def standard_base_curve_domain() -> np.ndarray:
    """Scene-linear input positions of the standard base-curve table.

    The domain covers the whole 0..1 scene-linear range that ``as_float_rgb``
    produces, but its 256 samples are spaced uniformly in *sRGB code value*
    rather than uniformly in linear light: entry ``i`` describes the input
    ``srgb_decode(i / 255)``. A uniform linear grid would spend one interval
    on everything below 0.4% and interpolate the whole toe as a straight
    line; this spacing keeps the interpolation error under 0.03/255 code
    values everywhere, including the deepest shadows.
    """
    code = np.arange(STANDARD_CURVE_SIZE, dtype=np.float64) / (
        STANDARD_CURVE_SIZE - 1)
    return _srgb_decode(code).astype(np.float32)


def standard_base_curve() -> np.ndarray:
    """The fixed Standard develop curve as 256 scene-linear output values.

    Entry ``i`` is the scene-linear value that
    ``standard_base_curve_domain()[i]`` renders to, before the sRGB encode.

    The shape is a pair of powers meeting at middle grey on the code-value
    axis, which gives all three parts the roadmap asks for from one
    parameter: a gentle toe (the slope falls away below roughly 10% code
    value), ``STANDARD_CURVE_CONTRAST`` mid-tone slope through the pivot, and
    a filmic shoulder whose slope decays to zero as it approaches white, so
    highlights compress into 1.0 instead of clipping against it. Black and
    white are exact fixed points, so the 99.5th-percentile normalisation that
    follows behaves the same under either profile.
    """
    code = np.arange(STANDARD_CURVE_SIZE, dtype=np.float64) / (
        STANDARD_CURVE_SIZE - 1)
    pivot = float(_srgb_encode(MIDDLE_GREY_LINEAR))
    contrast = STANDARD_CURVE_CONTRAST
    toe = pivot * np.power(code / pivot, contrast)
    shoulder = 1.0 - (1.0 - pivot) * np.power(
        (1.0 - code) / (1.0 - pivot), contrast)
    shaped = np.where(code <= pivot, toe, shoulder)
    return np.clip(_srgb_decode(shaped), 0.0, 1.0).astype(np.float32)


def soft_base_curve() -> np.ndarray:
    """The Soft develop curve with an extended filmic shoulder as 256 output values.

    Shares the middle-grey fixed point and domain with ``standard_base_curve()``,
    but applies a gentler contrast exponent (1.12) to provide softer highlight
    roll-off and preserve cloud/sky detail in high-dynamic-range scenes.
    """
    code = np.arange(STANDARD_CURVE_SIZE, dtype=np.float64) / (
        STANDARD_CURVE_SIZE - 1)
    pivot = float(_srgb_encode(MIDDLE_GREY_LINEAR))
    contrast = SOFT_CURVE_CONTRAST
    toe = pivot * np.power(code / pivot, contrast)
    shoulder = 1.0 - (1.0 - pivot) * np.power(
        (1.0 - code) / (1.0 - pivot), contrast)
    shaped = np.where(code <= pivot, toe, shoulder)
    return np.clip(_srgb_decode(shaped), 0.0, 1.0).astype(np.float32)


def _normalise_develop_profile(value: object) -> str:
    value = str(value or DEFAULT_DEVELOP_PROFILE)
    return value if value in DEVELOP_PROFILES else DEFAULT_DEVELOP_PROFILE


def develop_profile_for(params: dict | None) -> str:
    """The Film-off develop profile a params dict selects.

    ``developProfile`` is the key ``film_pipeline.clean_params`` emits; the
    snake_case spelling is accepted for the same reason
    ``enhance_workflow.denoise_fingerprint`` accepts both, so a caller that
    has not been through ``clean_params`` cannot silently select the default.
    """
    params = params or {}
    return _normalise_develop_profile(
        params.get("developProfile", params.get("develop_profile")))


def raw_decode_fingerprint(params: dict | None) -> str:
    """Stable cache identity for decisions made before film exposure."""
    params = params or {}
    mode = str(params.get("wb_mode", "as_shot"))
    if mode not in RAW_WB_MODES:
        mode = "as_shot"
    try:
        temperature = round(float(params.get("wb_temperature", 5500.0)), 1)
        if not np.isfinite(temperature):
            temperature = 5500.0
    except (TypeError, ValueError):
        temperature = 5500.0
    try:
        tint = round(float(params.get("wb_tint", 0.0)), 3)
        if not np.isfinite(tint):
            tint = 0.0
    except (TypeError, ValueError):
        tint = 0.0
    profile = str(params.get("raw_profile", "camera"))
    if profile not in RAW_PROFILES:
        profile = "camera"
    recovery = str(params.get("raw_highlight_recovery", "reconstruct"))
    if recovery not in RAW_HIGHLIGHT_MODES:
        recovery = "reconstruct"
    denoise = str(params.get("raw_sensor_denoise", "off"))
    if denoise not in RAW_DENOISE_MODES:
        denoise = "off"
    develop = develop_profile_for(params)
    # Imported lazily because enhance_workflow uses this module for its tile
    # exchange. The model identity must be part of the neutral decode cache.
    import enhance_workflow
    learned = enhance_workflow.denoise_fingerprint(params)
    import hashlib
    return hashlib.sha256(
        (f"linear-prophoto-v5|{profile}|{recovery}|{denoise}|{learned}|"
         f"{mode}|{temperature}|{tint}|{develop}").encode()
    ).hexdigest()[:12]


def raw_postprocess_options(params: dict | None = None,
                            *, half_size: bool = False) -> dict:
    """Build rawpy options for capture-stage quality decisions.

    The camera profile uses LibRaw's camera-specific default demosaic and
    embedded colour matrices. Detail selects DCB with enhancement; Smooth
    selects AHD with a single mosaic median pass. FBDD noise reduction is
    intentionally applied here, before demosaic, rather than by the later
    display-referred noise controls.
    """
    import rawpy

    params = params or {}
    profile = str(params.get("raw_profile", "camera"))
    recovery = str(params.get("raw_highlight_recovery", "reconstruct"))
    denoise = str(params.get("raw_sensor_denoise", "off"))
    options = {
        "gamma": (1, 1),
        "no_auto_bright": True,
        "output_bps": 16,
        "output_color": rawpy.ColorSpace.ProPhoto,
        "half_size": bool(half_size),
        "highlight_mode": {
            "off": rawpy.HighlightMode.Clip,
            "blend": rawpy.HighlightMode.Blend,
            "reconstruct": rawpy.HighlightMode.ReconstructDefault,
        }.get(recovery, rawpy.HighlightMode.ReconstructDefault),
        "fbdd_noise_reduction": {
            "off": rawpy.FBDDNoiseReductionMode.Off,
            "light": rawpy.FBDDNoiseReductionMode.Light,
            "full": rawpy.FBDDNoiseReductionMode.Full,
        }.get(denoise, rawpy.FBDDNoiseReductionMode.Off),
    }
    if profile == "detail":
        options.update(demosaic_algorithm=rawpy.DemosaicAlgorithm.DCB,
                       dcb_iterations=2, dcb_enhance=True)
    elif profile == "smooth":
        options.update(demosaic_algorithm=rawpy.DemosaicAlgorithm.AHD,
                       median_filter_passes=1)
    return options


def _temperature_xy(temperature: float) -> np.ndarray:
    """Return a daylight/Planckian chromaticity for 2000..50000 K."""
    import warnings
    import colour
    temperature = float(np.clip(temperature, 2000.0, 50000.0))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=colour.utilities.ColourUsageWarning)
        return np.asarray(colour.CCT_to_xy(temperature, method="Kang 2002"),
                          dtype=np.float64)


def apply_custom_raw_white_balance(image: np.ndarray, temperature: float,
                                   tint: float = 0.0) -> np.ndarray:
    """Apply a custom illuminant correction in linear ProPhoto RGB.

    RAW data is first developed with the camera's daylight multipliers. The
    selected illuminant is then neutralised before the film model sees it.
    This is deliberately a capture-stage transform, unlike the creative
    display-referred Temp/Tint controls in the grade.
    """
    import colour

    xy = _temperature_xy(temperature)
    source_xyz = colour.xy_to_XYZ(xy)
    prophoto = colour.RGB_COLOURSPACES["ProPhoto RGB"]
    source_rgb = np.asarray(source_xyz) @ prophoto.matrix_XYZ_to_RGB.T
    gains = 1.0 / np.maximum(source_rgb, 1e-5)
    gains /= gains[1]
    tint = float(np.clip(tint, -1.0, 1.0))
    gains *= np.array([1.0 + tint * 0.05,
                       1.0 - tint * 0.10,
                       1.0 + tint * 0.05])
    return np.clip(np.asarray(image, dtype=np.float32) * gains,
                   0.0, 1.0).astype(np.float32)


def decode_raw(path: Path | str, params: dict | None = None,
               *, half_size: bool = False,
               max_width: int | None = None, learned_denoise_runner=None,
               learned_denoise_status=None,
               learned_denoise_cancel=None,
               apply_learned_denoise: bool = True) -> np.ndarray:
    """Decode RAW to 16-bit scene-linear ProPhoto with pre-film WB.

    SCUNet was trained on gamma-encoded RGB, while this stage is linear
    ProPhoto. We therefore apply only the sRGB transfer function before the
    model (the ProPhoto primaries are deliberately untouched), then decode the
    transfer function afterwards. This is an explicit approximation, not a
    claim that the working pixels are sRGB. A smooth field of block-averaged
    source residuals then restores low-frequency scene colour without
    restoring high-frequency sensor noise.
    """
    import raw_decode_runtime

    params = params or {}
    mode = str(params.get("wb_mode", "as_shot"))
    if mode not in RAW_WB_MODES:
        mode = "as_shot"
    # Capture identity before opening: an atomic replacement must never put
    # pixels from an old open handle under the replacement's cache identity.
    identity = source_identity(path)
    with raw_decode_runtime.open_raw(path) as (raw, decoder):
        sensor_width = int(getattr(raw.sizes, "width", 0) or 0)
        preview_half_size = bool(
            max_width and sensor_width and int(max_width) * 2 <= sensor_width)
        kwargs = raw_decode_runtime.native_options(raw_postprocess_options(
            params, half_size=half_size or preview_half_size), decoder)
        if mode == "as_shot":
            kwargs["use_camera_wb"] = True
        elif mode == "auto":
            kwargs["use_auto_wb"] = True
        else:
            kwargs["user_wb"] = list(raw.daylight_whitebalance)
        # Key only the actual LibRaw decisions. Custom temperature/tint,
        # Develop curves and learned-denoise strength act on these pixels
        # afterwards and therefore do not require another demosaic.
        option_key = tuple(sorted((key, repr(getattr(value, "value", value)))
                                  for key, value in kwargs.items()))
        key = (identity, option_key) if identity is not None else None

        def demosaic():
            with raw_decode_runtime.decode_slot(), raw_decode_runtime.interruptible(raw, decoder):
                try:
                    return raw.postprocess(**kwargs)
                except decoder.LibRawError:
                    raw_decode_runtime.check_cancel()
                    # Unsupported sensor algorithms retain the established
                    # camera fallback without retrying a cancelled decode.
                    fallback = dict(kwargs)
                    for key in ("demosaic_algorithm", "dcb_iterations", "dcb_enhance",
                                "median_filter_passes", "fbdd_noise_reduction"):
                        fallback.pop(key, None)
                    return raw.postprocess(**fallback)

        rgb = RAW_DEMOSAIC_CACHE.get_or_build(
            key, demosaic, raw_decode_runtime.check_cancel)
    if mode in RAW_WB_PRESETS and mode != "daylight":
        temperature, tint = RAW_WB_PRESETS[mode]
        balanced = apply_custom_raw_white_balance(
            rgb.astype(np.float32) / 65535.0, temperature, tint)
        rgb = (balanced * 65535.0 + 0.5).astype(np.uint16)
    elif mode == "custom":
        temperature = float(params.get("wb_temperature", 5500.0))
        tint = float(params.get("wb_tint", 0.0))
        balanced = apply_custom_raw_white_balance(
            rgb.astype(np.float32) / 65535.0, temperature, tint)
        rgb = (balanced * 65535.0 + 0.5).astype(np.uint16)
    learned = bool(params.get("learned_denoise", params.get(
        "learnedDenoise", False)))
    if apply_learned_denoise and learned:
        import enhance_workflow
        strength = params.get("learned_denoise_strength", params.get(
            "learnedDenoiseStrength", 0.6))
        try:
            cleaned = enhance_workflow.denoise_linear_prophoto(
                rgb.astype(np.float32) / 65535.0, strength=float(strength),
                runner=learned_denoise_runner,
                status=learned_denoise_status,
                cancel=lambda: raw_decode_runtime.is_cancelled() or
                    enhance_workflow._cancel_requested(learned_denoise_cancel))
        except Exception:
            raw_decode_runtime.check_cancel()
            raise
        rgb = (cleaned * 65535.0 + 0.5).astype(np.uint16)
    raw_decode_runtime.check_cancel()
    return rgb


def linear_prophoto_to_display_srgb(
        image: np.ndarray, params: dict | None = None,
        *, develop_profile: str | None = None) -> np.ndarray:
    """Render the existing bounded sRGB Develop preview, unchanged."""
    return linear_prophoto_to_display(
        image, params, develop_profile=develop_profile, output_space="srgb")


def linear_prophoto_to_display(
        image: np.ndarray, params: dict | None = None,
        *, develop_profile: str | None = None,
        output_space: str = "srgb") -> np.ndarray:
    """Convert scene-linear ProPhoto to an encoded Develop rendering.

    ``params`` is the same cleaned parameter dict the decode already takes;
    only its ``developProfile`` is read here. ``develop_profile`` names the
    profile directly and wins when both are given. Both are optional and
    default to :data:`DEFAULT_DEVELOP_PROFILE`, so existing callers keep
    working.

    Under ``standard`` the base curve is applied to the scene-linear input,
    before the gamut conversion and the sRGB encode, which is where a camera
    profile's tone curve belongs and matches how the wide-gamut working space
    keeps per-channel curves from shifting hue. ``linear`` skips it entirely
    and is byte-for-byte the render this function produced before the profile
    existed. A wider output retains the same extended sRGB tone rendering,
    converting to the requested gamut before its first gamut clip.
    """
    import colour

    profile = (_normalise_develop_profile(develop_profile)
               if develop_profile is not None else develop_profile_for(params))
    linear = as_float_rgb(image)
    if profile == "standard":
        linear = np.interp(linear, standard_base_curve_domain(),
                           standard_base_curve()).astype(np.float32)
    elif profile == "soft":
        linear = np.interp(linear, standard_base_curve_domain(),
                           soft_base_curve()).astype(np.float32)
    prophoto = colour.RGB_COLOURSPACES["ProPhoto RGB"]
    srgb = colour.RGB_COLOURSPACES["sRGB"]
    encoded = colour.RGB_to_RGB(
        linear, prophoto, srgb,
        chromatic_adaptation_transform="Bradford",
        apply_cctf_decoding=False, apply_cctf_encoding=True,
    )
    # Match the useful brightness of a conventional neutral RAW conversion
    # without applying a camera look or throwing away reconstructed channels.
    # Both profiles normalise identically: the base curve fixes white at 1.0,
    # so the percentile it measures barely moves and the two profiles differ
    # in contrast rather than in overall exposure.
    white = float(np.percentile(np.maximum(encoded, 0.0), 99.5))
    if np.isfinite(white) and white > 0:
        encoded = encoded * min(4.0, 0.96 / white)
    # Keep the same tone/exposure rendering as the sRGB preview. Convert its
    # extended (unclipped) code values before bounding to the delivered gamut;
    # clipping here first would irreversibly discard RAW colors outside sRGB.
    return convert_output_space(encoded, output_space)


def decode_raw_display(path: Path | str, params: dict | None = None,
                       **decode_options) -> np.ndarray:
    """Decode RAW with the selected capture settings to encoded 16-bit sRGB."""
    linear = decode_raw(path, params, **decode_options)
    display = linear_prophoto_to_display_srgb(linear, params)
    return (display * 65535.0 + 0.5).astype(np.uint16)


_EMBEDDED_PREVIEWS: OrderedDict[tuple, np.ndarray] = OrderedDict()
_EMBEDDED_PREVIEW_LOCK = threading.Lock()
_EMBEDDED_PREVIEW_BYTES = 32 * 1024 * 1024


def raw_embedded_preview(path: Path | str, max_width: int) -> np.ndarray:
    """Decode a bounded camera preview once for film input and Before/Match."""
    import rawpy

    path = Path(path)
    max_width = max(1, int(max_width))
    # Before uses up to 1600px. Share that draft with smaller interactive inputs
    # instead of reopening the RAW (and JPEG) for the Match factor.
    decode_width = max(1600, max_width)
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, decode_width)
    with _EMBEDDED_PREVIEW_LOCK:
        pixels = _EMBEDDED_PREVIEWS.get(key)
        if pixels is None:
            with rawpy.imread(str(path)) as raw:
                thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                with Image.open(io.BytesIO(thumb.data)) as opened:
                    orientation = opened.getexif().get(274, 1)
                    oriented_width = opened.height if orientation in (5, 6, 7, 8) else opened.width
                    ratio = min(1.0, decode_width / max(1, oriented_width))
                    opened.draft("RGB", (max(1, round(opened.width * ratio)),
                                         max(1, round(opened.height * ratio))))
                    image = ImageOps.exif_transpose(opened).convert("RGB")
            else:
                image = Image.fromarray(thumb.data, "RGB")
            if image.width > decode_width:
                image = image.resize((decode_width, max(1, round(
                    image.height * decode_width / image.width))), Image.Resampling.LANCZOS)
            pixels = np.array(image, dtype=np.uint8)
            pixels.flags.writeable = False
            if pixels.nbytes <= _EMBEDDED_PREVIEW_BYTES:
                _EMBEDDED_PREVIEWS[key] = pixels
                while sum(value.nbytes for value in _EMBEDDED_PREVIEWS.values()) > _EMBEDDED_PREVIEW_BYTES:
                    _EMBEDDED_PREVIEWS.popitem(last=False)
        else:
            _EMBEDDED_PREVIEWS.move_to_end(key)
    if pixels.shape[1] > max_width:
        image = Image.fromarray(pixels).resize((max_width, max(1, round(
            pixels.shape[0] * max_width / pixels.shape[1]))), Image.Resampling.LANCZOS)
        return np.asarray(image, dtype=np.uint8)
    return pixels


def raw_embedded_thumbnail(path: Path | str, destination: Path,
                           max_pixel: int = 240, quality: int = 80) -> bool:
    """Extract and save an oriented thumbnail directly from the embedded RAW preview."""
    import rawpy

    with rawpy.imread(str(path)) as raw:
        thumb = raw.extract_thumb()
    if thumb.format == rawpy.ThumbFormat.JPEG:
        with Image.open(io.BytesIO(thumb.data)) as opened:
            opened.draft("RGB", (max_pixel, max_pixel))
            image = ImageOps.exif_transpose(opened).convert("RGB")
            image.thumbnail((max_pixel, max_pixel), Image.Resampling.BILINEAR)
            image.save(destination, "JPEG", quality=quality, subsampling=1)
            return True
    elif thumb.format == rawpy.ThumbFormat.BITMAP:
        image = Image.fromarray(thumb.data, "RGB")
        image.thumbnail((max_pixel, max_pixel), Image.Resampling.BILINEAR)
        image.save(destination, "JPEG", quality=quality, subsampling=1)
        return True
    return False


def decode_raw_draft(path: Path | str, params: dict | None,
                     max_width: int) -> np.ndarray:
    """Turn the embedded sRGB preview into provisional linear ProPhoto RGB."""
    try:
        preview = raw_embedded_preview(path, max_width)
    except Exception:  # noqa: BLE001 - DNGs commonly omit embedded previews
        return decode_raw(path, params, half_size=True,
                          apply_learned_denoise=False)
    encoded = preview.astype(np.float32) / 255.0
    linear = np.where(
        encoded <= 0.04045,
        encoded / 12.92,
        ((encoded + 0.055) / 1.055) ** 2.4,
    )
    prophoto = np.clip(linear @ _LINEAR_SRGB_TO_PROPHOTO.T, 0.0, 1.0)
    mode = str((params or {}).get("wb_mode", "as_shot"))
    if mode in RAW_WB_PRESETS and mode != "daylight":
        temperature, tint = RAW_WB_PRESETS[mode]
        prophoto = apply_custom_raw_white_balance(prophoto, temperature, tint)
    elif mode == "custom":
        temperature = float((params or {}).get("wb_temperature", 5500.0))
        tint = float((params or {}).get("wb_tint", 0.0))
        prophoto = apply_custom_raw_white_balance(prophoto, temperature, tint)
    return (prophoto * 65535.0 + 0.5).astype(np.uint16)


def load_float_rgb(path: Path | str) -> np.ndarray:
    """Load an 8/16/32-bit RGB render without reducing its precision."""
    path = Path(path)
    if path.suffix.lower() in (".tif", ".tiff"):
        return as_float_rgb(tifffile.imread(path))
    return as_float_rgb(np.asarray(Image.open(path).convert("RGB")))


def resize_float_to_size(image: np.ndarray,
                         size: tuple[int, int]) -> np.ndarray:
    """Resize float RGB with Pillow's fast float32 Lanczos implementation.

    Pillow cannot resize a three-channel float image directly, so each channel
    remains in mode ``F`` and the results are stacked without an 8-bit
    round-trip. ``reducing_gap`` uses a cheap reduction before the final
    Lanczos pass for large camera originals.
    """
    source = as_float_rgb(image)
    width = max(1, int(size[0]))
    height = max(1, int(size[1]))
    if source.shape[1] == width and source.shape[0] == height:
        return source
    channels = [
        np.asarray(
            Image.fromarray(source[..., channel], mode="F").resize(
                (width, height), Image.Resampling.LANCZOS, reducing_gap=3.0),
            dtype=np.float32,
        )
        for channel in range(3)
    ]
    return np.clip(np.stack(channels, axis=2), 0.0, 1.0).astype(
        np.float32, copy=False)


def resize_float_width(image: np.ndarray, max_width: int | None) -> np.ndarray:
    """Downscale an RGB float image to a maximum pixel width."""
    if not max_width or image.shape[1] <= int(max_width):
        return as_float_rgb(image)
    width = max(1, int(max_width))
    height = max(1, round(image.shape[0] * width / image.shape[1]))
    return resize_float_to_size(image, (width, height))


def resize_float(image: np.ndarray, long_edge: int | None) -> np.ndarray:
    if not long_edge or max(image.shape[:2]) <= int(long_edge):
        return as_float_rgb(image)
    scale = int(long_edge) / max(image.shape[:2])
    width = max(1, round(image.shape[1] * scale))
    height = max(1, round(image.shape[0] * scale))
    return resize_float_to_size(image, (width, height))


def convert_output_space(image_srgb: np.ndarray, output_space: str, *,
                         input_space: str = "srgb") -> np.ndarray:
    """Convert encoded RGB, clipping only in the destination colour space."""
    output_space = normalise_output_space(output_space)
    input_space = normalise_output_space(input_space)
    if output_space == input_space:
        return np.clip(image_srgb, 0.0, 1.0).astype(np.float32)
    import colour

    converted = colour.RGB_to_RGB(
        np.asarray(image_srgb, dtype=np.float64),
        COLOUR_SPACE_NAMES[input_space],
        COLOUR_SPACE_NAMES[output_space],
        apply_cctf_decoding=True,
        apply_cctf_encoding=True,
    )
    return np.clip(converted, 0.0, 1.0).astype(np.float32)


def wide_develop_edits_supported(job: dict) -> bool:
    """Whether only gamut-independent Develop operations were requested.

    The current grade, local edit and watermark implementations are defined
    in bounded sRGB. Feeding P3/ProPhoto values to them would silently change
    the previewed look. Rotation, crop and resize operate on the selected
    encoded output and are safe on this path.
    """
    import edits
    import export_workflow
    import grade

    return (grade.is_identity(job.get("grade") or {})
            and edits.base_edits_are_identity(job.get("optics"), job.get("heals"))
            and not any(mask["enabled"] for mask in edits.clean_masks(job.get("masks")))
            and not export_workflow.clean_watermark(job.get("watermark"))["enabled"])


def icc_bytes(output_space: str) -> bytes | None:
    output_space = normalise_output_space(output_space)
    path = ICC_PROFILES.get(output_space, ICC_PROFILES["srgb"])
    try:
        return path.read_bytes()
    except OSError:
        return None


def required_icc_bytes(output_space: str) -> bytes:
    profile = icc_bytes(output_space)
    if not profile:
        raise RuntimeError(
            T("The {space} output color profile is missing. Repair the installation before exporting.",
              space=normalise_output_space(output_space)))
    return profile


def save_export_image(image_srgb: np.ndarray, destination: Path | str,
                      *, fmt: str, quality: int = 92,
                      output_space: str = "srgb",
                      input_space: str = "srgb",
                      bit_depth: int = 16,
                      metadata_source: Path | str | None = None,
                      metadata_policy: str = "none",
                      metadata_fields: dict | None = None,
                      warnings: list[str] | None = None) -> tuple[int, int]:
    """Encode tagged RGB, retaining 16 bits for TIFF.

    ``input_space`` describes the supplied pixels; it is sRGB for film and
    adjusted renders, or the selected space for unadjusted Develop exports.
    """
    destination = Path(destination)
    output_space = normalise_output_space(output_space)
    converted = convert_output_space(as_float_rgb(image_srgb), output_space,
                                     input_space=input_space)
    profile = required_icc_bytes(output_space)
    height, width = converted.shape[:2]
    if fmt in ("tif", "tiff"):
        depth = 8 if int(bit_depth) == 8 else 16
        maximum = 255.0 if depth == 8 else 65535.0
        dtype = np.uint8 if depth == 8 else np.uint16
        encoded = (converted * maximum + 0.5).astype(dtype)
        extras = []
        if profile:
            # 34675 is the TIFF InterColorProfile tag; type 7 is UNDEFINED.
            extras.append((34675, 7, len(profile), profile, False))
        tifffile.imwrite(destination, encoded, photometric="rgb",
                         metadata=None, extratags=extras)
    elif fmt in ("heif", "heic"):
        helper = Path(os.environ.get(
            "LIGHTTABLE_VISION_HELPER",
            str(Path(__file__).resolve().parent / "build" / "LightTableVision")))
        if sys.platform != "darwin" or not os.access(helper, os.X_OK):
            raise RuntimeError(
                T("HEIF export requires the bundled macOS ImageIO helper"))
        encoded = (converted * 255.0 + 0.5).astype(np.uint8)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                    prefix="lighttable-heif-", suffix=".tif",
                    dir=destination.parent, delete=False) as stream:
                temporary = Path(stream.name)
            extras = []
            if profile:
                extras.append((34675, 7, len(profile), profile, False))
            tifffile.imwrite(temporary, encoded, photometric="rgb",
                             metadata=None, extratags=extras)
            # Exiv2 cannot currently modify BMFF/HEIF metadata after encode.
            # Put the requested EXIF/XMP on the lossless staging TIFF instead;
            # CGImageDestinationAddImageFromSource carries it into the HEIF.
            if str(metadata_policy).lower() != "none":
                before = len(warnings) if warnings is not None else 0
                succeeded = platform_image.write_metadata(
                    temporary, metadata_source, metadata_policy,
                    metadata_fields or {}, warnings=warnings)
                if not succeeded and warnings is not None and len(warnings) == before:
                    warnings.append(T("Requested metadata could not be saved."))
            completed = subprocess.run(
                [str(helper), "--encode-heif", str(temporary),
                 str(destination), str(int(quality))],
                capture_output=True, text=True, timeout=90, check=False)
            if completed.returncode or not destination.is_file():
                detail = (completed.stderr or completed.stdout or
                          T("ImageIO did not create an output file")).strip()
                raise RuntimeError(T("HEIF export failed: {detail}", detail=detail))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    else:
        encoded = (converted * 255.0 + 0.5).astype(np.uint8)
        image = Image.fromarray(encoded, "RGB")
        kwargs = {"icc_profile": profile} if profile else {}
        if fmt == "png":
            image.save(destination, "PNG", **kwargs)
        else:
            image.save(destination, "JPEG", quality=int(quality),
                       subsampling=1, **kwargs)
    return width, height
