# SPDX-License-Identifier: GPL-3.0-only
"""Post-film grade: the adjustments applied on top of the film render.

The maths here is mirrored exactly in web/gl.js so the WebGL preview and the
exported file agree. Any change to one must be made in the other; the order
of operations below is the contract.

Input and output are float arrays in [0,1], display-referred sRGB.
"""
from __future__ import annotations

import colorsys
import math
import threading
import numpy as np

try:
    import numba
    _HAS_NUMBA = True
except ImportError:
    _HAS_NUMBA = False

# numba's default workqueue threading layer aborts the whole process when two
# Python threads enter a parallel=True kernel at once, and the thread-safe tbb
# and omp layers are not part of the runtime. Export workers, request handler
# threads, and the startup warm-up all reach the kernel, so entry is
# serialised here; the kernel itself still fans out across cores.
_PARALLEL_KERNEL_LOCK = threading.Lock()

DEFAULTS = {
    "exposure": 0.0,     # stops
    "contrast": 0.0,     # -1..1
    "highlights": 0.0,   # -1..1
    "shadows": 0.0,      # -1..1
    "whites": 0.0,       # -1..1
    "blacks": 0.0,       # -1..1
    "temp": 0.0,         # -1..1  (blue <-> amber)
    "tint": 0.0,         # -1..1  (green <-> magenta)
    "vibrance": 0.0,     # -1..1
    "saturation": 0.0,   # -1..1
    "texture": 0.0,      # -1..1, one-pixel local detail
    "clarity": 0.0,      # -1..1, midtone-weighted local contrast
    "dehaze": 0.0,       # -1..1
    "vignette": 0.0,     # -1..1
    "vignetteSize": 0.5,  # 0..1, size of the clear central area
    "vignetteFeather": 1.0,  # 0..1, softness of the transition
    "sharpness": 0.0,    # 0..1, output sharpening
    "sharpenRadius": 1.0,  # 0.5..3 pixels
    "sharpenDetail": 0.25,  # 0..1, fine texture emphasis
    "sharpenMasking": 0.0,  # 0..1, restrict to stronger edges
    "luminanceNoise": 0.0,  # 0..1
    "colorNoise": 0.0,      # 0..1
    "chromaticAberrationRedCyan": 0.0,   # -1..1 radial red shift
    "chromaticAberrationBlueYellow": 0.0,  # -1..1 radial blue shift
    "monochrome": 0.0,   # 0 or 1, dedicated B&W treatment
}

RANGES = {
    "vignetteSize": (0.0, 1.0),
    "vignetteFeather": (0.0, 1.0),
    "exposure": (-5.0, 5.0),
    "sharpenRadius": (0.5, 3.0),
    "sharpenDetail": (0.0, 1.0),
    "sharpenMasking": (0.0, 1.0),
    "sharpness": (0.0, 1.0),
    "luminanceNoise": (0.0, 1.0),
    "colorNoise": (0.0, 1.0),
    "monochrome": (0.0, 1.0),
}

LUMA = np.array([0.2126, 0.7152, 0.0722])


# The tone curve travels as a 256-entry lookup table computed by the client,
# not as control points. That makes preview/export parity structural: the
# server applies the exact table the shader sampled, so there is no second
# interpolation implementation to drift.
CURVE_KEYS = ("curveL", "curveR", "curveG", "curveB")
# Eight hue bands, matching web/gl.js. Each carries hue / sat / lum in -1..1.
HSL_BANDS = ("red", "orange", "yellow", "green", "aqua", "blue",
             "purple", "magenta")
HSL_CENTRES = np.array([0.0, 30.0, 60.0, 120.0, 180.0, 240.0, 280.0, 320.0])
ADVANCED_KEYS = ("pointColor", "colorGrading")
COLOR_GRADING_TONES = ("shadows", "midtones", "highlights", "global")


