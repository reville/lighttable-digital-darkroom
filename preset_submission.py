"""Prepare local submission examples from the three bundled, licensed scenes.

Rendering runs in a bounded subprocess with a separate cache and no catalog.
The user's photographs, preferences, metadata and originals are never inputs.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import zipfile

ROOT = Path(__file__).resolve().parent
SAMPLES = ROOT / "presets" / "samples"
_RENDER_LOCK = threading.Lock()


def build_bundle(preset):
    import preset_io
    import preset_library

    preset_library.validate_look(preset)
    if not _RENDER_LOCK.acquire(blocking=False):
        raise ValueError("An example bundle is already being prepared. Try again shortly.")
    try:
        with tempfile.TemporaryDirectory(prefix="lighttable-preset-examples-") as work:
            environment = dict(os.environ)
            environment.update(LIGHTTABLE_CATALOG="0", LIGHTTABLE_DIR=str(SAMPLES),
                LIGHTTABLE_CATALOG_FILE=str(Path(work) / "unused.sqlite3"),
                LIGHTTABLE_PREFS_FILE=str(Path(work) / "prefs.json"),
                LIGHTTABLE_PRESETS_FILE=str(Path(work) / "presets.json"),
                LIGHTTABLE_INSTANCE_DIR=str(Path(work) / "instances"),
                LIGHTTABLE_CACHE_DIR=str(Path(work) / "cache"),
                LIGHTTABLE_AI_DIR=str(Path(work) / "ai"), MPLCONFIGDIR=str(Path(work) / "mpl"))
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), work],
                input=json.dumps(preset), text=True, capture_output=True, timeout=90,
                cwd=ROOT, env=environment, check=False)
            if result.returncode:
                raise ValueError("The standard examples could not be rendered. Your preset is still saved.")
            filename, _, content = preset_io.export_preset(preset, "lighttable")
            provenance = json.loads((SAMPLES / "provenance.json").read_text())
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(filename, content)
                archive.writestr("photo-credits.json", json.dumps(provenance, indent=2))
                archive.writestr("README.txt", (
                    f"{preset['name']} - LightTable preset submission\n\n"
                    "This bundle was prepared locally. Nothing has been uploaded.\n"
                    "It contains one creative preset and three before/after scene pairs.\n"
                    "The bundled photographs are CC0; their sources are in photo-credits.json.\n"
                    "The example JPEGs contain no EXIF, GPS, camera serial numbers or original RAW files.\n\n"
                    f"Film behavior: {preset['filmMode']}\n"
                    f"Grade controls: {', '.join(preset.get('includedGrade', [])) or 'none'}\n"
                    f"Film controls: {', '.join(preset.get('includedFilm', [])) or 'none'}\n\n"
                    "Before submitting, add your creator name, description and recipe reuse permission.\n"
                    "Only submit presets you have permission to redistribute. Recipe permission is\n"
                    "separate from photo display permission. Existing author credit is retained.\n"
                    "Attach this ZIP to the gallery submission form; a maintainer reviews it before publishing.\n"
                ))
                for photo in provenance:
                    for stage in ("before", "after"):
                        name = f"{Path(photo['file']).stem}-{stage}.jpg"
                        path = Path(work) / name
                        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                            raise ValueError("A rendered example is unavailable or too large")
                        archive.write(path, "examples/" + name)
            return {"filename": Path(filename).stem + "-submission.zip",
                    "contentType": "application/zip", "encoding": "base64",
                    "content": base64.b64encode(buffer.getvalue()).decode("ascii")}
    except subprocess.TimeoutExpired as error:
        raise ValueError("The example rendering timed out. Try preparing the submission again.") from error
    finally:
        _RENDER_LOCK.release()


def render_examples(work):
    import preset_library
    import server

    preset = preset_library.validate_look(json.load(sys.stdin))
    sources = json.loads((SAMPLES / "provenance.json").read_text())
    for photo in sources:
        source = SAMPLES / photo["file"]
        if source.parent != SAMPLES or hashlib.sha256(source.read_bytes()).hexdigest() != photo["sha256"]:
            raise ValueError("Bundled example checksum mismatch")
        for stage in ("before", "after"):
            state = preset_library.look_patch(preset) if stage == "after" else {"params": {"profile_enabled": False}, "grade": {}}
            # Supply every group so catalog/session state can never contribute.
            state.update(masks=[], heals=[], optics={}, crop=None)
            if preset["filmMode"] == "preserve":
                state.setdefault("params", {})["profile_enabled"] = False
            image = server.program_render_image({"name": photo["file"], "state": state,
                "w": 960, "engine": "rs", "client": "preset-submission"})
            image.convert("RGB").save(Path(work) / f"{source.stem}-{stage}.jpg", "JPEG", quality=90)


if __name__ == "__main__":
    render_examples(sys.argv[1])
