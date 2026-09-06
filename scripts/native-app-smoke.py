#!/usr/bin/env python3
"""Run layered real-product photo journeys against a built LightTable.app."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = ROOT / "demo-assets" / "cc0-raw" / "files"
DEFAULT_FIXTURES = (
    ROOT / "tests" / "fixtures" / "photos" / "field.jpg",
    ROOT / "tests" / "fixtures" / "photos" / "still-life.jpg",
)
CURATED_RAW_NAMES = (
    "01-canon-eos-80d-city-tree.CR2",
    "19-nikon-d200-rolling-hills.NEF",
    "03-fujifilm-xq2-harbor-ferry.RAF",
    "09-google-pixel-7-pro-succulents.DNG",
    "05-sony-rx100-vii-tropical-homestead.ARW",
    "17-canon-eos-r100-window-flowers.CR3",
    "11-om-system-tg-7-forest-floor.ORF",
    "14-panasonic-fz28-spring-backyard.RW2",
)
REQUIRED_PR_STEPS = {
    "edit-recovery",
    "navigate-photo",
    "rapid-navigation",
    "lens-profile-metal",
    "return-to-metal",
    "post-fallback-navigation",
    "sampling-pickers",
    "film-off",
    "film-on",
    "slider-adjustment",
    "compare-on",
    "compare-off",
    "zoom-actual",
    "zoom-fit",
    "export-photo",
    "visibility-controls-on",
}
RAW_EXTENSIONS = {"arw", "cr2", "cr3", "dng", "nef", "orf", "raf", "rw2"}
PERFORMANCE_FIELDS = (
    "queueMs",
    "requestMs",
    "serverMs",
    "residentMs",
    "gpuMs",
    "imageDecodeMs",
    "textureUploadMs",
    "paintAfterUploadMs",
    "nativeFetchMs",
    "nativeGpuMs",
    "nativeTotalMs",
    "totalMs",
)


def fixtures_for_layer(layer: str) -> tuple[Path, ...]:
    if layer in {"pr", "package"}:
        return DEFAULT_FIXTURES
    if layer == "raw-curated":
        return tuple(RAW_ROOT / name for name in CURATED_RAW_NAMES)
    if layer == "raw-full":
        return tuple(sorted(path for path in RAW_ROOT.iterdir() if path.is_file()))
    raise ValueError(f"unknown journey layer: {layer}")


def _is_lfs_pointer(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size > 4096:
        return False
    return path.read_bytes().startswith(b"version https://git-lfs.github.com/spec/v1")


def validate_fixtures(fixtures: tuple[Path, ...], layer: str) -> None:
    missing = [str(path) for path in fixtures if not path.is_file()]
    pointers = [str(path) for path in fixtures if _is_lfs_pointer(path)]
    if missing:
        raise RuntimeError(f"missing real-photo journey fixtures: {missing}")
    if pointers:
        raise RuntimeError(
            f"{layer} requires Git LFS photo contents, but found pointers: {pointers}"
        )
    if layer.startswith("raw-"):
        extensions = {path.suffix.lower().lstrip(".") for path in fixtures}
        if layer == "raw-curated" and extensions != RAW_EXTENSIONS:
            raise RuntimeError(
                f"curated RAW layer must cover all supported formats: {extensions}"
            )


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(round(
        (len(ordered) - 1) * percentile
    ))))
    return round(ordered[rank], 3)


def performance_summary(payload: dict, process_wall_ms: float) -> dict:
    journey = payload.get("journey") or {}
    steps = journey.get("steps") or []
    renders = [
        step.get("render") for step in steps if isinstance(step.get("render"), dict)
    ] + [
        entry for entry in payload.get("renders", []) if isinstance(entry, dict)
    ]
    render_stats = {}
    for field in PERFORMANCE_FIELDS:
        values = [float(entry[field]) for entry in renders
                  if isinstance(entry.get(field), (int, float))]
        if values:
            render_stats[field] = {
                "count": len(values),
                "median": round(statistics.median(values), 3),
                "p95": _percentile(values, 0.95),
                "max": round(max(values), 3),
            }
    step_values = [float(step["durationMs"]) for step in steps
                   if isinstance(step.get("durationMs"), (int, float))]
    return {
        "processWallMs": round(process_wall_ms, 3),
        "journeyStepTotalMs": round(sum(step_values), 3),
        "journeyStepMedianMs": round(statistics.median(step_values), 3)
        if step_values else None,
        "journeyStepP95Ms": _percentile(step_values, 0.95),
        "steps": [
            {"name": step.get("name"), "image": step.get("image"),
             "durationMs": round(float(step.get("durationMs", 0)), 3)}
            for step in steps
        ],
        "render": render_stats,
    }


def validate_benchmark(
    payload: dict, require_journey: bool = False, layer: str = "pr"
) -> dict:
    if payload.get("error"):
        raise RuntimeError(f"native photo journey failed: {payload['error']}")
    if payload.get("windowConnected") is not True:
        raise RuntimeError("native photo journey never connected a visible app window")
    renders = payload.get("renders")
    if not isinstance(renders, list) or not renders:
        raise RuntimeError("native photo journey produced no render results")
    failures = [entry for entry in renders if entry.get("error")]
    if failures:
        raise RuntimeError(f"native photo journey reported errors: {failures}")
    presentations = {entry.get("presentation") for entry in renders}
    if presentations != {"native-metal"}:
        raise RuntimeError(
            f"expected only native-metal presentations, received {presentations}"
        )
    journey = payload.get("journey")
    if require_journey:
        if not isinstance(journey, dict):
            raise RuntimeError("native photo journey did not report its user steps")
        final_render = journey.get("finalState", {}).get("render", {})
        if final_render.get("state") != "ready":
            raise RuntimeError("native photo journey did not end in ready state")
        if final_render.get("backend") != "native-metal":
            raise RuntimeError("native photo journey did not end in Metal")
        if final_render.get("name") != journey.get("finalState", {}).get("current"):
            raise RuntimeError("native photo journey ended with the wrong photo displayed")
        if layer in {"pr", "package"}:
            if journey.get("startImage") == journey.get("navigatedImage"):
                raise RuntimeError("native photo journey did not navigate to another photo")
            if journey.get("correctedPresentation") != "native-metal":
                raise RuntimeError("native photo journey did not keep corrected previews in Metal")
            if journey.get("returnPresentation") != "native-metal":
                raise RuntimeError("native photo journey did not return to Metal")
            controls = journey.get("visibilityControls") or {}
            if (controls.get("clipping") is not True
                    or controls.get("whiteBalance") is not True
                    or controls.get("webglFallback") is not False):
                raise RuntimeError(
                    "clipping and white-balance controls did not preserve Metal"
                )
            pickers = journey.get("samplingPickers") or {}
            if (pickers.get("pointColor") is not True
                    or pickers.get("maskColor") is not True):
                raise RuntimeError("color sampling pickers did not preserve Metal")
            steps = {entry.get("name") for entry in journey.get("steps", [])}
            missing_steps = sorted(REQUIRED_PR_STEPS - steps)
            if missing_steps:
                raise RuntimeError(f"native photo journey skipped steps: {missing_steps}")
            exported = journey.get("export", {})
            if exported.get("total") != 1 or exported.get("done") != 1:
                raise RuntimeError("native photo journey did not export exactly one photo")
        else:
            extensions = {
                Path(name).suffix.lower().lstrip(".")
                for name in journey.get("images", [])
            }
            if layer == "raw-curated" and extensions != RAW_EXTENSIONS:
                raise RuntimeError(
                    f"RAW journey did not open every supported format: {extensions}"
                )
            recovery = journey.get("recovery")
            if not isinstance(recovery, dict) or not recovery.get("recoveredImage"):
                raise RuntimeError("RAW journey did not prove corrupt-file recovery")
    return {
        "image": payload.get("image"),
        "renders": len(renders),
        "presentation": "native-metal",
        "requestedWidth": payload.get("requestedWidth"),
        "windowConnected": True,
        "journey": journey,
        "interactions": payload.get("interactions"),
        "screenshot": payload.get("screenshot"),
    }


def require_bundle(app: Path) -> tuple[Path, Path]:
    native = app / "Contents" / "MacOS" / "LightTable"
    cli = app / "Contents" / "MacOS" / "lighttable-cli"
    if not native.is_file() or not os.access(native, os.X_OK):
        raise RuntimeError(f"missing native app executable: {native}")
    if not cli.is_file() or not os.access(cli, os.X_OK):
        raise RuntimeError(f"missing command wrapper: {cli}")
    if os.path.samefile(native, cli):
        raise RuntimeError("native app executable and command wrapper are the same file")
    file_type = subprocess.run(
        ["/usr/bin/file", "-b", str(native)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if "Mach-O" not in file_type:
        raise RuntimeError(f"app executable is not Mach-O: {file_type.strip()}")
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        check=True,
        capture_output=True,
        text=True,
    )
    return native, cli


def smoke_environment(
    temporary: Path,
    photos: Path,
    output: Path,
    screenshot: Path,
    layer: str = "pr",
    image_names: tuple[str, ...] = (),
    corrupt_name: str | None = None,
) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "LIGHTTABLE_DIR": str(photos),
            "LIGHTTABLE_CATALOG": "0",
            "LIGHTTABLE_CATALOG_FILE": str(temporary / "catalog/library.sqlite3"),
            "LIGHTTABLE_CACHE_DIR": str(temporary / "cache"),
            "LIGHTTABLE_PREFS_FILE": str(temporary / "prefs.json"),
            "LIGHTTABLE_PRESETS_FILE": str(temporary / "presets.json"),
            "LIGHTTABLE_AI_DIR": str(temporary / "ai"),
            "LIGHTTABLE_INSTANCE_DIR": str(temporary / "instances"),
            "LIGHTTABLE_SERVER_LOG": str(temporary / "server.log"),
            "LIGHTTABLE_NATIVE_PERF_LOG": str(temporary / "native-perf.jsonl"),
            "LIGHTTABLE_NATIVE_BENCHMARK_OUTPUT": str(output),
            "LIGHTTABLE_NATIVE_BENCHMARK_ITERATIONS": "1",
            "LIGHTTABLE_NATIVE_BENCHMARK_WIDTH": "1100",
            "LIGHTTABLE_NATIVE_BENCHMARK_QUIT": "1",
            "LIGHTTABLE_NATIVE_SMOKE_JOURNEY": "1",
            "LIGHTTABLE_NATIVE_JOURNEY_LAYER": layer,
            "LIGHTTABLE_NATIVE_JOURNEY_EXPORT_DIR": str(temporary / "exports"),
            "LIGHTTABLE_NATIVE_JOURNEY_SCREENSHOT": str(screenshot),
            "LIGHTTABLE_NATIVE_JOURNEY_IMAGES": json.dumps(image_names),
            "MPLCONFIGDIR": str(temporary / "matplotlib"),
            "NUMBA_CACHE_DIR": str(temporary / "numba"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if corrupt_name:
        env["LIGHTTABLE_NATIVE_JOURNEY_CORRUPT"] = corrupt_name
    return env


def _image_metrics_with_python(python: Path, screenshot: Path, rect: dict) -> dict:
    script = """