def _finite_number(value, default):
    """Coerce a JSON number, rejecting NaN/Infinity before they reach math."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clean_curve(v):
    if not v or not isinstance(v, (list, tuple)) or len(v) != 256:
        return None
    out = []
    for x in v:
        number = _finite_number(x, None)
        if number is None:
            # A single non-finite sample would otherwise clamp to 1.0 and
            # turn the whole curve into a white-out. Drop the curve instead.
            return None
        out.append(max(0.0, min(1.0, number)))
    # An identity ramp is the same as no curve at all.
    if all(abs(out[i] - i / 255.0) < 0.002 for i in range(256)):
        return None
    return out


def _clean_hsl(v):
    if not isinstance(v, dict):
        return None
    out = {}
    for b in HSL_BANDS:
        e = v.get(b) or {}
        # Clamp to the documented -1..1 travel. The sliders cannot leave it,
        # but a preset, an XMP sidecar, or a command-line edit can, and both
        # the shader and this module multiply the value straight into a hue
        # rotation and a saturation gain, where an unbounded number produces
        # nonsense rather than a stronger effect.
        trio = [round(max(-1.0, min(1.0, _finite_number(
            e.get(k, 0) or 0, 0.0))), 4)
            for k in ("h", "s", "l")]
        if any(abs(x) > 1e-6 for x in trio):
            out[b] = {"h": trio[0], "s": trio[1], "l": trio[2]}
    return out or None


def _clean_point_color(value):
    result = []
    for raw in value[:8] if isinstance(value, list) else []:
        if not isinstance(raw, dict):
            continue
        item = {
            "hue": round(_finite_number(raw.get("hue", 0), 0.0) % 360.0, 3),
            "range": round(max(2.0, min(90.0, _finite_number(
                raw.get("range", 30), 30.0))), 3),
            "hueShift": round(max(-180.0, min(180.0, _finite_number(
                raw.get("hueShift", 0), 0.0))), 3),
            "saturation": round(max(-1.0, min(1.0, _finite_number(
                raw.get("saturation", 0), 0.0))), 4),
            "luminance": round(max(-1.0, min(1.0, _finite_number(
                raw.get("luminance", 0), 0.0))), 4),
            "uniformHue": round(max(0.0, min(1.0, _finite_number(
                raw.get("uniformHue", 0), 0.0))), 4),
            "uniformSaturation": round(max(0.0, min(1.0, _finite_number(
                raw.get("uniformSaturation", 0), 0.0))), 4),
            "uniformLuminance": round(max(0.0, min(1.0, _finite_number(
                raw.get("uniformLuminance", 0), 0.0))), 4),
        }
        for key in ("refSaturation", "refLuminance"):
            if raw.get(key) is None:
                continue
            number = _finite_number(raw[key], None)
            if number is not None:
                item[key] = round(max(0.0, min(1.0, number)), 4)
        result.append(item)
    return result or None


def _clean_color_grading(value):
    if not isinstance(value, dict):
        return None
    result = {}
    active = False
    for tone in COLOR_GRADING_TONES:
        raw = value.get(tone) if isinstance(value.get(tone), dict) else {}
        item = {
            "hue": round(_finite_number(raw.get("hue", 0), 0.0) % 360.0, 3),
            "saturation": round(max(0.0, min(1.0, _finite_number(
                raw.get("saturation", 0), 0.0))), 4),
            "luminance": round(max(-1.0, min(1.0, _finite_number(
                raw.get("luminance", 0), 0.0))), 4),
        }
        result[tone] = item
        active = active or item["saturation"] > 1e-6 or abs(item["luminance"]) > 1e-6
    result["balance"] = round(max(-1.0, min(1.0, _finite_number(
        value.get("balance", 0), 0.0))), 4)
    result["blending"] = round(max(0.0, min(1.0, _finite_number(
        value.get("blending", 0.5), 0.5))), 4)
    return result if active else None


def clean(g: dict | None) -> dict:
    g = g or {}
    out = {}
    for k, v in DEFAULTS.items():
        number = _finite_number(g.get(k, v), v)
        minimum, maximum = RANGES.get(k, (-1.0, 1.0))
        out[k] = round(max(minimum, min(maximum, number)), 4)
    for k in CURVE_KEYS:
        c = _clean_curve(g.get(k))
        if c:
            out[k] = c
    # Retain the editor controls alongside their authoritative sampled curve.
    parametric = g.get("parametricCurve")
    if isinstance(parametric, dict):
        ranges = {"highlights": (-100, 100, 0), "lights": (-100, 100, 0),
                  "darks": (-100, 100, 0), "shadows": (-100, 100, 0),
                  "splitSD": (0.10, 0.40, 0.25), "splitDL": (0.40, 0.60, 0.50),
                  "splitLH": (0.60, 0.90, 0.75)}
        controls = {}
        for key, (low, high, default) in ranges.items():
            try:
                value = float(parametric.get(key, default))
                controls[key] = min(high, max(low, value)) if np.isfinite(value) else default
            except (TypeError, ValueError):
                controls[key] = default
        out["parametricCurve"] = controls
    h = _clean_hsl(g.get("hsl"))
    if h:
        out["hsl"] = h
    points = _clean_point_color(g.get("pointColor"))
    if points:
        out["pointColor"] = points
    color_grading = _clean_color_grading(g.get("colorGrading"))
    if color_grading:
        out["colorGrading"] = color_grading
    return out


def is_identity(g: dict) -> bool:
    g = clean(g)
    if any(g.get(k) for k in (*CURVE_KEYS, *ADVANCED_KEYS)) or g.get("hsl"):
        return False
    return all(abs(g[k] - DEFAULTS[k]) < 1e-6 for k in DEFAULTS
               if g["vignette"] or k not in ("vignetteSize", "vignetteFeather"))


def _apply_curve(c: np.ndarray, g: dict) -> np.ndarray:
    """Sample the client's 256-entry tables with linear interpolation."""
    xs = np.arange(256, dtype=np.float32) / 255.0
    lut = g.get("curveL")
    if lut:
        c = np.interp(c, xs, np.asarray(lut, dtype=np.float32)).astype(np.float32)
    for ch, key in enumerate(("curveR", "curveG", "curveB")):
        lut = g.get(key)
        if lut:
            c[..., ch] = np.interp(
                c[..., ch], xs, np.asarray(lut, dtype=np.float32))
    return c


