#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Repeatable end-to-end LightTable performance benchmark.

The benchmark starts the selected app tree against a real photo directory,
measures HTTP/library/thumbnail/original/film-render paths, exercises a burst of
distinct film renders, and times one full-resolution export. Results are JSON so
before/after runs can be compared mechanically.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import media_formats  # noqa: E402

RAW_SUFFIXES = media_formats.RAW_EXTENSIONS


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def summary(values: list[float]) -> dict[str, float | int | list[float]]:
    return {
        "count": len(values),
        "min_ms": round(min(values), 2) if values else 0.0,
        "median_ms": round(statistics.median(values), 2) if values else 0.0,
        "mean_ms": round(statistics.mean(values), 2) if values else 0.0,
        "p90_ms": round(percentile(values, 0.90), 2),
        "p95_ms": round(percentile(values, 0.95), 2),
        "max_ms": round(max(values), 2) if values else 0.0,
        "samples_ms": [round(value, 2) for value in values],
    }


BROWSER_TIMING_FIELDS = (
    "totalMs", "queueMs", "requestMs", "serverMs", "residentMs", "gpuMs",
    "imageDecodeMs", "textureUploadMs", "paintAfterUploadMs",
)


def render_timing_summary(entries: list[dict]) -> dict:
    output = {
        "count": len(entries),
        "cached": sum(bool(entry.get("cached")) for entry in entries),
        "refining": sum(bool(entry.get("refining")) for entry in entries),
    }
    for field in BROWSER_TIMING_FIELDS:
        values = [float(entry[field]) for entry in entries
                  if isinstance(entry.get(field), (int, float))]
        output[field] = summary(values)
    return output


def find_playwright_module() -> Path | None:
    configured = os.environ.get("LIGHTTABLE_PLAYWRIGHT_MODULE")
    if configured:
        path = Path(configured).expanduser()
        if path.is_dir():
            path = path / "index.mjs"
        if path.is_file():
            return path
    root = Path.home() / ".npm" / "_npx"
    candidates = list(root.glob("*/node_modules/playwright/index.mjs"))
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def find_playwright_browser() -> Path | None:
    root = Path.home() / "Library" / "Caches" / "ms-playwright"
    candidates = list(root.glob(
        "chromium_headless_shell-*/chrome-headless-shell-mac-*/chrome-headless-shell"))
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def run_browser_benchmark(base_url: str, app_root: Path, widths: list[int],
                          iterations: int, image_name: str) -> dict:
    module = find_playwright_module()
    browser = find_playwright_browser()
    node = shutil.which("node")
    if not module or not browser or not node:
        return {"skipped": True, "reason": "Playwright, Chromium, or Node.js unavailable"}
    with tempfile.TemporaryDirectory(prefix="lighttable-browser-bench-") as temp:
        config = Path(temp) / "config.json"
        config.write_text(json.dumps({
            "baseUrl": base_url,
            "widths": widths,
            "iterations": iterations,
            "browserExecutable": str(browser),
            "imageName": image_name,
        }))
        env = dict(os.environ, LIGHTTABLE_PLAYWRIGHT_MODULE=str(module))
        result = subprocess.run(
            [node, str(app_root / "bench" / "browser_benchmark.mjs"), str(config)],
            cwd=app_root, env=env, capture_output=True, text=True,
            timeout=max(600, len(widths) * iterations * 20), check=False,
        )
    if result.returncode:
        return {"skipped": True, "reason": (result.stderr or result.stdout)[-1000:]}
    raw = json.loads(result.stdout.strip().splitlines()[-1])
    return {
        "userAgent": raw.get("userAgent"),
        "startup": raw.get("startup"),
        "widths": {
            width: {
                "firstFrame": render_timing_summary(entries.get("first", [])),
                "settledFrame": render_timing_summary(entries.get("settled", [])),
            }
            for width, entries in raw.get("widths", {}).items()
        },
        "navigation": {
            "firstFrame": render_timing_summary(
                raw.get("navigation", {}).get("first", [])),
            "settledFrame": render_timing_summary(
                raw.get("navigation", {}).get("settled", [])),
        },
    }