import json, sys
from PIL import Image, ImageStat
image = Image.open(sys.argv[1]).convert('RGB')
rect = json.loads(sys.argv[2])
box = (rect['x'], rect['y'], rect['x'] + rect['width'], rect['y'] + rect['height'])
crop = image.crop(box)
stat = ImageStat.Stat(crop)
extrema = crop.getextrema()
print(json.dumps({'imageWidth': image.width, 'imageHeight': image.height,
  'photoWidth': crop.width, 'photoHeight': crop.height,
  'mean': [round(x, 3) for x in stat.mean],
  'stddev': [round(x, 3) for x in stat.stddev],
  'range': [high - low for low, high in extrema]}))
"""
    completed = subprocess.run(
        [str(python), "-c", script, str(screenshot), json.dumps(rect)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def validate_screenshot(app: Path, screenshot: Path, payload: dict) -> dict:
    info = payload.get("screenshot")
    if not isinstance(info, dict) or info.get("error"):
        raise RuntimeError(f"native journey screenshot failed: {info}")
    if not screenshot.is_file() or screenshot.stat().st_size < 20_000:
        raise RuntimeError("native journey did not produce a substantial PNG screenshot")
    rect = info.get("photoRect")
    if not isinstance(rect, dict) or min(rect.get("width", 0), rect.get("height", 0)) < 100:
        raise RuntimeError("native journey screenshot did not identify the photo viewport")
    candidates = [
        Path(os.sys.executable),
        app / "Contents" / "Resources" / "Python" / "bin" / "python3",
        ROOT / ".venv" / "bin" / "python",
    ]
    metrics = None
    errors = []
    for python in dict.fromkeys(candidates):
        if not python.is_file():
            continue
        try:
            metrics = _image_metrics_with_python(python, screenshot, rect)
            break
        except (subprocess.CalledProcessError, json.JSONDecodeError) as error:
            errors.append(str(error))
    if metrics is None:
        raise RuntimeError(f"Pillow was unavailable for screenshot validation: {errors}")
    if max(metrics["stddev"]) < 5 or max(metrics["range"]) < 32:
        raise RuntimeError(f"native photo viewport appears blank: {metrics}")
    return metrics


def run_smoke(
    app: Path,
    fixtures: tuple[Path, ...],
    timeout: int,
    layer: str = "pr",
    screenshot_output: Path | None = None,
) -> dict:
    native, _ = require_bundle(app)
    validate_fixtures(fixtures, layer)

    with tempfile.TemporaryDirectory(prefix="lighttable-native-smoke-") as name:
        temporary = Path(name)
        photos = temporary / "photos"
        photos.mkdir()
        for fixture in fixtures:
            shutil.copy2(fixture, photos / fixture.name)
        corrupt_name = None
        if layer.startswith("raw-"):
            corrupt_name = "zz-corrupt-recovery.DNG"
            (photos / corrupt_name).write_bytes(
                b"This intentionally corrupt RAW verifies viewer recovery.\n"
            )
        image_names = tuple(path.name for path in fixtures)
        if corrupt_name:
            image_names += (corrupt_name,)
        output = temporary / "native-benchmark.json"
        screenshot = screenshot_output or temporary / "native-journey.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        started_at = time.perf_counter()
        process = subprocess.Popen(
            [str(native)],
            cwd=app.parent,
            env=smoke_environment(
                temporary,
                photos,
                output,
                screenshot,
                layer,
                image_names,
                corrupt_name,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            stdout, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            process.terminate()
            try:
                stdout, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, _ = process.communicate(timeout=5)
            server_log = temporary / "server.log"
            detail = server_log.read_text(errors="replace")[-2000:] \
                if server_log.is_file() else stdout[-2000:]
            raise RuntimeError(
                f"native photo journey timed out after {timeout}s\n{detail}"
            ) from error
        process_wall_ms = (time.perf_counter() - started_at) * 1000
        if process.returncode:
            raise RuntimeError(
                f"LightTable exited with {process.returncode}\n{stdout[-2000:]}"
            )
        if not output.is_file():
            raise RuntimeError(
                f"LightTable did not write native benchmark results\n{stdout[-2000:]}"
            )
        payload = json.loads(output.read_text())
        if screenshot_output:
            screenshot_output.with_suffix('.raw.json').write_text(json.dumps(payload, indent=2))
        summary = validate_benchmark(
            payload, require_journey=True, layer=layer)
        if layer in {"pr", "package"}:
            exports = [path for path in (temporary / "exports").glob("*")
                       if path.is_file()
                       and path.suffix.lower() in {".jpg", ".jpeg", ".tif", ".tiff"}]
            if len(exports) != 1 or exports[0].stat().st_size < 10_000:
                raise RuntimeError(
                    f"native journey did not create one substantial export: {exports}"
                )
            summary["export"] = {
                "name": exports[0].name,
                "bytes": exports[0].stat().st_size,
            }
        summary["screenshotMetrics"] = validate_screenshot(
            app, screenshot, payload)
        summary["performance"] = performance_summary(payload, process_wall_ms)
        summary["layer"] = layer
        summary["fixtureCount"] = len(fixtures)
        if screenshot_output:
            summary["screenshot"]["path"] = str(screenshot_output)
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, default=ROOT / "dist/LightTable.app")
    parser.add_argument(
        "--layer",
        choices=("pr", "package", "raw-curated", "raw-full"),
        default="package",
    )
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--screenshot", type=Path)
    arguments = parser.parse_args()
    if arguments.timeout < 10 or arguments.timeout > 7200:
        raise SystemExit("--timeout must be between 10 and 7200 seconds")
    app = arguments.app.expanduser().resolve()
    fixtures = fixtures_for_layer(arguments.layer)
    screenshot = arguments.screenshot.expanduser().resolve() \
        if arguments.screenshot else None
    summary = run_smoke(
        app,
        fixtures,
        arguments.timeout,
        layer=arguments.layer,
        screenshot_output=screenshot,
    )
    result = {
        "schema": 2,
        "ok": True,
        "machine": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        **summary,
    }
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if arguments.output:
        destination = arguments.output.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
