#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build the bundled ``LightTable Standard`` camera look as a ``.dcp`` file.

This is a modelled look, not a measurement of any camera and not derived
from any vendor's profile. The numbers below are the whole definition; the
script only expands them into the tables the DNG profile format carries,
and ``tests/test_lighttable_standard_profile.py`` checks the tracked file is
exactly what these numbers produce.

The look has two parts:

* A tone curve that lands scene middle grey (0.18 linear) at display code
  0.52 rather than the 0.46 a plain sRGB encode gives, keeps the slope
  through the mid-tones a little above one, and rolls the highlights off
  into white with a decaying slope instead of clipping against it. The
  control points are stated on the sRGB-encoded axis, where the shape reads
  as a photographer expects, and interpolated with a monotone cubic so the
  curve never reverses. The stored curve is the linear-to-linear form the
  profile format uses, sampled at 256 inputs spaced uniformly in sRGB code
  value so the reader's piecewise-linear lookup stays accurate in the toe.
* A modest hue/saturation map: small hue rotations of a few degrees at
  twelve anchor hues, and a saturation lift of a few percent that fades to
  nothing for fully saturated colours so nothing new is pushed out of
  gamut. Value is never scaled.

The profile carries no colour matrices, so the decoder's own camera
calibration stays in charge for every camera. It is single-illuminant
(D65 nominal); there is nothing to blend by capture white balance.

Regenerate the tracked file with:

    ../.venv/bin/python build_lighttable_standard.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import camera_profile  # noqa: E402
import camera_profile_write  # noqa: E402

OUTPUT = camera_profile.BUNDLED_STANDARD_FILE

PROFILE_NAME = camera_profile.BUNDLED_STANDARD_LABEL
COPYRIGHT = ("LightTable, GPL-3.0-only. A modelled generic look built by "
             "profiles/build_lighttable_standard.py; not measured from any "
             "camera and not derived from any other profile.")

# --- Tone curve targets -----------------------------------------------------
#
# (scene-linear input, display code output). The output column is on the
# sRGB-encoded axis; the input is listed in linear light because that is how
# exposure is reasoned about. A plain sRGB encode would give, for the same
# inputs: 0.000, 0.026, 0.149, 0.461, 0.700, 0.881, 0.954, 1.000.
TONE_TARGETS = [
    (0.000, 0.000),   # black is a fixed point
    (0.002, 0.020),   # a slight toe: deep shadows sit a touch under the encode
    (0.020, 0.140),   # the toe eases back toward the encode
    (0.180, 0.520),   # middle grey lands at 0.52, a modest lift
    (0.450, 0.775),   # mid-tone slope about 1.07 above grey
    (0.750, 0.915),   # the shoulder begins
    (0.900, 0.968),   # rolling off
    (1.000, 1.000),   # white is a fixed point
]
TONE_SAMPLES = 256

# --- Hue/saturation map targets --------------------------------------------
#
# (anchor hue in degrees, hue shift in degrees, saturation scale) at the
# mid-saturation grid row. The grey row is identity. The fully saturated row
# keeps half the hue shift and no saturation lift, so already-saturated
# colours are not pushed further.
HUE_SAT_TARGETS = [
    (0.0, 2.0, 1.06),     # red, a nudge toward orange
    (30.0, 0.0, 1.03),    # orange and skin: hue held
    (60.0, -3.0, 1.04),   # yellow toward orange
    (90.0, 3.0, 1.05),    # yellow-green toward green
    (120.0, 4.0, 1.06),   # green, slightly toward cyan
    (150.0, 2.0, 1.05),
    (180.0, 0.0, 1.04),   # cyan held
    (210.0, -2.0, 1.06),
    (240.0, -3.0, 1.08),  # blue, slightly toward cyan-blue
    (270.0, -2.0, 1.05),
    (300.0, 0.0, 1.03),   # magenta held
    (330.0, 2.0, 1.04),
]
HUE_SAT_DIMS = (len(HUE_SAT_TARGETS), 3, 1)
SATURATED_HUE_FRACTION = 0.5
ILLUMINANT = 21  # D65, nominal


def srgb_encode(value):
    value = np.asarray(value, dtype=np.float64)
    return np.where(value <= 0.0031308, value * 12.92,
                    1.055 * np.power(np.maximum(value, 0.0), 1.0 / 2.4) - 0.055)