def fetch(url: str, payload: dict | None = None, timeout: float = 300) -> tuple[bytes, float]:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return body, (time.perf_counter() - started) * 1000


def fetch_json(url: str, payload: dict | None = None, timeout: float = 300) -> tuple[dict, float]:
    body, elapsed = fetch(url, payload, timeout)
    return json.loads(body), elapsed


def wait_ready(base_url: str, timeout: float = 90) -> tuple[dict, float]:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return fetch_json(f"{base_url}/api/images", timeout=2)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            last_error = error
            time.sleep(0.1)
    raise RuntimeError(f"LightTable did not become ready: {last_error}")


def wait_for_images(base_url: str, initial: dict,
                    timeout: float = 90) -> dict:
    """Wait for the first background scan without redefining startup time.

    `wait_ready` measures when the port can answer. A fresh catalog is allowed
    to answer before it has rows, but render benchmarks still need one image.
    """
    result = initial
    deadline = time.monotonic() + timeout
    while not result.get("images") and time.monotonic() < deadline:
        time.sleep(0.1)
        result, _ = fetch_json(f"{base_url}/api/images", timeout=2)
    return result


def wait_for_catalog_scan(base_url: str, timeout: float = 900) -> None:
    """Wait for queued and running scan work, tolerating the startup race."""
    deadline = time.monotonic() + timeout
    observed_work = False
    idle_samples = 0
    while time.monotonic() < deadline:
        status, _ = fetch_json(f"{base_url}/api/catalog/scan", timeout=2)
        busy = bool(status.get("running") or status.get("queued"))
        if busy:
            observed_work = True
            idle_samples = 0
        else:
            idle_samples += 1
            if observed_work or idle_samples >= 3:
                return
        time.sleep(0.1)
    raise RuntimeError("catalog scan did not complete before the benchmark timeout")


def server_environment(photos: Path, port: int,
                       cache_dir: Path | None = None) -> dict[str, str]:
    env = dict(
        os.environ,
        LIGHTTABLE_DIR=str(photos),
        LIGHTTABLE_PORT=str(port),
        OMP_NUM_THREADS="4",
        NUMBA_NUM_THREADS="4",
        OPENBLAS_NUM_THREADS="4",
        MPLCONFIGDIR=str(Path(tempfile.gettempdir()) / "lighttable-benchmark-mpl"),
        PYTHONUNBUFFERED="1",
    )
    if cache_dir is not None:
        env["LIGHTTABLE_CACHE_DIR"] = str(cache_dir)
        env["LIGHTTABLE_PREFS_FILE"] = str(cache_dir / "benchmark-prefs.json")
        env["LIGHTTABLE_CATALOG_FILE"] = str(
            cache_dir / "benchmark-catalog.sqlite3")
        env["LIGHTTABLE_CATALOG_MIRROR"] = "0"
    return env


