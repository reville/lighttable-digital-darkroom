#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Repeatable export pipeline benchmark against a real photo folder.

Starts the app tree with an isolated cache/catalog, then measures:

* the first export after startup (whether the background engine was warmed),
* an N-photo JPEG batch through ``/api/export`` (wall time plus per-photo
  phase timings), repeated so the cold and warm-cache runs are both visible,
* one interactive preview fired while the batch is running, to check that
  exports never starve the preview path,
* the SHA-256 of every delivered JPEG, so before/after runs can be compared
  for identical pixels as well as speed.

Environment switches the server honours (``LIGHTTABLE_EXPORT_WORKERS``,
``LIGHTTABLE_DEFERRED_ENCODE``, ``LIGHTTABLE_WARM_BACKGROUND_ENGINE``) pass
through from the caller, so a before/after pair is two invocations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark import (  # noqa: E402
    RAW_SUFFIXES, fetch_json, git_revision, server_environment, stop_process,
    wait_for_catalog_scan, wait_for_images, wait_ready,
)


def post_json(url: str, payload: dict, token: str, timeout: float) -> tuple[dict, float]:
    """Mutating routes need the instance token that the instance file carries."""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-LightTable-Token": token})
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
    return json.loads(body), (time.perf_counter() - started) * 1000


