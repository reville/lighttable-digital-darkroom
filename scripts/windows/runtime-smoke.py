#!/usr/bin/env python3
"""Exercise the packaged Windows runtime without opening the desktop UI."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import build_opener, ProxyHandler


def smoke_environment(temp_path: Path) -> dict[str, str]:
    # Never let a developer's selected library or background-service settings
    # leak into this smoke test. All catalog/support/cache files are temporary.
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("LIGHTTABLE_")}
    environment.update({
        "LIGHTTABLE_DIR": str(temp_path / "photos"),
        "LIGHTTABLE_CATALOG_FILE": str(temp_path / "catalog" / "library.sqlite3"),
        "LIGHTTABLE_CACHE_DIR": str(temp_path / "cache"),
        "LIGHTTABLE_PREFS_FILE": str(temp_path / "prefs.json"),
        "LIGHTTABLE_PRESETS_FILE": str(temp_path / "presets.json"),
        "LIGHTTABLE_AI_DIR": str(temp_path / "ai"),
        "LIGHTTABLE_INSTANCE_DIR": str(temp_path / "instances"),
        "LIGHTTABLE_STARTUP_FILE": str(temp_path / "startup.json"),
        "LIGHTTABLE_PORT": "0",
        "LIGHTTABLE_SAFE_MODE": "1",
        "LIGHTTABLE_HEADLESS": "1",
        "NUMBA_CACHE_DIR": str(temp_path / "compiled-runtime"),
    })
    (temp_path / "photos").mkdir()
    return environment


def check_server_startup(resources: Path, temp_path: Path,
                         environment: dict[str, str], *, timeout: float = 60) -> None:
    """Check real packaged startup, with a bound and unconditional child cleanup."""
    startup = Path(environment["LIGHTTABLE_STARTUP_FILE"])
    log = temp_path / "server.log"
    # Local health requests must not use a machine's configured HTTP proxy.
    opener = build_opener(ProxyHandler({}))
    with log.open("wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-B", "-u", str(resources / "server.py")],
            cwd=temp_path, env=environment, stdout=output, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"Packaged server exited with {process.returncode}")
                try:
                    report = json.loads(startup.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    report = {}
                if report.get("phase") == "failed":
                    raise RuntimeError(f"Packaged server startup failed: {report.get('detail')}")
                if report.get("phase") == "ready":
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError(f"Packaged server did not start within {timeout:g} seconds")

            base = f"http://127.0.0.1:{int(report['port'])}"
            with opener.open(base + "/api/health", timeout=10) as response:
                assert response.status == 200
                health = json.load(response)
            assert health["ok"] is True, health
            assert health["pid"] == process.pid, health
            assert Path(health["catalog"]).resolve() == Path(
                environment["LIGHTTABLE_CATALOG_FILE"]).resolve(), health
            with opener.open(base + "/", timeout=10) as response:
                assert response.status == 200
                assert b"<html" in response.read().lower()
            with opener.open(base + "/api/options", timeout=10) as response:
                assert response.status == 200
                assert isinstance(json.load(response), dict)
        except Exception as error:
            output.flush()
            raise RuntimeError(f"{error}\n{log.read_text(encoding='utf-8', errors='replace')[-12000:]}") from error
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def check_file_identity(temp_path: Path) -> None:
    """Exercise native file-revision handles in the packaged interpreter."""
    import file_identity

    source = temp_path / "identity.bin"
    source.write_bytes(b"original pixels")
    original = source.stat()
    signature = file_identity.stat_signature(original, path=source)
    key = file_identity.signature_key(original, path=source)
    digest = file_identity.content_hash(source, expected_signature=key)
    with source.open("rb") as stream:
        stream.seek(3)
        assert file_identity.stat_signature(os.fstat(stream.fileno()),
                                            fd=stream.fileno()) == signature
        assert stream.tell() == 3, "Identity moved a borrowed file position"
        assert stream.read() == b"ginal pixels", "Identity closed a borrowed handle"

    # Keep the inode, length, and modification time while changing the bytes.
    # ChangeTime can also stay equal within one Windows clock tick. The fresh
    # complete content signature must distinguish this revision without sleeps.
    with source.open("r+b") as stream:
        stream.write(b"replaced pixels")
    os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
    current = source.stat()
    assert file_identity.stat_signature(current, path=source)[:4] == signature[:4]
    current_signature = file_identity.stat_signature(current, path=source)
    assert file_identity.signature_key(current, path=source) != key, {
        "before": signature, "after": current_signature,
    }
    assert file_identity.content_hash(source) != digest
    try:
        file_identity.content_hash(source, expected_signature=key)
    except OSError:
        pass
    else:
        raise AssertionError("Replaced bytes passed the queued source guard")

    if os.name == "nt":
        api = file_identity._windows_bindings()
        with source.open("rb") as stream:
            locked_fd = api.reopen_content_fd(stream.fileno())
            try:
                try:
                    with source.open("r+b"):
                        pass
                except PermissionError:
                    pass
                else:
                    raise AssertionError("Content identity allowed a concurrent writer")
            finally:
                os.close(locked_fd)
        other = temp_path / "other-identity.bin"
        other.write_bytes(b"replaced pixels")
        os.utime(other, ns=(current.st_atime_ns, current.st_mtime_ns))
        try:
            file_identity.stat_signature(current, path=other)
        except OSError:
            pass
        else:
            raise AssertionError("Native metadata handle accepted another file's stat")
    print("Packaged file-revision identity smoke passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("resources", type=Path)
    parser.add_argument("shell", type=Path, nargs="?",
                        help="When supplied, also require the compiled release executables")
    args = parser.parse_args()
    resources = args.resources.resolve()
    vendor = (resources / "vendor" / "spektrafilm" / "src").resolve()
    runtime_paths = {Path(entry).resolve() for entry in sys.path if entry}
    assert resources in runtime_paths
    assert vendor in runtime_paths

    with tempfile.TemporaryDirectory(prefix="lighttable-windows-smoke-") as temp:
        temp_path = Path(temp)
        environment = smoke_environment(temp_path)
        for key in list(os.environ):
            if key.startswith("LIGHTTABLE_"):
                del os.environ[key]
        os.environ.update(environment)

        from PIL import Image
        import OpenImageIO  # noqa: F401
        import exiv2  # noqa: F401
        import numpy as np
        import rawpy  # noqa: F401
        import tifffile
        import camera_profile  # noqa: F401 -- optional local runtime dependency
        import lighttable_cli.__main__  # noqa: F401
        import server

        if args.shell is not None:
            assert args.shell.is_file()
            assert server.RUST_BIN.name == "spektrafilm-rs.exe"
            assert server.RUST_WORKER_BIN is not None
            assert server.RUST_WORKER_BIN.name == "lighttable-engine.exe"
            assert server.RUST_BIN.is_file()
            assert server.RUST_WORKER_BIN.is_file()
        for profile in server.color_pipeline.ICC_PROFILES.values():
            assert profile.is_file(), profile

        source = temp_path / "source.jpg"
        destination = temp_path / "converted.tif"
        Image.new("RGB", (32, 24), (80, 120, 160)).save(source)
        server.platform_image.convert_processed_to_tiff(
            source,
            destination,
            app_root=resources,
            output_space="display_p3",
            force_portable=True,
        )
        with tifffile.TiffFile(destination) as converted:
            assert converted.asarray().shape == (24, 32, 3)
            assert converted.pages[0].tags.get(34675) is not None

        # Exercise the Windows high-precision ICC backend in the real embedded
        # interpreter, where a missing/incompatible native wheel would surface.
        precise_source = temp_path / "gradient-16bit.tif"
        precise_destination = temp_path / "converted-16bit.tif"
        gradient = np.repeat(np.linspace(0, 65535, 1024, dtype=np.uint16)[None, :, None], 3, axis=2)
        tifffile.imwrite(precise_source, gradient, photometric="rgb", metadata=None)
        server.platform_image.convert_processed_to_tiff(
            precise_source, precise_destination, app_root=resources,
            output_space="srgb", force_portable=True,
        )
        with tifffile.TiffFile(precise_destination) as converted:
            pixels = converted.asarray()
            assert pixels.dtype == np.uint16
            assert pixels.shape == gradient.shape
            assert np.unique(pixels[..., 0]).size > 256
            assert converted.pages[0].tags.get(34675) is not None

        check_file_identity(temp_path)
        check_server_startup(resources, temp_path, environment)

    print("Packaged Windows runtime smoke passed")


if __name__ == "__main__":
    main()