def stop_process(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def benchmark_catalog(app_root: Path, python: Path, port: int,
                      image_count: int, iterations: int) -> dict:
    """Measure the catalog at the scale it exists for.

    The old per-folder library could only be asked "what is in this folder";
    these are the numbers that decide whether "every photograph I own, newest
    first" is usable: the cold scan that builds the catalog, the warm start
    once it exists, and the paged query behind the grid.
    """
    with tempfile.TemporaryDirectory(prefix="lighttable-catalog-") as temp:
        photos = Path(temp) / "photos"
        photos.mkdir()
        # Distinct bytes per file. Hard links to one seed would make every
        # file share a content hash, which is not what a real library looks
        # like and would measure duplicate handling rather than scanning.
        seed = photos / ".seed.jpg"
        Image.new("RGB", (8, 8), (92, 108, 126)).save(seed, "JPEG", quality=70)
        template = seed.read_bytes()
        # A realistic spread: many shoot folders rather than one flat heap.
        folders = max(1, min(400, round(image_count / 250) or 1))
        for index in range(image_count):
            folder = photos / f"{2020 + index % 6}" / f"roll-{index % folders:04d}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"frame-{index:06d}.jpg").write_bytes(
                template + b"\xff" + index.to_bytes(4, "big"))

        cache = Path(temp) / "cache"
        env = server_environment(photos, port, cache)
        log = Path(temp) / "server.log"

        def run_server():
            return subprocess.Popen(
                [str(python), str(app_root / "server.py")], cwd=app_root,
                env=env, stdout=log.open("ab"), stderr=subprocess.STDOUT)

        # Cold: the catalog does not exist yet, so this includes the walk, the
        # content hashing, and the metadata read for every file.
        cold_started = time.perf_counter()
        process = run_server()
        try:
            wait_ready(f"http://127.0.0.1:{port}")
            cold_ready_ms = (time.perf_counter() - cold_started) * 1000
            deadline = time.time() + 900
            scan_ms = None
            while time.time() < deadline:
                status, _ = fetch_json(
                    f"http://127.0.0.1:{port}/api/catalog")
                stats = (status or {}).get("stats") or {}
                scanning = ((status or {}).get("scan") or {}).get("running")
                if stats.get("files", 0) >= image_count and not scanning:
                    scan_ms = (time.perf_counter() - cold_started) * 1000
                    break
                time.sleep(0.5)
            indexed = stats.get("files", 0)
        finally:
            stop_process(process)

        # Warm: the catalog is already built, which is every launch after the
        # first one.
        warm_started = time.perf_counter()
        process = run_server()
        try:
            wait_ready(f"http://127.0.0.1:{port}")
            warm_ready_ms = (time.perf_counter() - warm_started) * 1000
            queries: dict[str, list[float]] = {}
            shapes = {
                "all_by_capture": {"limit": 500,
                                   "sort": {"field": "capture"}},
                "all_by_name": {"limit": 500, "sort": {"field": "name"}},
                "rated_only": {"limit": 500, "filter": {"ratingMin": 3}},
                "text_search": {"limit": 500,
                                "filter": {"query": "frame-0001"}},
                "deep_page": {"limit": 500,
                              "offset": max(0, image_count - 600)},
            }
            for label, body in shapes.items():
                samples = []
                for _ in range(max(1, iterations)):
                    _, elapsed = fetch_json(
                        f"http://127.0.0.1:{port}/api/catalog/query", body)
                    samples.append(elapsed)
                queries[label] = samples
        finally:
            stop_process(process)

    return {
        "images": image_count,
        "indexed": indexed,
        "cold_ready_ms": round(cold_ready_ms, 1),
        "cold_scan_ms": round(scan_ms, 1) if scan_ms else None,
        "warm_ready_ms": round(warm_ready_ms, 1),
        "queries": {label: summary(values) for label, values in queries.items()},
    }


