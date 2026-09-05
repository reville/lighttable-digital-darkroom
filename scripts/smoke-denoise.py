#!/usr/bin/env python3
"""Release smoke: load the bundled model and denoise one deterministic tile."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import enhance_workflow


def main() -> None:
    rng = np.random.default_rng(20260903)
    noisy = np.clip(
        np.full((512, 512, 3), 0.45, dtype=np.float32)
        + rng.normal(0.0, 0.08, (512, 512, 3)).astype(np.float32),
        0.0, 1.0)
    result = enhance_workflow.run_model(
        noisy, "denoise", strength=1.0, tile=512, overlap=64)
    before = float(np.std(noisy))
    after = float(np.std(result))
    if result.shape != noisy.shape or not np.isfinite(result).all():
        raise SystemExit("bundled denoise returned an invalid tile")
    if after >= before * 0.5:
        raise SystemExit(
            f"bundled denoise did not halve flat-field noise ({before} -> {after})")
    print(json.dumps({
        "ok": True, "shape": list(result.shape),
        "noiseStdBefore": round(before, 6),
        "noiseStdAfter": round(after, 6),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
