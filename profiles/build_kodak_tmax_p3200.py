#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build LightTable's measured Kodak T-MAX P3200 Rust profile.

The published graphs are necessarily digitized at finite resolution. The
control points below are the auditable source; interpolation only expands them
to the 5 nm / 256-sample schema expected by spektrafilm-rs.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
from scipy.interpolate import PchipInterpolator


KODAK_DATASHEET = (
    "https://business.kodakmoments.com/sites/default/files/files/"
    "products/F4001.pdf"
)
OUTPUT = Path(__file__).with_name("kodak_tmax_p3200.json")

# F-4001 page 7, the curve measured at 1.0 density above D-min. A constant
# vertical offset normalizes the graph to the sensitivity scale used by the
# bundled B&W profiles; it does not change relative spectral response.
SPECTRAL_CONTROL_POINTS = [
    (420.0, -0.70), (440.0, -0.70), (460.0, -0.76),
    (480.0, -0.84), (500.0, -0.90), (525.0, -0.84),
    (550.0, -0.88), (575.0, -0.98), (600.0, -1.02),
    (615.0, -1.35), (630.0, -1.42), (640.0, -1.55),
    (650.0, -1.90), (660.0, -2.45), (670.0, -2.90),
]

# F-4001 page 8, daylight / T-MAX Developer / small tank / 20 C. These points
# follow Kodak's published 12-minute characteristic curve and subtract the
# measured 0.30 base-plus-fog density. The horizontal shift maps physical log
# exposure onto spektrafilm's normalized curve domain without changing shape.
DENSITY_CONTROL_POINTS = [
    (-1.20, 0.00), (-0.90, 0.01), (-0.60, 0.07),
    (-0.40, 0.17), (-0.20, 0.32), (0.30, 0.65),
    (0.80, 1.05), (1.30, 1.37), (1.80, 1.66),
    (2.30, 1.93), (2.80, 2.20),
]


def build_profile() -> dict:
    wavelengths = np.arange(380.0, 780.0 + 5.0, 5.0)
    spectral_x, spectral_y = np.asarray(SPECTRAL_CONTROL_POINTS).T
    spectral_interp = PchipInterpolator(spectral_x, spectral_y)
    log_sensitivity = []
    for wavelength in wavelengths:
        value = (float(spectral_interp(wavelength))
                 if spectral_x[0] <= wavelength <= spectral_x[-1] else None)
        log_sensitivity.append([value])

    log_exposure = np.linspace(-3.0, 4.0, 256)
    density_x, density_y = np.asarray(DENSITY_CONTROL_POINTS).T
    density_interp = PchipInterpolator(density_x, density_y)
    density = np.where(
        log_exposure < density_x[0], density_y[0],
        np.where(log_exposure > density_x[-1], density_y[-1],
                 density_interp(log_exposure)),
    )
    density = np.maximum.accumulate(np.clip(density, 0.0, None))

    return {
        "metadata": {
            "version": "0.1.0",
            "copyright": (
                "Profile integration Copyright (c) 2026 Nicholas Reville. "
                "Kodak data and trademarks remain property of their holders."
            ),
            "created": date(2026, 9, 1).isoformat(),
            "license": (
                "LightTable profile integration licensed under CC BY-SA 4.0. "
                "Source measurements are cited below."
            ),
            "citation": KODAK_DATASHEET,
            "datasource": (
                "Relative spectral sensitivity and characteristic-curve "
                "control points digitized from Kodak Professional T-MAX "
                "P3200 technical data F-4001 (March 2018), pages 7-8. The "
                "profile represents EI 3200 developed in T-MAX Developer "
                "for 12 minutes at 20 C in a small tank. Kodak publishes "
                "the corresponding 12-minute characteristic curve. RMS "
                "granularity 18 is retained in LightTable catalog notes but "
                "is not converted directly into the engine particle model."
            ),
        },
        "info": {
            "stock": "kodak_tmax_p3200",
            "name": "Kodak Professional T-MAX P3200 (EI 3200)",
            "type": "negative",
            "support": "film",
            "stage": "filming",
            "use": "still",
            "antihalation": "strong",
            "target_print": "kodak_2302",
            "channel_model": "bw",
            "densitometer": "diffuse_visual",
            "log_sensitivity_density_over_min": 1.0,
            "reference_illuminant": "D55",
            "viewing_illuminant": "D50",
            "default_grain_um2": 1.20,
        },
        "data": {
            "wavelengths": wavelengths.tolist(),
            "log_sensitivity": log_sensitivity,
            "hanatos2025_adaptation_window_params": None,
            "hanatos2025_adaptation_surface_params": None,
            "channel_density": [[1.0] for _ in wavelengths],
            "base_density": [[0.30] for _ in wavelengths],
            "midscale_neutral_density": None,
            "log_exposure": log_exposure.tolist(),
            "density_curves": [[float(value)] for value in density],
            "density_curves_layers": None,
            "density_curves_model": None,
            "development_time": [12.0],
        },
    }


if __name__ == "__main__":
    OUTPUT.write_text(json.dumps(build_profile(), indent=2,
                                 allow_nan=False) + "\n")
    print(OUTPUT)