def _rgb_to_hsv(c: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mx = c.max(axis=2)
    mn = c.min(axis=2)
    v = mx
    d = mx - mn
    sat = np.where(mx > 1e-5, d / np.maximum(mx, 1e-5), 0.0)

    r, gch, b = c[..., 0], c[..., 1], c[..., 2]
    hue = np.zeros_like(mx)
    safe = d > 1e-5
    with np.errstate(invalid="ignore", divide="ignore"):
        hr = ((gch - b) / np.where(safe, d, 1)) % 6.0
        hg = (b - r) / np.where(safe, d, 1) + 2.0
        hb = (r - gch) / np.where(safe, d, 1) + 4.0
    hue = np.where(mx == r, hr, np.where(mx == gch, hg, hb)) * 60.0
    hue = np.where(safe, hue % 360.0, 0.0)

    return hue, sat, v


def _hsv_to_rgb(hue: np.ndarray, sat: np.ndarray,
                value: np.ndarray) -> np.ndarray:
    hue = hue % 360.0
    sat = np.clip(sat, 0.0, 1.0)
    v = np.clip(value, 0.0, 1.0)
    hh = hue / 60.0
    i = np.floor(hh).astype(np.int32) % 6
    f = hh - np.floor(hh)
    pv = v * (1.0 - sat)
    q = v * (1.0 - sat * f)
    t = v * (1.0 - sat * (1.0 - f))
    out = np.empty((*hue.shape, 3), dtype=np.float32)
    choices = [(v, t, pv), (q, v, pv), (pv, v, t),
               (pv, q, v), (t, pv, v), (v, pv, q)]
    for idx, (rr, gg, bb) in enumerate(choices):
        mask = i == idx
        out[..., 0] = np.where(mask, rr, out[..., 0]) if idx else rr
        out[..., 1] = np.where(mask, gg, out[..., 1]) if idx else gg
        out[..., 2] = np.where(mask, bb, out[..., 2]) if idx else bb
    return np.clip(out, 0.0, 1.0)


def _smoothstep(edge0, edge1, value):
    t = np.clip((value - edge0) / max(edge1 - edge0, 1e-6), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _apply_hsl(c: np.ndarray, hsl: dict) -> np.ndarray:
    """Per-hue-band hue / saturation / luminance, mirroring web/gl.js."""
    hue, sat, v = _rgb_to_hsv(c)
    dh = np.zeros_like(hue)
    ds = np.zeros_like(hue)
    dl = np.zeros_like(hue)
    for i, band in enumerate(HSL_BANDS):
        e = hsl.get(band)
        if not e:
            continue
        diff = np.abs(((hue - HSL_CENTRES[i] + 180.0) % 360.0) - 180.0)
        w = np.clip(1.0 - diff / 45.0, 0.0, 1.0)
        w = w * w * (3.0 - 2.0 * w) * sat      # weight by how colourful it is
        dh += w * e["h"] * 30.0
        ds += w * e["s"]
        dl += w * e["l"]

    hue = (hue + dh) % 360.0
    sat = np.clip(sat * (1.0 + ds), 0.0, 1.0)
    v = np.clip(v * (1.0 + dl * 0.5), 0.0, 1.0)

    return _hsv_to_rgb(hue, sat, v)


def _apply_monochrome(c: np.ndarray, hsl: dict | None) -> np.ndarray:
    """Per-hue-band B&W mixer, mirroring web/gl.js and rust-engine."""
    hue, sat, _ = _rgb_to_hsv(c)
    dl = np.zeros_like(hue)
    if hsl:
        for i, band in enumerate(HSL_BANDS):
            e = hsl.get(band)
            if not e or not e.get("l"):
                continue
            diff = np.abs(((hue - HSL_CENTRES[i] + 180.0) % 360.0) - 180.0)
            w = np.clip(1.0 - diff / 45.0, 0.0, 1.0)
            w = w * w * (3.0 - 2.0 * w) * sat
            dl += w * e["l"] * 0.5
    luma = (c @ LUMA) + dl
    return np.clip(np.repeat(luma[..., None], 3, axis=-1), 0.0, 1.0)


def _apply_point_color(c: np.ndarray, points: list[dict]) -> np.ndarray:
    hue, sat, value = _rgb_to_hsv(c)
    for point in points:
        difference = np.abs(((hue - point["hue"] + 180.0) % 360.0) - 180.0)
        weight = 1.0 - _smoothstep(point["range"] * 0.45,
                                   point["range"], difference)
        weight *= sat
        if "refSaturation" in point and "refLuminance" in point:
            shortest = ((point["hue"] - hue + 180.0) % 360.0) - 180.0
            hue = (hue + shortest * point["uniformHue"] * weight) % 360.0
            sat = np.clip(sat + (point["refSaturation"] - sat)
                          * point["uniformSaturation"] * weight, 0.0, 1.0)
            value = np.clip(value + (point["refLuminance"] - value)
                            * point["uniformLuminance"] * weight, 0.0, 1.0)
        hue = (hue + point["hueShift"] * weight) % 360.0
        sat = np.clip(sat * (1.0 + point["saturation"] * weight), 0.0, 1.0)
        value = np.clip(value * (1.0 + point["luminance"] * 0.5 * weight),
                        0.0, 1.0)
    return _hsv_to_rgb(hue, sat, value)


def _grading_weights(luminance: np.ndarray, balance: float,
                     blending: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    shift = balance * 0.2
    width = 0.18 + blending * 0.22
    shadows = 1.0 - _smoothstep(0.28 + shift - width,
                                0.28 + shift + width, luminance)
    highlights = _smoothstep(0.72 + shift - width,
                             0.72 + shift + width, luminance)
    midtones = np.clip(1.0 - shadows - highlights, 0.0, 1.0)
    return shadows, midtones, highlights


def _apply_color_grading(c: np.ndarray, settings: dict) -> np.ndarray:
    luminance = c @ LUMA
    weights = _grading_weights(luminance, settings["balance"],
                               settings["blending"])
    result = c.copy()
    for tone, weight in zip(("shadows", "midtones", "highlights"), weights):
        item = settings[tone]
        tint = np.asarray(colorsys.hsv_to_rgb(item["hue"] / 360.0, 1.0, 1.0),
                          dtype=np.float32)
        chroma = tint - float(tint @ LUMA)
        result += (chroma * item["saturation"] * 0.28
                   + item["luminance"] * 0.22) * weight[..., None]
    item = settings["global"]
    tint = np.asarray(colorsys.hsv_to_rgb(item["hue"] / 360.0, 1.0, 1.0),
                      dtype=np.float32)
    chroma = tint - float(tint @ LUMA)
    result += chroma * item["saturation"] * 0.2 + item["luminance"] * 0.18
    return np.clip(result, 0.0, 1.0)


def _srgb_to_linear(c: np.ndarray) -> np.ndarray:
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0.0, None)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def _axis_sample(c: np.ndarray, offset: float, axis: int) -> np.ndarray:
    """Linearly sample an image at a constant x/y offset, clamped at edges."""
    size = c.shape[axis]
    coordinates = np.clip(np.arange(size, dtype=np.float32) + offset,
                          0.0, size - 1.0)
    lower = np.floor(coordinates).astype(np.int32)
    upper = np.minimum(lower + 1, size - 1)
    fraction = coordinates - lower
    shape = [1, 1, 1]
    shape[axis] = size
    fraction = fraction.reshape(shape)
    return (np.take(c, lower, axis=axis) * (1.0 - fraction)
            + np.take(c, upper, axis=axis) * fraction)


def _cross_blur(c: np.ndarray, radius: float = 1.0) -> np.ndarray:
    """Five-tap, edge-clamped blur matching the WebGL neighbor samples."""
    return (c + _axis_sample(c, radius, 1) + _axis_sample(c, -radius, 1)
            + _axis_sample(c, radius, 0) + _axis_sample(c, -radius, 0)) / 5.0


def _cross_blur_with_center(center: np.ndarray, source: np.ndarray,
                            radius: float = 1.0) -> np.ndarray:
    """Blur whose centre may contain earlier stages, as in the GPU shader."""
    return (center + _axis_sample(source, radius, 1)
            + _axis_sample(source, -radius, 1)
            + _axis_sample(source, radius, 0)
            + _axis_sample(source, -radius, 0)) / 5.0


def _bilinear_channel(channel: np.ndarray, sx: np.ndarray,
                      sy: np.ndarray) -> np.ndarray:
    height, width = channel.shape
    sx = np.clip(sx, 0.0, width - 1.0)
    sy = np.clip(sy, 0.0, height - 1.0)
    x0 = np.floor(sx).astype(np.int32)
    y0 = np.floor(sy).astype(np.int32)
    x1 = np.minimum(x0 + 1, width - 1)
    y1 = np.minimum(y0 + 1, height - 1)
    fx = sx - x0
    fy = sy - y0
    return ((channel[y0, x0] * (1.0 - fx) + channel[y0, x1] * fx) * (1.0 - fy)
            + (channel[y1, x0] * (1.0 - fx) + channel[y1, x1] * fx) * fy)


def _correct_chromatic_aberration(c: np.ndarray, red_cyan: float,
                                  blue_yellow: float) -> np.ndarray:
    """Radially realign red/blue fringes, matching the preview shader."""
    if not red_cyan and not blue_yellow:
        return c
    height, width = c.shape[:2]
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    nx = xx / max(width - 1, 1) - 0.5
    ny = yy / max(height - 1, 1) - 0.5
    out = c.copy()
    if red_cyan:
        out[..., 0] = _bilinear_channel(
            c[..., 0], xx + nx * red_cyan * 6.0, yy + ny * red_cyan * 6.0)
    if blue_yellow:
        out[..., 2] = _bilinear_channel(
            c[..., 2], xx + nx * blue_yellow * 6.0, yy + ny * blue_yellow * 6.0)
    return np.clip(out, 0.0, 1.0).astype(np.float32)


if _HAS_NUMBA:
    @numba.njit(parallel=True)
    def _numba_grade_stages_5_6(c, exp, hl, sh, whites, blacks, contrast, dehaze, temp, tint, sat, vib):
        h, w, _ = c.shape
        out = np.empty((h, w, 3), dtype=np.float32)
        exp_factor = 2.0 ** exp
        gain_r = 1.0 + temp * 0.18 + tint * 0.06
        gain_g = 1.0 - tint * 0.12
        gain_b = 1.0 - temp * 0.18 + tint * 0.06
        w_denom = max(1.0 + whites * 0.35 - (blacks * -0.25), 1e-4)
        b_val = blacks * -0.25
        haze_val = dehaze * 0.12
        haze_denom = max(1.0 - haze_val, 0.2)

        for y in numba.prange(h):
            for x in range(w):
                r = c[y, x, 0]
                g = c[y, x, 1]
                b = c[y, x, 2]

                # _srgb_to_linear
                r_lin = r / 12.92 if r <= 0.04045 else ((r + 0.055) / 1.055) ** 2.4
                g_lin = g / 12.92 if g <= 0.04045 else ((g + 0.055) / 1.055) ** 2.4
                b_lin = b / 12.92 if b <= 0.04045 else ((b + 0.055) / 1.055) ** 2.4

                if exp != 0.0:
                    r_lin *= exp_factor
                    g_lin *= exp_factor
                    b_lin *= exp_factor

                if hl != 0.0 or sh != 0.0:
                    lum = max(0.0, r_lin * 0.2126 + g_lin * 0.7152 + b_lin * 0.0722)
                    if hl != 0.0:
                        m = max(0.0, min(1.0, (lum - 0.35) / 0.65)) ** 1.2
                        scale = 1.0 + hl * 0.85 * m
                        r_lin *= scale; g_lin *= scale; b_lin *= scale
                    if sh != 0.0:
                        m = max(0.0, min(1.0, (0.45 - lum) / 0.45)) ** 1.2
                        scale = 1.0 + sh * 1.5 * m
                        r_lin *= scale; g_lin *= scale; b_lin *= scale

                # _linear_to_srgb
                r_lin = max(0.0, r_lin)
                g_lin = max(0.0, g_lin)
                b_lin = max(0.0, b_lin)
                r_s = r_lin * 12.92 if r_lin <= 0.0031308 else 1.055 * (r_lin ** (1.0 / 2.4)) - 0.055
                g_s = g_lin * 12.92 if g_lin <= 0.0031308 else 1.055 * (g_lin ** (1.0 / 2.4)) - 0.055
                b_s = b_lin * 12.92 if b_lin <= 0.0031308 else 1.055 * (b_lin ** (1.0 / 2.4)) - 0.055

                r_s = max(0.0, min(1.0, r_s))
                g_s = max(0.0, min(1.0, g_s))
                b_s = max(0.0, min(1.0, b_s))

                # whites / blacks
                if whites != 0.0 or blacks != 0.0:
                    r_s = max(0.0, min(1.0, (r_s - b_val) / w_denom))
                    g_s = max(0.0, min(1.0, (g_s - b_val) / w_denom))
                    b_s = max(0.0, min(1.0, (b_s - b_val) / w_denom))

                # contrast
                if contrast != 0.0:
                    if contrast > 0.0:
                        r_s = r_s + (r_s * r_s * (3.0 - 2.0 * r_s) - r_s) * contrast
                        g_s = g_s + (g_s * g_s * (3.0 - 2.0 * g_s) - g_s) * contrast
                        b_s = b_s + (b_s * b_s * (3.0 - 2.0 * b_s) - b_s) * contrast
                    else:
                        r_s = 0.5 + (r_s - 0.5) * (1.0 + contrast * 0.8)
                        g_s = 0.5 + (g_s - 0.5) * (1.0 + contrast * 0.8)
                        b_s = 0.5 + (b_s - 0.5) * (1.0 + contrast * 0.8)
                    r_s = max(0.0, min(1.0, r_s))
                    g_s = max(0.0, min(1.0, g_s))
                    b_s = max(0.0, min(1.0, b_s))

                # dehaze
                if dehaze != 0.0:
                    r_s = max(0.0, min(1.0, (r_s - haze_val) / haze_denom))
                    g_s = max(0.0, min(1.0, (g_s - haze_val) / haze_denom))
                    b_s = max(0.0, min(1.0, (b_s - haze_val) / haze_denom))
                    lum = r_s * 0.2126 + g_s * 0.7152 + b_s * 0.0722
                    factor = 1.0 + dehaze * 0.18
                    r_s = max(0.0, min(1.0, lum + (r_s - lum) * factor))
                    g_s = max(0.0, min(1.0, lum + (g_s - lum) * factor))
                    b_s = max(0.0, min(1.0, lum + (b_s - lum) * factor))

                # temp / tint
                if temp != 0.0 or tint != 0.0:
                    r_s = max(0.0, min(1.0, r_s * gain_r))
                    g_s = max(0.0, min(1.0, g_s * gain_g))
                    b_s = max(0.0, min(1.0, b_s * gain_b))

                # sat / vibrance
                if sat != 0.0 or vib != 0.0:
                    lum = r_s * 0.2126 + g_s * 0.7152 + b_s * 0.0722
                    if sat != 0.0:
                        r_s = max(0.0, min(1.0, lum + (r_s - lum) * (1.0 + sat)))
                        g_s = max(0.0, min(1.0, lum + (g_s - lum) * (1.0 + sat)))
                        b_s = max(0.0, min(1.0, lum + (b_s - lum) * (1.0 + sat)))
                    if vib != 0.0:
                        lum = r_s * 0.2126 + g_s * 0.7152 + b_s * 0.0722
                        mx = max(r_s, max(g_s, b_s))
                        mn = min(r_s, min(g_s, b_s))
                        s_val = (mx - mn) / max(mx, 1e-4)
                        factor = 1.0 + vib * (1.0 - s_val)
                        r_s = max(0.0, min(1.0, lum + (r_s - lum) * factor))
                        g_s = max(0.0, min(1.0, lum + (g_s - lum) * factor))
                        b_s = max(0.0, min(1.0, lum + (b_s - lum) * factor))

                out[y, x, 0] = r_s
                out[y, x, 1] = g_s
                out[y, x, 2] = b_s
        return out


def warm_grade_jit() -> None:
    if not _HAS_NUMBA:
        return
    try:
        dummy = np.zeros((2, 2, 3), dtype=np.float32)
        with _PARALLEL_KERNEL_LOCK:
            _numba_grade_stages_5_6(dummy, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    except Exception:
        pass


def apply(img: np.ndarray, g: dict) -> np.ndarray:
    """Apply the grade. `img` is float [0,1] sRGB, HxWx3."""
    g = clean(g)
    if is_identity(g):
        return img
    c = np.clip(img.astype(np.float32), 0.0, 1.0)

    c = _correct_chromatic_aberration(
        c, g["chromaticAberrationRedCyan"],
        g["chromaticAberrationBlueYellow"])
    source = c

    if g["luminanceNoise"] or g["colorNoise"]:
        blur = _cross_blur(c)
        y = (c @ LUMA)[..., None]
        blur_y = (blur @ LUMA)[..., None]
        if g["luminanceNoise"]:
            c = np.clip(c + (blur_y - y) * g["luminanceNoise"], 0.0, 1.0)
            y = (c @ LUMA)[..., None]
        if g["colorNoise"]:
            blur_chroma = blur - blur_y
            chroma = c - y
            c = np.clip(y + chroma * (1.0 - g["colorNoise"])
                        + blur_chroma * g["colorNoise"], 0.0, 1.0)

    if g["texture"] or g["clarity"]:
        detail = c - _cross_blur_with_center(c, source)
        if g["texture"]:
            c = np.clip(c + detail * g["texture"] * 1.1, 0.0, 1.0)
        if g["clarity"]:
            mid = np.clip(1.0 - np.abs(c @ LUMA - 0.5) * 2.0,
                          0.0, 1.0)[..., None]
            c = np.clip(c + detail * g["clarity"] * 1.8 * mid,
                        0.0, 1.0)

    if g["sharpness"]:
        detail = c - _cross_blur_with_center(c, source, g["sharpenRadius"])
        luma_detail = (detail @ LUMA)[..., None]
        shaped = luma_detail + (detail - luma_detail) * g["sharpenDetail"]
        edge = np.sqrt(np.sum(detail * detail, axis=2, keepdims=True))
        # GLSL smoothstep(0.015, 0.16, edge).
        threshold = np.clip((edge - 0.015) / (0.16 - 0.015), 0.0, 1.0)
        threshold = threshold * threshold * (3.0 - 2.0 * threshold)
        mask = 1.0 - g["sharpenMasking"] + threshold * g["sharpenMasking"]
        c = np.clip(c + shaped * g["sharpness"] * 1.8 * mask, 0.0, 1.0)

    need_stages_5_6 = bool(
        g["exposure"] or g["highlights"] or g["shadows"] or
        g["whites"] or g["blacks"] or g["contrast"] or
        g["dehaze"] or g["temp"] or g["tint"] or
        g["saturation"] or g["vibrance"]
    )
    if _HAS_NUMBA and need_stages_5_6:
        try:
            with _PARALLEL_KERNEL_LOCK:
                c = _numba_grade_stages_5_6(
                    c, float(g["exposure"]), float(g["highlights"]), float(g["shadows"]),
                    float(g["whites"]), float(g["blacks"]), float(g["contrast"]),
                    float(g["dehaze"]), float(g["temp"]), float(g["tint"]),
                    float(g["saturation"]), float(g["vibrance"])
                )
            need_stages_5_6 = False
        except Exception:
            pass

    if need_stages_5_6:
        # --- linear-light stage -------------------------------------------------
        lin = _srgb_to_linear(c)
        if g["exposure"]:
            lin = lin * (2.0 ** g["exposure"])

        # Highlight / shadow recovery, masked by luminance so each acts on its own
        # end of the range rather than the whole image.
        if g["highlights"] or g["shadows"]:
            y = np.clip(lin @ LUMA, 0.0, None)[..., None]
            if g["highlights"]:
                m = np.clip((y - 0.35) / 0.65, 0.0, 1.0) ** 1.2
                lin = lin * (1.0 + g["highlights"] * 0.85 * m)
            if g["shadows"]:
                m = np.clip((0.45 - y) / 0.45, 0.0, 1.0) ** 1.2
                lin = lin * (1.0 + g["shadows"] * 1.5 * m)
        c = np.clip(_linear_to_srgb(lin), 0.0, 1.0)

        # --- display-referred stage --------------------------------------------
        if g["whites"] or g["blacks"]:
            # Move the endpoints, then renormalise so the range stays [0,1].
            w = 1.0 + g["whites"] * 0.35
            b = g["blacks"] * -0.25
            c = np.clip((c - b) / max(w - b, 1e-4), 0.0, 1.0)

        if g["contrast"]:
            k = g["contrast"]
            if k > 0:
                s_curve = c * c * (3.0 - 2.0 * c)      # smoothstep: steeper mid
                c = c + (s_curve - c) * k
            else:
                c = 0.5 + (c - 0.5) * (1.0 + k * 0.8)  # flatten toward mid grey
            c = np.clip(c, 0.0, 1.0)

        if g["dehaze"]:
            haze = g["dehaze"] * 0.12
            c = np.clip((c - haze) / max(1.0 - haze, 0.2), 0.0, 1.0)
            y = (c @ LUMA)[..., None]
            c = np.clip(y + (c - y) * (1.0 + g["dehaze"] * 0.18),
                        0.0, 1.0)

        if g["temp"] or g["tint"]:
            gain = np.array([
                1.0 + g["temp"] * 0.18 + g["tint"] * 0.06,
                1.0 - g["tint"] * 0.12,
                1.0 - g["temp"] * 0.18 + g["tint"] * 0.06,
            ], dtype=np.float32)
            c = np.clip(c * gain, 0.0, 1.0)

        if g["saturation"] or g["vibrance"]:
            y = (c @ LUMA)[..., None]
            if g["saturation"]:
                c = np.clip(y + (c - y) * (1.0 + g["saturation"]), 0.0, 1.0)
            if g["vibrance"]:
                mx = c.max(axis=2, keepdims=True)
                mn = c.min(axis=2, keepdims=True)
                sat = (mx - mn) / np.maximum(mx, 1e-4)
                y = (c @ LUMA)[..., None]
                c = np.clip(y + (c - y) * (1.0 + g["vibrance"] * (1.0 - sat)), 0.0, 1.0)

    if g.get("monochrome", 0.0) > 0.5:
        c = _apply_monochrome(c, g.get("hsl"))
    elif g.get("hsl"):
        c = _apply_hsl(c, g["hsl"])

    if g.get("pointColor"):
        c = _apply_point_color(c, g["pointColor"])

    if g.get("colorGrading"):
        c = _apply_color_grading(c, g["colorGrading"])

    if any(g.get(k) for k in CURVE_KEYS):
        c = _apply_curve(c, g)

    if g["vignette"]:
        h, w = c.shape[:2]
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        nx = (xx / max(w - 1, 1) - 0.5) * 2.0
        ny = (yy / max(h - 1, 1) - 0.5) * 2.0
        r = np.sqrt(nx * nx + ny * ny) / 1.4142
        size, feather = g["vignetteSize"], g["vignetteFeather"]
        # The defaults retain the original curve, including its corner values.
        if size != 0.5 or feather != 1.0:
            outer = 0.25 + 1.5 * size
            width = outer * max(feather, 0.01)
            r = np.clip((r - outer + width) / width, 0.0, 1.0)
        falloff = np.clip(1.0 - g["vignette"] * 0.9 * (r ** 2.2), 0.0, 2.0)[..., None]
        c = np.clip(c * falloff, 0.0, 1.0)

    return c


def apply_accelerated(img: np.ndarray, g: dict) -> np.ndarray:
    """Float32 GPU finishing for exports, with the established CPU fallback.

    Small images stay on CPU because worker transport costs more than the
    shader saves. This does not change the sRGB domain or any edit ordering.
    """
    g = clean(g)
    if is_identity(g):
        return img
    heavy = (any(g[key] for key in ("texture", "clarity", "sharpness",
                                   "luminanceNoise", "colorNoise"))
             or any(g.get(key) for key in (*CURVE_KEYS, *ADVANCED_KEYS, "hsl")))
    # Tone-only grades already have a fast fused Numba kernel. At 33 MP,
    # sending those pixels across the worker boundary costs more than it saves.
    # Luminance uniformity can lift near-black pixels by hundreds of times,
    # amplifying harmless earlier float32 hue roundoff past 16-bit tolerance.
    # A CPU Point Color pass after GPU detail would retain that roundoff, so
    # these uncommon recipes keep the whole established reference sequence.
    sensitive_uniformity = any(
        point.get("uniformLuminance", 0.0) > 0.0
        and point.get("refLuminance", 0.0) > 0.0
        and "refSaturation" in point
        for point in g.get("pointColor", []))
    worthwhile = (heavy or not _HAS_NUMBA) and not sensitive_uniformity
    if (worthwhile and img.ndim == 3 and img.shape[2] == 3
            and img.shape[0] * img.shape[1] >= 262144):
        try:
            import gpu_compute
            source, settings = img, g
            if g["chromaticAberrationRedCyan"] or g["chromaticAberrationBlueYellow"]:
                # Float32 GPU coordinate fusion shifts high-contrast fringes
                # enough to exceed our 16-bit export tolerance. Keep this
                # geometry step exact, then accelerate the remaining grade.
                source = _correct_chromatic_aberration(
                    np.clip(img.astype(np.float32), 0.0, 1.0),
                    g["chromaticAberrationRedCyan"], g["chromaticAberrationBlueYellow"])
                settings = dict(g, chromaticAberrationRedCyan=0.0,
                                chromaticAberrationBlueYellow=0.0)
                if is_identity(settings):
                    return source
            return gpu_compute.compute("grade", source,
                [img.shape[1], img.shape[0]], img.shape, grade=settings)
        except (RuntimeError, OSError, ValueError, TimeoutError):
            pass
    return apply(img, g)