def srgb_decode(value):
    value = np.asarray(value, dtype=np.float64)
    return np.where(value <= 0.04045, value / 12.92,
                    np.power((np.maximum(value, 0.0) + 0.055) / 1.055, 2.4))


def monotone_cubic(xs, ys, samples):
    """Fritsch–Carlson monotone cubic interpolation, in plain numpy.

    The same construction scipy's PchipInterpolator uses, written out so the
    tracked profile can be regenerated and verified without scipy.
    """
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)
    steps = np.diff(xs)
    slopes = np.diff(ys) / steps
    tangents = np.empty_like(ys)
    tangents[0] = slopes[0]
    tangents[-1] = slopes[-1]
    for index in range(1, len(xs) - 1):
        left, right = slopes[index - 1], slopes[index]
        if left * right <= 0.0:
            tangents[index] = 0.0
        else:
            weight_left = 2 * steps[index] + steps[index - 1]
            weight_right = steps[index] + 2 * steps[index - 1]
            tangents[index] = (weight_left + weight_right) / (
                weight_left / left + weight_right / right)
    samples = np.asarray(samples, dtype=np.float64)
    segment = np.clip(np.searchsorted(xs, samples, side="right") - 1, 0, len(xs) - 2)
    x0, x1 = xs[segment], xs[segment + 1]
    y0, y1 = ys[segment], ys[segment + 1]
    m0, m1 = tangents[segment], tangents[segment + 1]
    width = x1 - x0
    t = (samples - x0) / width
    h00 = 2 * t ** 3 - 3 * t ** 2 + 1
    h10 = t ** 3 - 2 * t ** 2 + t
    h01 = -2 * t ** 3 + 3 * t ** 2
    h11 = t ** 3 - t ** 2
    return h00 * y0 + h10 * width * m0 + h01 * y1 + h11 * width * m1


def tone_curve() -> np.ndarray:
    """The (n, 2) linear-to-linear curve the profile stores."""
    inputs = np.array([point[0] for point in TONE_TARGETS])
    outputs = np.array([point[1] for point in TONE_TARGETS])
    codes = np.arange(TONE_SAMPLES, dtype=np.float64) / (TONE_SAMPLES - 1)
    encoded = monotone_cubic(srgb_encode(inputs), outputs, codes)
    encoded = np.clip(encoded, 0.0, 1.0)
    encoded[0], encoded[-1] = 0.0, 1.0
    curve = np.stack((srgb_decode(codes), srgb_decode(encoded)), axis=-1)
    if np.any(np.diff(curve[:, 1]) < 0.0):
        raise ValueError("the tone targets produced a non-monotone curve")
    return curve


def hue_sat_grid():
    """The (value, hue, sat, 3) grid, with identity at grey."""
    def triple(hue_index, sat_index, value_index):
        _, shift, scale = HUE_SAT_TARGETS[hue_index]
        if sat_index == 0:
            return (0.0, 1.0, 1.0)
        if sat_index == 1:
            return (shift, scale, 1.0)
        return (shift * SATURATED_HUE_FRACTION, 1.0, 1.0)
    return camera_profile_write.grid_bytes(*HUE_SAT_DIMS, triple)


def profile_bytes() -> bytes:
    hues = [point[0] for point in HUE_SAT_TARGETS]
    expected = [360.0 * index / len(HUE_SAT_TARGETS) for index in range(len(HUE_SAT_TARGETS))]
    if hues != expected:
        raise ValueError("hue anchors must be evenly spaced from 0 degrees")
    tags = camera_profile_write.profile_tags(
        name=PROFILE_NAME,
        copyright_text=COPYRIGHT,
        hue_sat_dims=HUE_SAT_DIMS,
        hue_sat_map=hue_sat_grid(),
        tone_curve=tone_curve(),
        illuminant1=ILLUMINANT,
        embed_policy=3,
        hue_sat_encoding=camera_profile.ENCODING_LINEAR,
    )
    return camera_profile_write.build_tiff(tags)


def main() -> None:
    payload = profile_bytes()
    OUTPUT.write_bytes(payload)
    print(f"{OUTPUT.relative_to(ROOT)}  {len(payload)} bytes")


if __name__ == "__main__":
    main()
