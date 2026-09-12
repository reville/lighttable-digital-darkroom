# SPDX-License-Identifier: GPL-3.0-only
"""Versioned LightTable interpretations; separate from measured film profiles.

V1 gently contracts the source yellow-green hue interval toward green before
spectral reconstruction. True yellows, skin, neutrals, blues and cyans are
outside its support. Luminance is preserved in linear ProPhoto (D50).
This is a visual interpretation with numerical guardrails, not a film scan fit.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np

TUNINGS = {
    "kodak_portra_160": {"green_amount": 0.9},
    "kodak_portra_400": {"green_amount": 0.8},
    "kodak_portra_800": {"green_amount": 0.55},
}
VERSION = "1"
DESCRIPTION = ("LightTable's interpretation, adjusted using reference "
               "photographs and color checks. Not calibrated to a measured film scan.")

# Processed sources (JPEG, HEIC, TIFF) arrive display-referred: a camera or an
# editor has already applied a tone curve, so their mid-tones are lifted and
# their highlights are packed into the top of the range. The film model expects
# scene light and adds its own toe and shoulder, which is why a finished JPEG
# rendered as-is comes out flat and grey. Before filming, the expansion below
# raises the linear values to this power around middle grey: grey stays put,
# shadows fall back to where a scene would have them, and highlights extend
# above 1.0 as scene highlights do. Metering is unaffected at middle grey, so
# an edit with a fixed print exposure keeps its brightness. RAW decodes are
# scene-linear already and are never expanded.
DISPLAY_EXPANSION = 1.8
MIDDLE_GREY_LINEAR = 0.18
# The film model's automatic exposure is a centre-weighted mean of luminance
# pulled to 0.184, measured after the input is prepared. An expansion anchored
# at a fixed grey would spread that mean and the meter would pull the whole
# frame darker. Anchoring it instead at the value that leaves the weighted
# mean unchanged makes the expansion exposure-neutral for the meter and, with
# metering off, keeps the frame's overall brightness. The weighting mirrors
# spektrafilm's centre-weighted method: a Gaussian with sigma 0.2 of the long
# edge, measured on a preview no larger than this.
ANCHOR_PREVIEW = 256
ANCHOR_SIGMA = 0.2
ANCHOR_RANGE = (0.001, 4.0)


def _romm_decode(value: np.ndarray) -> np.ndarray:
    return np.where(value < 0.03125, value / 16.0, np.maximum(value, 0.0) ** 1.8)


def expansion_anchor(image: np.ndarray, *, encoded: bool,
                     expansion: float = DISPLAY_EXPANSION) -> float:
    """The luminance the expansion pivots on so the metered mean is unchanged.

    For a power ``k`` around anchor ``a``, ``a * (Y / a) ** k`` has the same
    weighted mean as ``Y`` when ``a = (mean(Y**k) / mean(Y)) ** (1 / (k - 1))``.
    ``encoded`` says the pixels carry the ROMM transfer curve, as processed
    sources do; the anchor is always a linear value.
    """
    source = np.asarray(image, dtype=np.float32)
    if source.ndim != 3 or source.shape[-1] < 3 or not np.isfinite(expansion) or expansion <= 1.0:
        return MIDDLE_GREY_LINEAR
    step = max(1, int(np.ceil(max(source.shape[:2]) / ANCHOR_PREVIEW)))
    small = source[::step, ::step, :3].astype(np.float64)
    if encoded:
        small = _romm_decode(np.clip(small, 0.0, 1.0))
    # The expansion is applied per channel and the meter reads luminance, so
    # the means are taken per channel and combined with the luminance weights:
    # mean(Y') = a**(1-k) * sum_c(coef_c * mean(x_c**k)) must equal mean(Y).
    channels = np.maximum(small, 0.0)
    height, width = channels.shape[:2]
    longest = max(height, width)
    x = (np.arange(width) / width - 0.5) * (width / longest)
    y = (np.arange(height) / height - 0.5) * (height / longest)
    weight = np.exp(-(x[None, :] ** 2 + y[:, None] ** 2) / (2.0 * ANCHOR_SIGMA ** 2))
    weight = weight[:, :, None] / np.sum(weight)
    mean = float(np.sum(weight * channels, axis=(0, 1)) @ PROPHOTO_Y)
    if not np.isfinite(mean) or mean <= 1e-6:
        return MIDDLE_GREY_LINEAR
    mean_power = float(np.sum(weight * channels ** expansion, axis=(0, 1)) @ PROPHOTO_Y)
    anchor = (mean_power / mean) ** (1.0 / (expansion - 1.0))
    if not np.isfinite(anchor):
        return MIDDLE_GREY_LINEAR
    return float(min(ANCHOR_RANGE[1], max(ANCHOR_RANGE[0], anchor)))

# Bradford-adapted linear ProPhoto D50 -> linear sRGB D65, as used by
# colour-science. Only the inverse's red column is needed for this hue interval.
TO_SRGB = np.asarray([
    [2.0340626798588866, -0.7275927647469631, -0.30682606926988176],
    [-0.22868941146731234, 1.2317680014826924, -0.0028926564624774157],
    [-0.008499732546934672, -0.15328915068335006, 1.1615688759268012],
], dtype=np.float64)
FROM_SRGB_RED = np.asarray([
    0.5293363376743782, 0.09831587646200844, 0.016847881261919936,
], dtype=np.float64)
PROPHOTO_Y = np.asarray([0.2880402, 0.7118741, 0.0000857])

TUNING_DIGEST = hashlib.sha256(json.dumps({
    "version": VERSION, "stocks": TUNINGS, "matrix": TO_SRGB.tolist(),
    "red": FROM_SRGB_RED.tolist(), "luminance": PROPHOTO_Y.tolist(),
    "algorithm": "source-yellow-green-contraction-v1",
    "display_expansion": DISPLAY_EXPANSION, "middle_grey": MIDDLE_GREY_LINEAR,
    "anchor": [ANCHOR_PREVIEW, ANCHOR_SIGMA, list(ANCHOR_RANGE)],
}, sort_keys=True).encode()).hexdigest()


def profile_tunings(stock: str) -> list[dict]:
    if stock not in TUNINGS:
        return []
    return [{"id": "lighttable", "version": VERSION,
             "name": "LightTable tuned", "description": DESCRIPTION}]


def specification(params: dict, image: np.ndarray | None = None) -> dict | None:
    """The input preparation both engines apply before filming, or None.

    Two independent parts share one specification so the pixels are prepared
    exactly once: the versioned green interpretation, which only the tuned
    stocks carry, and the display-referred expansion, which every processed
    source needs and no RAW decode does. A RAW source with an untuned stock
    is the one case that needs nothing, and returns None so the engines take
    their original path byte for byte.
    """
    if params.get("profile_enabled", True) is False:
        return None
    tuned = (params.get("film_tuning", "original") == "lighttable"
             and str(params.get("film_tuning_version", VERSION)) == VERSION
             and params.get("stock") in TUNINGS)
    linear_input = bool(params.get("linear_input", False))
    if not tuned and linear_input:
        return None
    expansion = 0.0 if linear_input else DISPLAY_EXPANSION
    anchor = MIDDLE_GREY_LINEAR
    if expansion > 0 and image is not None:
        anchor = expansion_anchor(image, encoded=True, expansion=expansion)
    return {"version": 1,
            "green_amount": TUNINGS[params["stock"]]["green_amount"] if tuned else 0.0,
            "input_cctf_decoding": not linear_input,
            "display_expansion": expansion,
            "display_expansion_anchor": anchor}


def _smooth(value):
    value = np.clip(value, 0.0, 1.0)
    return value * value * (3.0 - 2.0 * value)


def prepare_input(image: np.ndarray, spec: dict) -> np.ndarray:
    """Return linear ProPhoto input without mutating the source/cache array.

    Bounded chunks avoid several full-resolution float64 intermediates. Rust's
    resident renderer implements the same operation before all film stages.
    Encoded processed sources use the ROMM/ProPhoto transfer curve, not sRGB.
    """
    if spec.get("version") != 1:
        raise ValueError("Unsupported LightTable film tuning version")
    amount = float(spec["green_amount"])
    if not np.isfinite(amount) or not 0 <= amount < 1:
        raise ValueError("Invalid LightTable green amount")
    expansion = float(spec.get("display_expansion", 0.0))
    if not np.isfinite(expansion) or expansion < 0 or expansion > 4:
        raise ValueError("Invalid LightTable display expansion")
    anchor = float(spec.get("display_expansion_anchor", MIDDLE_GREY_LINEAR))
    if not np.isfinite(anchor) or not ANCHOR_RANGE[0] <= anchor <= ANCHOR_RANGE[1]:
        raise ValueError("Invalid LightTable display expansion anchor")
    source = np.asarray(image, dtype=np.float32)
    if source.ndim != 3 or source.shape[-1] != 3:
        raise ValueError("Film tuning requires RGB input")
    out = np.empty_like(source)
    source_flat, out_flat = source.reshape(-1, 3), out.reshape(-1, 3)
    for start in range(0, len(source_flat), 262144):
        stop = min(start + 262144, len(source_flat))
        linear = source_flat[start:stop].astype(np.float64)
        if not np.isfinite(linear).all():
            raise ValueError("Film tuning requires finite input")
        if spec.get("input_cctf_decoding", False):
            linear = np.where(linear < 0.03125, linear / 16.0,
                              np.maximum(linear, 0.0) ** 1.8)
        if expansion > 0:
            # Anchored where the metered mean stays put, so exposure is
            # unchanged and highlights are free to exceed 1.0. Non-positive
            # values are left alone.
            linear = np.where(
                linear > 0,
                anchor * (np.maximum(linear, 0.0) / anchor) ** expansion,
                linear)
        rgb = linear @ TO_SRGB.T
        r, g, b = rgb.T
        delta = g - b
        # This sector has G=max, B=min. Other sectors are exact no-ops.
        eligible = (g > r) & (r > b) & (b >= 0) & (g > 1e-10)
        hue = 120.0 + 60.0 * np.divide(b - r, delta,
            out=np.zeros_like(delta), where=delta > 1e-10)
        saturation = np.divide(delta, g, out=np.zeros_like(g), where=g > 1e-10)
        weight = _smooth((hue - 60.0) / 30.0) * _smooth(saturation / 0.15)
        shift = np.where(eligible,
            delta * (120.0 - hue) / 60.0 * amount * weight, 0.0)
        candidate = linear - shift[:, None] * FROM_SRGB_RED
        before, after = linear @ PROPHOTO_Y, candidate @ PROPHOTO_Y
        gain = np.divide(before, after, out=np.ones_like(before), where=after > 1e-10)
        candidate *= gain[:, None]
        # Preserve luminance while easing back toward the original at the
        # input gamut boundary. No per-channel clipping or hue discontinuity.
        difference = candidate - linear
        room = np.divide(1.0 - linear, difference,
            out=np.ones_like(linear), where=difference > 1e-10)
        blend = np.clip(np.min(room, axis=1), 0.0, 1.0)
        tuned = linear + blend[:, None] * difference
        out_flat[start:stop] = np.where((shift > 0)[:, None], tuned, linear)
    return out
