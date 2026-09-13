#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Repeatable before/after measurement of the interactive preview transport.

Compares the JPEG-over-HTTP path every non-Metal client used before this
change (`/api/render` -> `/api/render/image`, then an <img> decode and a
WebGL texImage2D upload) against the raw-transport path this change adds
(`/api/render` with `raw: true` -> `/api/render/native`, a packed RGBA8
surface fetched with no image codec involved at all).

This machine is a Mac, so `sys.platform == "darwin"` is true for the server
process itself; that only affects whether the resident engine hands the
Python process its surface over POSIX shared memory or a plain file (see
`native_shared` in server.py). Either way the surface reaches the network
the same way, over the same HTTP/1.1 keep-alive connection used for the
JPEG, which is what this benchmark exercises: it measures transport, not
presentation. It does not run inside an actual WebView2/WebKitGTK shell,
which needs real Windows/Linux hardware; see the report this script prints
for what that leaves unverified.

Usage:
    .venv/bin/python bench/preview_transport_benchmark.py [--iterations 8] \
        [--widths 1100,2200] [--json out.json]
"""
from __future__ import annotations

import argparse
import io
import json
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEMO_IMAGE = ROOT / "demo-assets" / "cc0-raw" / "contact-sheet.jpg"


def wait_ready(base_url: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"{base_url}/api/images", timeout=2).read()
            return
        except Exception as error:  # noqa: BLE001
            last_error = error
            time.sleep(0.2)
    raise RuntimeError(f"server did not come up: {last_error}")


def post_json(base_url: str, path: str, body: dict, timeout: float = 60) -> dict:
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        f"{base_url}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    return payload, (time.perf_counter() - started) * 1000


def get_bytes(base_url: str, path: str, timeout: float = 60) -> tuple[bytes, float]:
    started = time.perf_counter()
    with urllib.request.urlopen(f"{base_url}{path}", timeout=timeout) as response:
        data = response.read()
    return data, (time.perf_counter() - started) * 1000


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def summary(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min_ms": round(min(values), 3),
        "median_ms": round(statistics.median(values), 3),
        "mean_ms": round(statistics.mean(values), 3),
        "p90_ms": round(percentile(values, 0.90), 3),
        "max_ms": round(max(values), 3),
    }


def measure_width(base_url: str, name: str, width: int, iterations: int) -> dict:
    params = {"profile_enabled": True}
    # Every call site below uses its own client id + a monotonically
    # increasing generation. The server drops a render as "superseded" the
    # moment it sees a lower generation than one it already served for a
    # client (a real drag never rewinds), so reusing numbers across widths
    # or transports is a bug in the benchmark, not the server.
    base_request = {
        "name": name, "params": params, "w": width, "engine": "rs",
        "priority": "interactive",
    }

    # -------------------------------------------------------------- JPEG path
    jpeg_request_ms, jpeg_fetch_ms, jpeg_decode_ms, jpeg_bytes_len = [], [], [], []
    for i in range(iterations):
        # A distinct exposure value per iteration defeats the render cache,
        # matching a real drag: every frame is a fresh film render.
        req = dict(base_request, params=dict(params, exposure_ev=0.001 * (i + 1)),
                   raw=False, native=False, client=f"bench-jpeg-{width}", generation=i,
                   allow_draft=False)
        result, request_ms = post_json(base_url, "/api/render", req)
        if result.get("error") or result.get("cancelled"):
            raise RuntimeError(f"render failed: {result}")
        assert result.get("img"), "expected a JPEG url for the legacy transport"
        jpeg_bytes, fetch_ms = get_bytes(base_url, result["img"])
        decode_started = time.perf_counter()
        Image.open(io.BytesIO(jpeg_bytes)).convert("RGB").load()
        decode_ms = (time.perf_counter() - decode_started) * 1000
        jpeg_request_ms.append(request_ms)
        jpeg_fetch_ms.append(fetch_ms)
        jpeg_decode_ms.append(decode_ms)
        jpeg_bytes_len.append(len(jpeg_bytes))

    # --------------------------------------------------------------- raw path
    raw_request_ms, raw_fetch_ms, raw_parse_ms, raw_bytes_len = [], [], [], []
    for i in range(iterations):
        req = dict(base_request, params=dict(params, exposure_ev=0.001 * (i + 1) + 0.5),
                   raw=True, native=False, client=f"bench-raw-{width}", generation=i,
                   allow_draft=False)
        result, request_ms = post_json(base_url, "/api/render", req)
        if result.get("error") or result.get("cancelled"):
            raise RuntimeError(f"render failed: {result}")
        assert result.get("native"), "expected a raw surface for the new transport"
        assert not result.get("img"), "raw transport should skip the eager JPEG encode"
        raw_bytes, fetch_ms = get_bytes(base_url, result["native"]["url"])
        parse_started = time.perf_counter()
        header_bytes = result["native"]["headerBytes"]
        width_px = result["native"]["width"]
        height_px = result["native"]["height"]
        row_bytes = result["native"]["rowBytes"]
        pixels = memoryview(raw_bytes)[header_bytes:header_bytes + row_bytes * height_px]
        assert len(pixels) == row_bytes * height_px
        parse_ms = (time.perf_counter() - parse_started) * 1000
        raw_request_ms.append(request_ms)
        raw_fetch_ms.append(fetch_ms)
        raw_parse_ms.append(parse_ms)
        raw_bytes_len.append(len(raw_bytes))
        del width_px  # keep pyflakes quiet; used for documentation above

    return {
        "width": width,
        "jpeg": {
            "server_render_roundtrip_ms": summary(jpeg_request_ms),
            "image_fetch_ms": summary(jpeg_fetch_ms),
            "client_decode_ms": summary(jpeg_decode_ms),
            "end_to_end_ms": summary([a + b + c for a, b, c in
                                      zip(jpeg_request_ms, jpeg_fetch_ms, jpeg_decode_ms)]),
            "payload_bytes": summary([float(v) for v in jpeg_bytes_len]),
        },
        "raw": {
            "server_render_roundtrip_ms": summary(raw_request_ms),
            "surface_fetch_ms": summary(raw_fetch_ms),
            "client_parse_ms": summary(raw_parse_ms),
            "end_to_end_ms": summary([a + b + c for a, b, c in
                                      zip(raw_request_ms, raw_fetch_ms, raw_parse_ms)]),
            "payload_bytes": summary([float(v) for v in raw_bytes_len]),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--widths", default="1100,2200")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--image", type=Path, default=DEMO_IMAGE)
    args = parser.parse_args()

    widths = [int(w) for w in args.widths.split(",")]

    with tempfile.TemporaryDirectory(prefix="lighttable-transport-bench-") as temp:
        photos = Path(temp) / "photos"
        photos.mkdir()
        target = photos / args.image.name
        target.write_bytes(args.image.read_bytes())
        cache = Path(temp) / "cache"
        env = {
            **__import__("os").environ,
            "LIGHTTABLE_DIR": str(photos),
            "LIGHTTABLE_PORT": str(args.port),
            "LIGHTTABLE_CACHE_DIR": str(cache),
            "LIGHTTABLE_PREFS_FILE": str(cache / "bench-prefs.json"),
            "LIGHTTABLE_CATALOG_FILE": str(cache / "bench-catalog.sqlite3"),
            "LIGHTTABLE_CATALOG_MIRROR": "0",
            "OMP_NUM_THREADS": "4", "NUMBA_NUM_THREADS": "4",
        }
        log = Path(temp) / "server.log"
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "server.py")], cwd=ROOT, env=env,
            stdout=log.open("ab"), stderr=subprocess.STDOUT)
        try:
            base_url = f"http://127.0.0.1:{args.port}"
            wait_ready(base_url)
            deadline = time.monotonic() + 30
            name = None
            while time.monotonic() < deadline and name is None:
                with urllib.request.urlopen(f"{base_url}/api/images", timeout=2) as response:
                    data = json.loads(response.read())
                images = data.get("images") or []
                if images:
                    name = images[0]["name"]
                else:
                    time.sleep(0.2)
            if name is None:
                raise RuntimeError("bench photo never appeared in the catalog")

            report = {"image": name, "widths": []}
            for width in widths:
                print(f"measuring width={width}px ...", file=sys.stderr)
                report["widths"].append(measure_width(base_url, name, width, args.iterations))
            report_text = json.dumps(report, indent=2)
            print(report_text)
            if args.json:
                args.json.write_text(report_text)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
