#!/usr/bin/env python3
"""Prepare the pinned offline hair model for source tests and release builds.

No photo is opened or uploaded. Runtime inference never calls this downloader.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "film_lab_ai"))
from hair_model_asset import (MODEL_FILE, MODEL_BYTES, MODEL_SHA256,
    MODEL_URL, MODEL_ID, MODEL_CARD, LICENSE_FILE, LICENSE_SHA256)


def valid_model(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size != MODEL_BYTES:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == MODEL_SHA256


def prepare(target: Path) -> Path:
    license_bytes = (ROOT / "film_lab_ai" / "licenses" / LICENSE_FILE).read_bytes()
    if hashlib.sha256(license_bytes).hexdigest() != LICENSE_SHA256:
        raise ValueError("Hair model license text failed its integrity check")
    target.mkdir(parents=True, exist_ok=True)
    model = target / MODEL_FILE
    temporary = None
    if not valid_model(model):
        try:
            deadline = time.monotonic() + 180
            with urllib.request.urlopen(MODEL_URL, timeout=30) as response, \
                    tempfile.NamedTemporaryFile(dir=target, prefix=".hair-", delete=False) as stream:
                temporary = Path(stream.name)
                received = 0
                while received <= MODEL_BYTES:
                    if time.monotonic() > deadline:
                        raise TimeoutError("Hair model download exceeded three minutes")
                    block = response.read(min(128 * 1024, MODEL_BYTES + 1 - received))
                    if not block:
                        break
                    stream.write(block)
                    received += len(block)
            if not valid_model(temporary):
                raise ValueError("Hair model download failed its integrity check")
            os.replace(temporary, model)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    (target / LICENSE_FILE).write_bytes(license_bytes)
    (target / "hair-model.json").write_text(json.dumps({
        "id": MODEL_ID, "file": MODEL_FILE, "bytes": MODEL_BYTES,
        "sha256": MODEL_SHA256, "url": MODEL_URL, "modelCard": MODEL_CARD,
        "license": "Apache-2.0", "licenseFile": LICENSE_FILE,
        "input": {"shape": [1, 256, 256, 3], "mean": 127.5, "std": 127.5},
        "output": {"shape": [1, 256, 256, 6], "activation": "softmax", "hairChannel": 1},
    }, indent=2) + "\n")
    return model


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--into", type=Path, default=ROOT / "scripts" / "models")
    args = parser.parse_args()
    print(f"Verified offline hair model: {prepare(args.into)}")
