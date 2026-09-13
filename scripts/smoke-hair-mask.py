#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Exercise the actual packaged offline hair interpreter and pinned model."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from unittest import mock

# Packagers supply their staged runtime through PYTHONPATH. Never accidentally
# replace that with source imports when checking a relocated application.
if not os.environ.get("PYTHONPATH"):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image, ImageOps
from film_lab_ai import hair_segmentation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--image", type=Path)
    args = parser.parse_args()
    if args.image:
        with Image.open(args.image) as source:
            rgb = np.asarray(ImageOps.exif_transpose(source).convert("RGB"))
    else:
        y, x = np.mgrid[:256, :256]
        rgb = np.stack((x, y, (x + y) // 2), axis=-1).astype(np.uint8)
    model = args.model_dir / hair_segmentation.MODEL_FILE
    # This guards Python network use; it is not an OS-level socket audit of
    # native libraries. The direct interpreter needs no SDK/service account.
    with mock.patch("socket.socket.connect", side_effect=AssertionError("Offline smoke attempted network")), \
            mock.patch("socket.socket.connect_ex", side_effect=AssertionError("Offline smoke attempted network")):
        started = time.perf_counter()
        first = hair_segmentation.hair_mask(rgb, model_path=model)
        cold = time.perf_counter() - started
        started = time.perf_counter()
        second = hair_segmentation.hair_mask(rgb, model_path=model)
        warm = time.perf_counter() - started
    assert first.shape == (256, 256) and first.dtype == np.float32
    assert np.isfinite(first).all() and first.min() >= 0 and first.max() <= 1
    assert np.array_equal(first, second), "Repeated inference changed coverage"
    if args.image:
        assert np.count_nonzero(first >= 0.5) >= 64, "Portrait smoke found no substantial hair selection"
    print(json.dumps({"ok": True, "model": hair_segmentation.MODEL_ID,
        "module": str(Path(hair_segmentation.__file__).resolve()),
        "coldMs": round(cold * 1000, 2), "warmMs": round(warm * 1000, 2),
        "hairPixels": int(np.count_nonzero(first >= 0.5))}))


if __name__ == "__main__":
    main()
