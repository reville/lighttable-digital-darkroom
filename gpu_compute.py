"""Float32 application compute over the bundled resident Rust/GPU worker.

This worker is separate from interactive film rendering: exports and merges
cannot occupy its admission queue. File transport preserves every float bit;
there is no TIFF/PNG encode or 8-bit native preview surface in this path.
"""
from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time

import numpy as np

_LOCK = threading.RLock()
_PROCESS = None
_REPLIES = None
_REQUEST_ID = 0
_RETRY_AFTER = 0.0
_BINARY = Path(__file__).resolve().parent / "engine" / (
    "lighttable-engine.exe" if os.name == "nt" else "lighttable-engine")


def close() -> None:
    """Release the owned worker at shutdown (also usable by benchmark scripts)."""
    global _PROCESS, _REPLIES
    with _LOCK:
        process, _PROCESS = _PROCESS, None
        _REPLIES = None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()


atexit.register(close)


def _start():
    global _PROCESS, _REPLIES
    if _PROCESS is not None and _PROCESS.poll() is None:
        return _PROCESS
    close()
    flags = ({"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
             if os.name == "nt" else {})
    process = subprocess.Popen([str(_BINARY)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, **flags)
    replies = queue.Queue()

    def read_output():
        try:
            for line in process.stdout:
                replies.put(line)
        except (OSError, ValueError):
            pass
        finally:
            replies.put(None)

    threading.Thread(target=read_output, daemon=True,
                     name="gpu-compute-output").start()
    _PROCESS, _REPLIES = process, replies
    return process


def compute(operation: str, image: np.ndarray, parameters,
            output_shape: tuple, *, grade: dict | None = None) -> np.ndarray:
    """Compute or raise; callers retain their precise CPU fallback on failure.

    Inputs remain owned by the caller. The output owns a separate float32 array.
    A bounded timeout handles adapter loss and older installed worker binaries.
    """
    global _REQUEST_ID, _RETRY_AFTER
    if os.environ.get("LIGHTTABLE_GPU_COMPUTE", "1") == "0":
        raise RuntimeError("GPU application compute disabled")
    samples = np.ascontiguousarray(image, dtype=np.float32)
    expected = int(np.prod(output_shape))
    if not samples.size or expected <= 0:
        raise ValueError("empty GPU compute image")
    with _LOCK:
        if time.monotonic() < _RETRY_AFTER:
            raise RuntimeError("GPU application compute is temporarily unavailable")
        try:
            process = _start()
            with tempfile.TemporaryDirectory(prefix="lighttable-gpu-compute-") as temp:
                source = Path(temp) / "input.f32"
                destination = Path(temp) / "output.f32"
                samples.tofile(source)
                _REQUEST_ID += 1
                request = {"id": _REQUEST_ID, "command": "compute_float",
                    "operation": operation, "input": str(source), "output": str(destination),
                    "parameters": [float(value) for value in parameters]}
                if grade is not None:
                    request["grade"] = grade
                process.stdin.write(json.dumps(request, allow_nan=False) + "\n")
                process.stdin.flush()
                try:
                    line = _REPLIES.get(timeout=180)
                except queue.Empty as error:
                    raise TimeoutError("GPU application compute timed out") from error
                if not line:
                    raise RuntimeError("GPU application compute worker exited")
                result = json.loads(line)
                if result.get("id") != _REQUEST_ID or not result.get("ok"):
                    raise RuntimeError(result.get("error") or "GPU compute failed")
                if destination.stat().st_size != expected * 4:
                    raise RuntimeError("GPU output dimensions do not match")
                return np.fromfile(destination, dtype=np.float32).reshape(output_shape)
        except Exception:
            close()
            _RETRY_AFTER = time.monotonic() + 60.0
            raise
