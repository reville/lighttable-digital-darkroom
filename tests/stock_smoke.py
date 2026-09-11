#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Render a standard target through every selectable Rust film profile."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import film_pipeline as fp


def test_chart(width: int = 128, height: int = 96) -> np.ndarray:
    x = np.linspace(0.02, 0.98, width, dtype=np.float64)
    gradient = np.repeat(x[None, :, None], height, axis=0)
    image = np.repeat(gradient, 3, axis=2)
    patches = [
        (0.90, 0.08, 0.06), (0.08, 0.75, 0.12), (0.06, 0.18, 0.92),
        (0.95, 0.82, 0.08), (0.78, 0.08, 0.82), (0.05, 0.82, 0.88),
    ]
    patch_width = width // len(patches)
    for index, color in enumerate(patches):
        x0 = index * patch_width
        x1 = width if index == len(patches) - 1 else (index + 1) * patch_width
        image[:height // 3, x0:x1] = color
    return image


def run() -> list[dict]:
    binary = fp.APP / "engine" / "spektrafilm-rs"
    data_dir = fp.APP / "engine" / "data"
    if not binary.is_file():
        raise RuntimeError(f"missing Rust engine: {binary}")
    results = []
    with tempfile.TemporaryDirectory(prefix="lighttable-stock-smoke-") as temp:
        root = Path(temp)
        source = root / "chart.tif"
        tifffile.imwrite(source, (test_chart() * 65535 + 0.5).astype(np.uint16))
        for profile in fp.FILM_PROFILES:
            ident = profile["id"]
            paper = fp.default_paper(ident)
            paper_development = (0.0 if profile["type"] == "positive" else
                                 fp.PROFILE_BY_ID[paper]["defaultDevelopmentTime"])
            params = fp.rust_params_json({
                "stock": ident,
                "paper": paper,
                "development_time": profile["defaultDevelopmentTime"],
                "print_development_time": paper_development,
                "exposure_ev": profile.get("defaultExposureEv", 0.0),
            })
            params_path = root / f"{ident}.json"
            output_path = root / f"{ident}.png"
            params_path.write_text(json.dumps(params))
            command = [
                str(binary), "process", str(source), "-o", str(output_path),
                "--film", ident, "--data-dir", str(data_dir),
                "--params", str(params_path),
            ]
            if profile["type"] == "positive":
                command.append("--scan-film")
                route = "direct scan"
            else:
                command += ["--paper", paper]
                route = paper
            started = time.perf_counter()
            completed = subprocess.run(command, capture_output=True, text=True,
                                       timeout=120)
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"{ident} failed: {(completed.stderr or completed.stdout)[-500:]}"
                )
            image = np.asarray(Image.open(output_path).convert("RGB"))
            spread = int(image.max()) - int(image.min())
            if spread < 16:
                raise AssertionError(f"{ident} collapsed to {spread} code values")
            channel_delta = float(np.abs(
                image.astype(np.int16) - image[:, :, :1].astype(np.int16)
            ).max())
            if profile["channelModel"] == "bw" and channel_delta > 3:
                raise AssertionError(
                    f"{ident} B&W render has {channel_delta:.1f} code channel delta"
                )
            results.append({
                "stock": ident,
                "route": route,
                "developmentMinutes": profile["defaultDevelopmentTime"] or None,
                "printDevelopmentMinutes": paper_development or None,
                "exposureEv": profile.get("defaultExposureEv", 0.0),
                "mean": round(float(image.mean()), 2),
                "stddev": round(float(image.std()), 2),
                "range": spread,
                "maxChannelDelta": channel_delta,
                "ms": elapsed_ms,
            })
    return results


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
