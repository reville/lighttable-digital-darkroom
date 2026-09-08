"""Numerical proof of the production Metal renderer's submitted display frames.

Requires macOS, swiftc, Metal and a graphical login. Missing prerequisites are a
blocking failure, never a skipped/pass result. This is a real renderer/window,
not the complete LightTable app: it cannot prove bridge wiring or UI placement.
The Swift helper reads the drawable captured before each production render,
after GPU completion; it never calls the offscreen snapshot() renderer.
"""
from __future__ import annotations

import functools
import http.server
import json
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from unittest import mock
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import grade
import edits
from processing_support import compare_images, grade_cases, target_rgb8
from processing_edit_cases import edit_cases, edit_sources


def _blocked(message):
    return [{"name": "native-renderer-availability", "status": "blocked", "error": message}]


def run(output_dir: Path) -> list[dict]:
    """Compare grade.apply CLI/export pixels to genuine submitted Metal frames."""
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    swiftc = shutil.which("swiftc")
    if sys.platform != "darwin" or not swiftc:
        return _blocked("Native display proof requires macOS with swiftc, Metal, and a graphical session")
    inputs = output_dir / "inputs"
    captures = output_dir / "submitted-drawables"
    inputs.mkdir(exist_ok=True)
    captures.mkdir(exist_ok=True)
    source_a = target_rgb8()
    source_b = np.ascontiguousarray(255 - source_a[:, ::-1, [2, 0, 1]])
    portrait = np.ascontiguousarray(np.rot90(source_a))
    for name, pixels in (("a", source_a), ("b", source_b), ("portrait", portrait)):
        Image.fromarray(pixels).save(inputs / f"{name}.png")
    height, width = source_a.shape[:2]
    rgba = np.concatenate((source_a, np.full((height, width, 1), 255, np.uint8)), axis=2)
    (inputs / "a.flra").write_bytes(struct.pack("<4sIII", b"FLRA", width, height, width * 4) + rgba.tobytes())
    actions, references = [], {}

    def add(name, source, values, kind="grade", **extra):
        name = "native-" + name
        h, w = source.shape[:2]
        actions.append({"name": name, "width": w, "height": h,
                        "grade": values, "type": kind, **extra})
        references[name] = grade.apply(source.astype(np.float32) / 255, values)

    add("png-identity", source_a, {}, "load", surface={"url": "a.png"})
    add("flra-identity", source_a, {}, "load", surface={"url": "a.flra", "format": "rgba8",
        "width": width, "height": height, "rowBytes": width * 4, "headerBytes": 16})
    for case in grade_cases():
        add(case["name"], source_a, case["grade"])

    navigation_grade = {"exposure": 0.4, "contrast": 0.2}
    for name, source, url, values in (
        ("navigation-a", source_a, "a.png", navigation_grade),
        ("navigation-b", source_b, "b.png", {"saturation": -0.4}),
        ("navigation-a-restored", source_a, "a.png", navigation_grade),
    ):
        add(name, source, values, "load", surface={"url": url})
    for index, position in enumerate((0.1, 0.9, 0.25, 0.75, 0.5, 0.98, 0.02, 1.0, 0.0)):
        name = f"compare-{index}-{position:g}"
        add(name, source_a, navigation_grade, "compare", position=position)
        before = ((np.arange(width) + 0.5) / width <= position) if position else np.zeros(width, dtype=bool)
        references["native-" + name][:, before] = source_b[:, before].astype(np.float32) / 255
    add("portrait-orientation-and-size", portrait, {}, "load", surface={"url": "portrait.png"})
    add("landscape-size-restored", source_a, {}, "load", surface={"url": "a.png"})
    # Exercise the server's real bake decision, then the actual Metal display
    # payload. Without that route, Heal/Remove or local detail can accidentally
    # be tested against an already-corrected input the app never receives.
    import server as app_server
    sources = edit_sources()
    cache = output_dir / "route-cache"
    (cache / "render").mkdir(parents=True, exist_ok=True)
    with mock.patch.object(app_server, "CACHE", cache), \
         mock.patch.object(app_server, "file_key", return_value="processing-fixture"), \
         mock.patch.object(app_server, "exif_for", return_value={}), \
         mock.patch.object(app_server, "prune_render_cache_throttled"):
        for index, case in enumerate(edit_cases()):
            pixels = sources[case["fixture"]]
            h, w = pixels.shape[:2]
            key = f"{index:032x}"
            base_path = cache / "render" / f"{key}.rgba"
            app_server.write_native_surface(base_path, pixels)
            response = app_server.apply_preview_edits(
                {"key": key, "native": {"url": f"/api/render/native?key={key}"}},
                "fixture.png", w, {}, case.get("optics"), case.get("heals"), native=True,
                grade_values=case.get("grade"), masks=case.get("masks"))
            name = "native-edits-" + case["name"]
            shutil.copy(cache / "render" / f"{response['key']}.rgba", inputs / f"{name}.flra")
            baked = response.get("baseEditsBaked", False)
            grade_baked = response.get("gradeEditsBaked", False)
            actions.append({"name": name, "type": "processed", "width": w, "height": h,
                "surface": {"url": f"{name}.flra", "format": "rgba8", "width": w,
                            "height": h, "rowBytes": w * 4, "headerBytes": 16},
                "optics": {} if baked else case.get("optics", {}),
                "heals": [] if baked else case.get("heals", []),
                "grade": {} if grade_baked else case.get("grade", {}),
                "maskPayload": {"masks": []}, "recipe": case,
                "baseEditsBaked": baked, "gradeEditsBaked": grade_baked})
            references[name] = edits.apply_masks(grade.apply(edits.apply_base(
                pixels.astype(np.float32) / 255, case.get("optics"), case.get("heals")),
                case.get("grade")), case.get("masks"))
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(actions, indent=2))

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
        functools.partial(QuietHandler, directory=str(inputs)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="lighttable-native-processing-") as temporary:
            temporary = Path(temporary)
            bundle = temporary / "ProcessingPreview.app" / "Contents"
            resources = bundle / "Resources"
            resources.mkdir(parents=True)
            executable = bundle / "MacOS" / "ProcessingPreview"
            executable.parent.mkdir()
            (bundle / "Info.plist").write_bytes(plistlib.dumps({
                "CFBundleExecutable": "ProcessingPreview", "CFBundlePackageType": "APPL",
                "CFBundleIdentifier": "com.lighttable.test.processing-preview",
            }))
            shutil.copy(ROOT / "app" / "NativePreview.metal", resources)
            compiled = subprocess.run([swiftc, "-swift-version", "5", "-module-cache-path",
                str(temporary / "module-cache"), str(ROOT / "app" / "NativePreview.swift"),
                str(Path(__file__).with_suffix(".swift")), "-o", str(executable)],
                capture_output=True, text=True, timeout=120)
            (output_dir / "compile.log").write_text(compiled.stdout + compiled.stderr)
            if compiled.returncode:
                return _blocked(f"Production NativePreview compilation failed; see {output_dir / 'compile.log'}")
            executed = subprocess.run([str(executable), f"http://127.0.0.1:{server.server_port}/",
                str(manifest), str(captures)], capture_output=True, text=True, timeout=90)
            (output_dir / "renderer.log").write_text(executed.stdout + executed.stderr)
            if executed.returncode:
                return _blocked(f"Native display capture failed: {executed.stdout}{executed.stderr}")
    except subprocess.TimeoutExpired as error:
        return _blocked(f"Native renderer build/capture timed out after {error.timeout} seconds")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    evidence = json.loads((captures / "drawables.json").read_text())
    records = []
    frames = {frame["name"]: frame for frame in evidence["frames"]}
    for action in actions:
        name, h, w = action["name"], action["height"], action["width"]
        raw = np.frombuffer((captures / (name + ".bgra")).read_bytes(), dtype=np.uint8)
        actual = raw.reshape(h, w, 4)[..., [2, 1, 0]].astype(np.float32) / 255
        # Fixed shared tolerances are in 8-bit code values: mean .75, p95 2,
        # max 5. No image registration, trimming, color fitting, or per-case
        # tolerance escalation is allowed to hide orientation/processing errors.
        record = compare_images(name, references[name], actual, output_dir)
        record.update(reference="grade.apply from exact PNG/FLRA RGB8 input",
                      proof="production renderer submitted MTKView drawable",
                      device=evidence["device"], display_evidence=frames[name],
                      grade=action["grade"])
        if "recipe" in action:
            record.update(reference="Python export order: optics/retouch, grade, ordered masks",
                          recipe=action["recipe"],
                          baseEditsBaked=action["baseEditsBaked"],
                          gradeEditsBaked=action["gradeEditsBaked"])
        if name == "native-navigation-a-restored" and not frames[name]["texture_cache_hit"]:
            record.update(status="fail", error="A/B/A navigation did not exercise cached A restoration")
        records.append(record)
    (output_dir / "results.json").write_text(json.dumps(records, indent=2))
    return records


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    results = run(args.output_dir)
    print(json.dumps(results, indent=2))
    raise SystemExit(0 if results and all(row["status"] == "pass" for row in results) else 1)
