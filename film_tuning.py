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
}, sort_keys=True).encode()).hexdigest()


def profile_tunings(stock: str) -> list[dict]:
    if stock not in TUNINGS:
        return []
    return [{"id": "lighttable", "version": VERSION,
             "name": "LightTable tuned", "description": DESCRIPTION}]


def specification(params: dict) -> dict | None:
    if (params.get("profile_enabled", True) is False
            or params.get("film_tuning", "original") != "lighttable"
            or str(params.get("film_tuning_version", VERSION)) != VERSION
            or params.get("stock") not in TUNINGS):
        return None
    return {"version": 1, **TUNINGS[params["stock"]],
            "input_cctf_decoding": not params.get("linear_input", False)}


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