def benchmark_large_library(app_root: Path, python: Path, port: int,
                            image_count: int, iterations: int) -> dict:
    """Measure recursive enumeration and JSON response cost at realistic scale."""
    with tempfile.TemporaryDirectory(prefix="lighttable-large-library-") as temp:
        photos = Path(temp) / "photos"
        photos.mkdir()
        seed = photos / ".seed.jpg"
        Image.new("RGB", (8, 8), (92, 108, 126)).save(seed, "JPEG", quality=70)
        folders = max(1, min(64, round(image_count ** 0.5)))
        for index in range(image_count):
            folder = photos / f"roll-{index % folders:03d}"
            folder.mkdir(exist_ok=True)
            os.link(seed, folder / f"frame-{index:06d}.jpg")

        log = Path(temp) / "server.log"
        with log.open("wb") as output:
            started = time.perf_counter()
            process = subprocess.Popen(
                [str(python), str(app_root / "server.py")], cwd=app_root,
                env=server_environment(photos, port, Path(temp) / "cache"), stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                url = f"http://127.0.0.1:{port}"
                initial, first_request_ms = wait_ready(url)
                if process.poll() is not None:
                    raise RuntimeError(
                        f"benchmark server exited early with {process.returncode}; "
                        f"see {log}")
                startup_ms = (time.perf_counter() - started) * 1000
                wait_for_catalog_scan(url)
                initial, _ = fetch_json(f"{url}/api/images")
                samples = [first_request_ms]
                for _ in range(max(0, iterations - 1)):
                    _, elapsed = fetch_json(f"http://127.0.0.1:{port}/api/images")
                    samples.append(elapsed)
            finally:
                stop_process(process)
        return {
            "image_count": int(initial.get("total", len(initial.get("images", [])))),
            "initial_page_count": len(initial.get("images", [])),
            "folder_count": len(initial.get("folders", [])),
            "startup_ms": round(startup_ms, 2),
            "api_images": summary(samples),
        }


def git_revision(app_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=app_root,
        capture_output=True, text=True, check=False,
    )
    return result.stdout.strip() or "unknown"


def cache_bytes(cache_root: Path) -> int:
    total = 0
    if cache_root.exists():
        for path in cache_root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    return total


def choose_image(images: list[dict]) -> str:
    raw = [entry["name"] for entry in images if Path(entry["name"]).suffix.lower() in RAW_SUFFIXES]
    if raw:
        preferred = [name for name in raw if name.lower().endswith(".dng")]
        return (preferred or raw)[0]
    if not images:
        raise RuntimeError("benchmark folder contains no supported images")
    return images[0]["name"]


def run_export(base_url: str, app_root: Path, python: Path, cache_root: Path,
               image_name: str, params: dict, width: int) -> dict:
    tiffs = sorted((cache_root / "tiff").glob("*.tif"), key=lambda path: path.stat().st_mtime)
    source_tiff = tiffs[-1] if tiffs else None
    job = {
        "params": params,
        "grade": {
            "exposure": 0.15,
            "contrast": 0.2,
            "texture": 0.25,
            "clarity": 0.2,
            "dehaze": 0.1,
            "vignette": 0.15,
        },
        "crop": None,
        "format": "jpeg",
        "quality": 92,
        "longEdge": None,
        "engine": "rs",
    }
    try:
        result, _ = fetch_json(
            f"{base_url}/api/perf/export-one",
            {"name": image_name, "job": job}, timeout=1200)
        result["preview_width"] = width
        return result
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    if source_tiff is None:
        return {"skipped": True, "reason": "preview did not create a source TIFF"}
    with tempfile.TemporaryDirectory(prefix="lighttable-export-bench-") as temp:
        temp_path = Path(temp)
        output = temp_path / "benchmark-export.jpg"
        job_file = temp_path / "job.json"
        job_file.write_text(json.dumps(job))
        started = time.perf_counter()
        result = subprocess.run(
            [str(python), str(app_root / "render_cli.py"), str(source_tiff),
             str(output), str(job_file)],
            cwd=app_root,
            capture_output=True,
            text=True,
            timeout=1200,
            check=False,
            env=dict(os.environ, OMP_NUM_THREADS="4", NUMBA_NUM_THREADS="4",
                     OPENBLAS_NUM_THREADS="4"),
        )
        elapsed = (time.perf_counter() - started) * 1000
        return {
            "elapsed_ms": round(elapsed, 2),
            "returncode": result.returncode,
            "output_bytes": output.stat().st_size if output.exists() else 0,
            "stdout_tail": result.stdout.strip()[-300:],
            "stderr_tail": result.stderr.strip()[-300:],
            "preview_width": width,
            "source_tiff_bytes": source_tiff.stat().st_size,
        }


def benchmark_preview_matrix(base_url: str, image_name: str, params: dict,
                             engine: str, widths: list[int],
                             iterations: int) -> dict:
    output = {}
    for width in widths:
        entries = []
        for index in range(iterations):
            fraction = index / max(1, iterations - 1)
            changed = dict(params, exposure_ev=-1.35 + fraction * 2.7)
            result, elapsed = fetch_json(f"{base_url}/api/render", {
                "name": image_name,
                "params": changed,
                "w": width,
                "engine": engine,
                "priority": "interactive",
            })
            entries.append(dict(result, httpMs=elapsed))
        fields = {
            "httpMs": [float(entry["httpMs"]) for entry in entries],
            "serverMs": [float(entry["ms"]) for entry in entries
                         if isinstance(entry.get("ms"), (int, float))],
            "residentMs": [float(entry["resident_ms"]) for entry in entries
                           if isinstance(entry.get("resident_ms"), (int, float))],
            "gpuMs": [float(entry["gpu_ms"]) for entry in entries
                      if isinstance(entry.get("gpu_ms"), (int, float))],
        }
        output[str(width)] = {
            "count": len(entries),
            "cached": sum(bool(entry.get("cached")) for entry in entries),
            **{name: summary(values) for name, values in fields.items()},
        }
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--photos", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8340)
    parser.add_argument("--engine", choices=("rs", "py"), default="rs")
    parser.add_argument("--width", type=int, default=1100)
    parser.add_argument("--widths", default="1100,2200,5000")
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--browser-iterations", type=int)
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--large-library-count", type=int, default=2048)
    parser.add_argument("--skip-large-library", action="store_true")
    # The catalog exists for libraries far past what a single folder held, so
    # its own pass defaults to a size that would have been unusable before.
    parser.add_argument("--catalog-count", type=int, default=100000)
    parser.add_argument("--skip-catalog", action="store_true")
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--skip-export", action="store_true")
    args = parser.parse_args()

    if args.iterations < 1 or args.iterations > 500:
        raise SystemExit("--iterations must be between 1 and 500")
    widths = sorted({int(value) for value in args.widths.split(",") if value.strip()})
    if not widths or any(width < 320 or width > 10000 for width in widths):
        raise SystemExit("--widths must contain comma-separated values from 320 to 10000")
    browser_iterations = args.browser_iterations or args.iterations

    app_root = args.app_root.resolve()
    photos = args.photos.resolve()
    python = app_root / ".venv/bin/python"
    if not python.exists():
        raise SystemExit(f"missing runtime: {python}")
    owns_cache = args.cache_dir is None
    cache_root = (Path(tempfile.mkdtemp(prefix="lighttable-benchmark-cache-"))
                  if owns_cache else args.cache_dir.resolve())

    base_url = f"http://127.0.0.1:{args.port}"
    log_file = tempfile.NamedTemporaryFile(prefix="lighttable-bench-", suffix=".log", delete=False)
    log_path = Path(log_file.name)
    log_file.close()
    log_handle = log_path.open("wb")
    started = time.perf_counter()
    process = subprocess.Popen(
        [str(python), str(app_root / "server.py")],
        cwd=app_root,
        env=server_environment(photos, args.port, cache_root),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    try:
        initial, ready_request_ms = wait_ready(base_url)
        startup_ms = (time.perf_counter() - started) * 1000
        initial = wait_for_images(base_url, initial)
        image_name = choose_image(initial["images"])
        params = dict(initial["defaults"])

        image_list_times = [ready_request_ms]
        for _ in range(max(0, args.iterations - 1)):
            _, elapsed = fetch_json(f"{base_url}/api/images")
            image_list_times.append(elapsed)

        encoded_name = urllib.parse.quote(image_name, safe="")
        thumb_times = []
        for _ in range(args.iterations):
            _, elapsed = fetch(f"{base_url}/api/thumb?name={encoded_name}")
            thumb_times.append(elapsed)

        original_times = []
        for _ in range(args.iterations):
            _, elapsed = fetch(
                f"{base_url}/api/orig?name={encoded_name}&w={args.width}&rot=0")
            original_times.append(elapsed)

        render_url = f"{base_url}/api/render"
        first_render, first_render_ms = fetch_json(render_url, {
            "name": image_name,
            "params": params,
            "w": args.width,
            "engine": args.engine,
        })
        _, cached_render_ms = fetch_json(render_url, {
            "name": image_name,
            "params": params,
            "w": args.width,
            "engine": args.engine,
        })

        distinct_render_times = []
        distinct_engine_times = []
        for index in range(args.iterations):
            value = -1.2 + index * (2.4 / max(1, args.iterations - 1))
            changed = dict(params, exposure_ev=value)
            result, elapsed = fetch_json(render_url, {
                "name": image_name,
                "params": changed,
                "w": args.width,
                "engine": args.engine,
            })
            distinct_render_times.append(elapsed)
            if isinstance(result.get("ms"), (int, float)):
                distinct_engine_times.append(float(result["ms"]))

        burst_payloads = []
        for index in range(args.iterations):
            burst_payloads.append({
                "name": image_name,
                "params": dict(params, print_exposure=0.91 + index * 0.031),
                "w": args.width,
                "engine": args.engine,
            })
        burst_started = time.perf_counter()
        burst_times = []
        burst_errors = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(fetch_json, render_url, payload) for payload in burst_payloads]
            for future in as_completed(futures):
                try:
                    _, elapsed = future.result()
                    burst_times.append(elapsed)
                except Exception as error:  # benchmark records failures rather than hiding them
                    burst_errors.append(str(error))
        burst_wall_ms = (time.perf_counter() - burst_started) * 1000

        preview_matrix = benchmark_preview_matrix(
            base_url, image_name, params, args.engine, widths, args.iterations)
        browser = ({"skipped": True, "reason": "requested"}
                   if args.skip_browser else run_browser_benchmark(
                       base_url, app_root, widths, browser_iterations, image_name))
        catalog_scale = ({"skipped": True, "reason": "requested"}
                         if args.skip_catalog else benchmark_catalog(
                             app_root, python, args.port + 2,
                             args.catalog_count, args.iterations))
        large_library = ({"skipped": True, "reason": "requested"}
                         if args.skip_large_library else benchmark_large_library(
                             app_root, python, args.port + 1,
                             args.large_library_count, args.iterations))
        export = {"skipped": True, "reason": "requested"} if args.skip_export else run_export(
            base_url, app_root, python, cache_root, image_name, params, args.width)

        output = {
            "schema": 1,
            "label": args.output.stem,
            "app_root": str(app_root),
            "git_revision": git_revision(app_root),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "photo_folder": str(photos),
            "image": image_name,
            "image_count": len(initial["images"]),
            "engine": args.engine,
            "preview_width": args.width,
            "preview_widths": widths,
            "iterations": args.iterations,
            "startup_ms": round(startup_ms, 2),
            "api_images": summary(image_list_times),
            "thumbnail": summary(thumb_times),
            "original": summary(original_times),
            "first_render_http_ms": round(first_render_ms, 2),
            "first_render_engine_ms": first_render.get("ms"),
            "first_render_cached": first_render.get("cached"),
            "cached_render_http_ms": round(cached_render_ms, 2),
            "distinct_render_http": summary(distinct_render_times),
            "distinct_render_engine": summary(distinct_engine_times),
            "burst": {
                "wall_ms": round(burst_wall_ms, 2),
                "requests": summary(burst_times),
                "errors": burst_errors,
            },
            "preview_matrix": preview_matrix,
            "browser": browser,
            "large_library": large_library,
            "catalog": catalog_scale,
            "export": export,
            "cache_bytes": cache_bytes(cache_root),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, indent=2) + "\n")
        print(json.dumps(output, indent=2))
        return 0
    finally:
        stop_process(process)
        log_handle.close()
        if process.returncode not in (0, -15):
            print(log_path.read_text(errors="replace")[-2000:], file=sys.stderr)
        log_path.unlink(missing_ok=True)
        if owns_cache:
            shutil.rmtree(cache_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
