#!/usr/bin/env python3
"""Publish the bundled looks as static catalog sources with real LightTable renders.

Run with the app's Python environment and --site /path/to/lighttable-site.
Only redistributable, provenance-recorded fixture photographs are published.
No photographs or settings from the user's catalog are read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True, type=Path)
    args = parser.parse_args()
    samples = ROOT / "tests/fixtures/photos"
    work = ROOT / ".build/preset-gallery"
    work.mkdir(parents=True, exist_ok=True)
    os.environ.update(LIGHTTABLE_CATALOG="0", LIGHTTABLE_DIR=str(samples),
        LIGHTTABLE_CACHE_DIR=str(work / "cache"), LIGHTTABLE_PREFS_FILE=str(work / "prefs.json"),
        LIGHTTABLE_PRESETS_FILE=str(work / "saved.json"), LIGHTTABLE_INSTANCE_DIR=str(work / "instances"))
    # This build has a hard wall-clock bound even if a rendering subprocess stalls.
    if hasattr(signal, "alarm"):
        signal.alarm(1800)
    import server
    import preset_library
    import numpy as np

    site = args.site.resolve()
    records = site / "presets/source"
    images = site / "presets/images"
    records.mkdir(parents=True, exist_ok=True)
    images.mkdir(parents=True, exist_ok=True)
    provenance = json.loads((samples / "provenance.json").read_text())
    presets = preset_library.builtin_presets()
    image_records, before = [], {}

    def save_image(image, label):
        temporary = work / "render.webp"
        # Fresh encoding strips EXIF/GPS/serial numbers and other source metadata.
        image.convert("RGB").save(temporary, "WEBP", quality=86, method=6)
        data = temporary.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        destination = images / f"{digest[:24]}.webp"
        destination.write_bytes(data)
        array = np.asarray(image.convert("RGB"))
        image_records.append({"path": str(destination.relative_to(site)), "sha256": digest,
            "width": image.width, "height": image.height, "label": label,
            "meanRGB": np.mean(array, axis=(0, 1)).round(2).tolist()})
        return "/" + destination.relative_to(site).as_posix()

    for photo in provenance:
        source = samples / photo["file"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != photo["sha256"]:
            raise ValueError(f"Fixture source changed: {source.name}")
        image = server.program_render_image({"name": photo["file"], "w": 960,
            "state": {"params": {"profile_enabled": False}, "grade": {}}, "engine": "rs"})
        before[photo["file"]] = save_image(image, "Original · " + photo["file"])
    for preset in presets:
        recipe = {k: v for k, v in preset.items() if k != "collection"}
        preset_path = site / "presets/files" / recipe["id"] / (recipe["version"] + ".ltpreset")
        preset_path.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps({"format": "LightTable Preset", "version": 3,
                           "presets": [recipe]}, indent=2) + "\n").encode()
        preset_path.write_bytes(data)
        previews = []
        for photo in provenance:
            state = preset_library.look_patch(preset)
            image = server.program_render_image({"name": photo["file"], "w": 960,
                "state": state, "engine": "rs", "client": "preset-gallery-builder"})
            previews.append({"label": photo["file"].replace(".jpg", "").replace("-", " ").title(),
                "before": before[photo["file"]], "after": save_image(image, recipe["name"] + " · " + photo["file"]),
                "credit": {"name": "CC0 photograph", "url": photo["source_url"], "license": "CC0-1.0"}})
        if "Portrait" in recipe["tags"]:
            previews.sort(key=lambda p: p["label"] != "Portrait")
        record = {k: recipe[k] for k in ("id", "name", "version", "description", "tags", "author", "license", "filmMode")}
        record.update(schemaVersion=3, capabilities=["look-v1"],
            file={"url": "/" + preset_path.relative_to(site).as_posix(),
                  "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)},
            previews=previews, pageUrl=f"https://lighttable.app/presets/{recipe['id']}/",
            publishedAt="2026-09-08", featured=True)
        (records / (recipe["id"].replace("/", "-") + ".json")).write_text(json.dumps(record, indent=2) + "\n")
        print("Rendered " + recipe["name"], flush=True)
    proof = {"renderer": "LightTable program_render_image", "sourceRevision": subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "sourceDirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()),
        "engineSha256": hashlib.sha256((ROOT / "engine/lighttable-engine").read_bytes()).hexdigest(),
        "recipeSha256": hashlib.sha256((ROOT / "presets/builtin.json").read_bytes()).hexdigest(),
        "sources": provenance, "images": image_records}
    (site / "presets/render-provenance.json").write_text(json.dumps(proof, indent=2) + "\n")
    print(f"Wrote {len(presets)} presets and {len(image_records)} real renders", flush=True)


if __name__ == "__main__":
    main()