def read_token(instance_dir: Path, port: int, timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    path = instance_dir / f"{port}.json"
    while time.monotonic() < deadline:
        try:
            token = json.loads(path.read_text()).get("token")
            if token:
                return str(token)
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise RuntimeError(f"instance file without a token: {path}")


def batch_names(images: list[dict], count: int, raw_only: bool) -> list[str]:
    names = [entry["name"] for entry in images
             if not raw_only or Path(entry["name"]).suffix.lower() in RAW_SUFFIXES]
    if not names:
        raise RuntimeError("benchmark folder contains no eligible images")
    return names[:count]


def run_batch(base_url: str, token: str, names: list[str], destination: Path,
              params: dict, preview_name: str | None, width: int,
              quality: int, timeout: float) -> dict:
    """Queue one export batch and record its wall time and delivered hashes."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    preview: dict = {}

    def probe_preview() -> None:
        time.sleep(0.5)
        try:
            result, elapsed = fetch_json(f"{base_url}/api/render", {
                "name": preview_name, "params": params, "w": width,
                "engine": "rs", "priority": "interactive",
            }, timeout=timeout)
            preview.update(http_ms=round(elapsed, 2), ok=True,
                           engine_ms=result.get("ms"), cached=result.get("cached"))
        except Exception as error:  # the benchmark records failures
            preview.update(ok=False, error=str(error))

    probe = None
    if preview_name:
        probe = threading.Thread(target=probe_preview, daemon=True)
    started = time.perf_counter()
    queued, _ = post_json(f"{base_url}/api/export", {
        "names": names, "which": "all", "destination": str(destination),
        "format": "jpeg", "quality": quality, "metadata": "none",
        "collision": "overwrite", "engine": "rs", "sidecar": False,
    }, token, timeout)
    if queued.get("error"):
        raise RuntimeError(queued["error"])
    if probe:
        probe.start()
    deadline = time.monotonic() + timeout
    status: dict = {}
    while time.monotonic() < deadline:
        status, _ = fetch_json(f"{base_url}/api/export/status", timeout=10)
        if not status.get("running"):
            break
        time.sleep(0.05)
    wall_ms = (time.perf_counter() - started) * 1000
    if probe:
        probe.join(timeout=timeout)
    if status.get("running"):
        raise RuntimeError("export batch did not finish before the benchmark timeout")
    outputs = sorted(path for path in destination.iterdir() if path.suffix.lower() == ".jpg")
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in outputs}
    return {
        "wall_ms": round(wall_ms, 2),
        "queued": queued.get("queued"),
        "completed": status.get("completed"),
        "errors": status.get("errors", []),
        "timings": status.get("timings", []),
        "outputs": hashes,
        "output_bytes": {path.name: path.stat().st_size for path in outputs},
        "preview_during_export": preview,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--photos", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8352)
    parser.add_argument("--count", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--width", type=int, default=1100)
    parser.add_argument("--quality", type=int, default=92)
    parser.add_argument("--warm-wait", type=float, default=12.0,
                        help="seconds after the scan for the startup warm-up to finish")
    parser.add_argument("--all-formats", action="store_true",
                        help="include non-RAW originals in the batch")
    parser.add_argument("--skip-preview-probe", action="store_true")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.count <= 64 or not 1 <= args.repeats <= 10:
        raise SystemExit("--count must be 1..64 and --repeats 1..10")

    app_root = args.app_root.resolve()
    python = app_root / ".venv/bin/python"
    if not python.exists():
        raise SystemExit(f"missing runtime: {python}")
    base_url = f"http://127.0.0.1:{args.port}"
    work = Path(tempfile.mkdtemp(prefix="lighttable-export-bench-"))
    cache_root = work / "cache"
    log_path = work / "server.log"
    log_handle = log_path.open("wb")
    started = time.perf_counter()
    env = server_environment(args.photos.resolve(), args.port, cache_root)
    env["LIGHTTABLE_INSTANCE_DIR"] = str(work / "instances")
    process = subprocess.Popen(
        [str(python), str(app_root / "server.py")], cwd=app_root,
        env=env, stdout=log_handle, stderr=subprocess.STDOUT)
    try:
        initial, _ = wait_ready(base_url)
        token = read_token(work / "instances", args.port)
        startup_ms = (time.perf_counter() - started) * 1000
        initial = wait_for_images(base_url, initial)
        wait_for_catalog_scan(base_url)
        # Let the startup warm-up thread finish, so the first export measures
        # the background engine's own cold start rather than a shared one.
        time.sleep(args.warm_wait)
        names = batch_names(initial["images"], args.count, not args.all_formats)
        params = dict(initial["defaults"])
        job = {"params": params, "grade": {"exposure": 0.15, "contrast": 0.2},
               "crop": None, "format": "jpeg", "quality": args.quality,
               "longEdge": None, "engine": "rs"}
        first, first_http_ms = fetch_json(
            f"{base_url}/api/perf/export-one", {"name": names[0], "job": job},
            timeout=args.timeout)
        runs = []
        for index in range(args.repeats):
            runs.append(run_batch(
                base_url, token, names, work / f"batch-{index}", params,
                None if args.skip_preview_probe else names[0],
                args.width, args.quality, args.timeout))
        identical = all(run["outputs"] == runs[0]["outputs"] for run in runs)
        output = {
            "schema": 1,
            "label": args.output.stem,
            "app_root": str(app_root),
            "git_revision": git_revision(app_root),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "photo_folder": str(args.photos.resolve()),
            "names": names,
            "environment": {key: value for key, value in os.environ.items()
                            if key.startswith("LIGHTTABLE_")},
            "startup_ms": round(startup_ms, 2),
            "warm_wait_s": args.warm_wait,
            "first_export": {
                "http_ms": round(first_http_ms, 2),
                "elapsed_ms": first.get("elapsed_ms"),
                "phase_ms": first.get("phase_ms"),
                "input_transport": first.get("input_transport"),
                "backend": first.get("backend"),
            },
            "batches": runs,
            "batch_wall_ms": [run["wall_ms"] for run in runs],
            "outputs_identical_across_runs": identical,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(output, indent=2) + "\n")
        print(json.dumps({
            "first_export_ms": output["first_export"]["elapsed_ms"],
            "batch_wall_ms": output["batch_wall_ms"],
            "preview_during_export": [run["preview_during_export"] for run in runs],
            "outputs_identical_across_runs": identical,
            "errors": [run["errors"] for run in runs],
        }, indent=2))
        return 0
    finally:
        stop_process(process)
        log_handle.close()
        if process.returncode not in (0, -15):
            print(log_path.read_text(errors="replace")[-2000:], file=sys.stderr)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
