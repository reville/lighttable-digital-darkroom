#!/usr/bin/env python
# SPDX-License-Identifier: GPL-3.0-only
"""Deterministic Python/Rust parity gate for the spectral colour/tone path.

Run from the app directory:
    MPLCONFIGDIR=/tmp/lighttable-mpl .venv/bin/python tests/engine_parity.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import film_pipeline as fp  # noqa: E402


def synthetic_target(width: int = 128, height: int = 96) -> np.ndarray:
    x = np.linspace(0.002, 0.98, width, dtype=np.float64)
    y = np.linspace(0.05, 1.0, height, dtype=np.float64)[:, None]
    image = np.empty((height, width, 3), dtype=np.float64)
    image[..., 0] = np.clip(x[None, :] * y, 0, 1)
    image[..., 1] = np.clip(np.sqrt(x)[None, :] * y, 0, 1)
    image[..., 2] = np.clip((1 - x * 0.7)[None, :] * y, 0, 1)
    image[12:36, 12:36] = (0.18, 0.18, 0.18)
    image[12:36, 44:68] = (0.70, 0.22, 0.12)
    image[12:36, 76:100] = (0.08, 0.38, 0.78)
    return image


def main() -> None:
    binary = APP / "engine" / "spektrafilm-rs"
    data = APP / "engine" / "data"
    if not binary.is_file() or not data.is_dir():
        raise SystemExit("parity gate requires engine/spektrafilm-rs and engine/data")
    params = fp.clean_params({
        "stock": "kodak_portra_400",
        "paper": "kodak_portra_endura",
        "workflow_mode": "creative",
        "linear_input": True,
        "auto_exposure": False,
        "grain_on": False,
        "halation_on": False,
        "glare_on": False,
        "camera_diffusion_strength": 0.0,
        "scan_softness": 0.0,
        "scan_sharpen": False,
    })
    target = synthetic_target()
    python = fp.render_float(target, params)
    with tempfile.TemporaryDirectory(prefix="lighttable-parity-") as directory:
        directory = Path(directory)
        source = directory / "target.tif"
        output = directory / "rust.png"
        payload = directory / "params.json"
        tifffile.imwrite(source, (target * 65535 + 0.5).astype(np.uint16),
                         photometric="rgb")
        payload.write_text(json.dumps(fp.rust_params_json(params)))
        result = subprocess.run([
            str(binary), "process", str(source), "-o", str(output),
            "--film", params["stock"], "--paper", params["paper"],
            "--data-dir", str(data), "--params", str(payload),
        ], capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise SystemExit((result.stderr or result.stdout)[-1000:])
        rust = np.asarray(Image.open(output).convert("RGB"), dtype=np.float32) / 255.0
    error = np.abs(python - rust) * 255.0
    report = {
        "meanAbsoluteError255": round(float(error.mean()), 4),
        "p95Error255": round(float(np.percentile(error, 95)), 4),
        "maxError255": round(float(error.max()), 4),
        "meanPython": round(float(python.mean()), 6),
        "meanRust": round(float(rust.mean()), 6),
    }
    print(json.dumps(report, indent=2))
    if report["meanAbsoluteError255"] > 1.0:
        raise SystemExit("FAIL: mean Python/Rust error exceeds 1/255")
    if report["p95Error255"] > 2.0:
        raise SystemExit("FAIL: p95 Python/Rust error exceeds 2/255")
    if report["maxError255"] > 10.0:
        raise SystemExit("FAIL: maximum Python/Rust error exceeds 10/255")


if __name__ == "__main__":
    main()
