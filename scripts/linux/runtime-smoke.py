#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Check a relocated Linux bundle using isolated data and a real CPU render.

This checks dependencies, ICC conversion, both Rust engines, HTTP startup and
the packaged CLI. It does not certify native display, GPU speed or color.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    if sys.platform != "linux":
        parser.error("The packaged runtime smoke must run on Linux")
    assert Path(sys.prefix).resolve() == bundle / "Python", (sys.prefix, bundle)
    resources = bundle / "Resources/LightTable"
    vendor = resources / "vendor/spektrafilm/src"
    sys.path[:0] = [str(resources), str(vendor)]
    with tempfile.TemporaryDirectory(prefix="lighttable-linux-smoke-") as temporary:
        scratch = Path(temporary)
        photos = scratch / "photos"
        photos.mkdir()
        environment = {
            "XDG_DATA_HOME": str(scratch / "data"),
            "XDG_CONFIG_HOME": str(scratch / "config"),
            "XDG_CACHE_HOME": str(scratch / "cache"),
            "XDG_STATE_HOME": str(scratch / "state"),
            "LIGHTTABLE_DIR": str(photos),
            "LIGHTTABLE_INSTANCE_DIR": str(scratch / "instances"),
            "LIGHTTABLE_WATCH": "0",
            "NUMBA_CACHE_DIR": str(scratch / "compiled-runtime"),
            "MPLCONFIGDIR": str(scratch / "matplotlib"),
            "PYTHONPATH": os.pathsep.join((str(resources), str(vendor))),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "SPEKTRAFILM_BACKEND": "cpu",
            "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2", "NUMBA_NUM_THREADS": "2",
        }
        # User runtime overrides must never direct this package check into a
        # personal catalog or preferences file.
        for key in tuple(os.environ):
            if key.startswith("LIGHTTABLE_"):
                del os.environ[key]
        os.environ.update(environment)
        import OpenImageIO  # noqa: F401
        import exiv2  # noqa: F401
        import imagecodecs  # noqa: F401
        import lensfunpy  # noqa: F401
        import rawpy  # noqa: F401
        import numpy as np
        import tifffile
        from PIL import Image
        import server

        assert (bundle / "bin/lighttable-desktop-shell").is_file()
        for binary in (server.RUST_BIN, server.RUST_WORKER_BIN):
            assert binary and binary.is_file(), binary
        assert server.RUST_BIN.name == "spektrafilm-rs"
        assert server.RUST_WORKER_BIN.name == "lighttable-engine"
        assert server.fp.FILM_PROFILES, "Film stock catalog is empty"
        for profile in server.color_pipeline.ICC_PROFILES.values():
            assert profile.is_file(), profile
        source = photos / "source.jpg"
        pixels = np.repeat(np.linspace(8, 245, 48, dtype=np.uint8)[None, :, None], 32, axis=0)
        pixels = np.repeat(pixels, 3, axis=2)
        Image.fromarray(pixels).save(source)
        converted = scratch / "converted.tif"
        server.platform_image.convert_processed_to_tiff(
            source, converted, app_root=resources, output_space="display_p3", force_portable=True)
        with Image.open(converted) as image:
            assert image.size == (48, 32)
            assert image.info.get("icc_profile")

        render_source = scratch / "render-source.tif"
        tifffile.imwrite(render_source, pixels.astype(np.uint16) * 257, photometric="rgb")
        # Current import/export paths also read compressed, high-bit TIFFs.
        # Exercise the newly pinned native codec wheel in the relocated runtime.
        compressed = scratch / "compressed-source.tif"
        tifffile.imwrite(compressed, pixels.astype(np.uint16) * 257,
                         photometric="rgb", compression="deflate", predictor=True)
        assert np.array_equal(tifffile.imread(compressed), pixels.astype(np.uint16) * 257)
        film = "kodak_portra_400"
        paper = server.fp.default_paper(film)
        params = server.fp.rust_params_json({"stock": film, "paper": paper})
        resident_output = scratch / "resident.png"
        request = {"id": 1, "input": str(render_source), "output": str(resident_output),
                   "data_dir": str(resources / "engine/data"), "film": film, "paper": paper,
                   "params": params}
        completed = subprocess.run([str(server.RUST_WORKER_BIN)], input=json.dumps(request) + "\n",
                                   capture_output=True, text=True, check=True, timeout=180)
        result = json.loads(completed.stdout)
        assert result.get("ok"), result
        with Image.open(resident_output) as image:
            assert image.size == (48, 32)
            assert np.ptp(np.asarray(image)) >= 16
        params_file = scratch / "params.json"
        params_file.write_text(json.dumps(params))
        export_output = scratch / "export.png"
        subprocess.run([str(server.RUST_BIN), "process", str(render_source), "-o", str(export_output),
                        "--film", film, "--paper", paper, "--data-dir", str(resources / "engine/data"),
                        "--params", str(params_file)], capture_output=True, text=True, check=True, timeout=180)
        with Image.open(export_output) as image:
            assert image.size == (48, 32)
            assert np.ptp(np.asarray(image)) >= 16

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        os.environ["LIGHTTABLE_PORT"] = str(port)
        log_path = scratch / "server-output.log"
        with log_path.open("w+") as log:
            process = subprocess.Popen([sys.executable, "-B", "-u", str(resources / "server.py")],
                                       stdout=log, stderr=log, start_new_session=True)
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Packaged server exited: " + log_path.read_text()[-6000:])
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                            assert response.status == 200
                            json.load(response)
                        break
                    except (urllib.error.URLError, TimeoutError):
                        time.sleep(0.2)
                else:
                    raise RuntimeError("Packaged server did not become ready: " + log_path.read_text()[-6000:])
                subprocess.run([str(bundle / "bin/lighttable"), "--port", str(port), "status", "--json"],
                               capture_output=True, text=True, check=True, timeout=30)
            finally:
                # Stop the process group even if the server exited first, so
                # a render worker cannot outlive a failed smoke check.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                if process.poll() is None:
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
    print("Packaged Linux runtime smoke passed: relocated Python, imports, profiles, ICC, CPU preview/export, HTTP 200 and CLI")


if __name__ == "__main__":
    main()
