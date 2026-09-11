"""One way to reach the film engine, shared by the audit and the goldens.

Both use the resident `lighttable-engine` built from `rust-engine/`, the same
implementation the film gate puts under test, rather than the prebuilt
`engine/spektrafilm-rs` binary. PROCESSING-CORRECTNESS.md is explicit that the
legacy binary is not the implementation under test, and pinning goldens to a
binary that is not built from this repository would freeze pixels nobody can
reproduce from source.

The engine is resident by design: it reads one JSON request per line and
keeps its profile and cache state between them. A `Session` holds one process
open for a whole sweep, which is both faster than respawning and closer to
how the application actually drives it.

Set `LIGHTTABLE_FILM_ENGINE` to point at a different build.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import tifffile

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import film_pipeline as fp  # noqa: E402

DATA = APP / "engine" / "data"
BUILD_HINT = ("build it with:\n"
              "  CARGO_TARGET_DIR=build/processing-cargo cargo build "
              "--manifest-path rust-engine/Cargo.toml --locked")


def binary() -> Path:
    override = os.environ.get("LIGHTTABLE_FILM_ENGINE")
    if override:
        return Path(override).resolve()
    return (APP / "build/processing-cargo/debug/lighttable-engine").resolve()


def available() -> bool:
    return binary().is_file() and DATA.is_dir()


def require() -> None:
    if not available():
        raise RuntimeError(f"needs the film engine at {binary()}; {BUILD_HINT}")


class Session:
    """One long-lived engine process, driven request by request."""

    def __init__(self, timeout: int = 180):
        require()
        self.timeout = timeout
        self._next_id = 0
        self._process = subprocess.Popen(
            [str(binary())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, SPEKTRAFILM_BACKEND="cpu", RAYON_NUM_THREADS="2"))

    def render(self, source: Path, params: dict, output: Path) -> np.ndarray:
        """Render one recipe and return it as float RGB in 0..1.

        The film tuning is applied to the pixels before the engine sees them,
        exactly as render_cli does, so a tuned recipe is not silently untuned.
        """
        resolved = fp.clean_params(params)
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        # A stale file from an earlier case must never stand in for a result
        # the engine failed to write.
        output.unlink(missing_ok=True)
        self._next_id += 1
        with fp.prepared_input_file(str(source), resolved) as prepared:
            request = {"id": self._next_id, "input": str(prepared),
                       "output": str(output), "bit_depth": 16,
                       "data_dir": str(DATA), "film": resolved["stock"],
                       "paper": resolved["paper"],
                       "scan_film": resolved["stock"] in fp.POSITIVE_STOCKS,
                       "params": fp.rust_params_json(resolved)}
            response = self._exchange(request)
        if not response.get("ok"):
            raise RuntimeError(f"engine refused the request: {response}")
        if response.get("id") != self._next_id:
            raise RuntimeError(
                f"engine answered request {response.get('id')} while "
                f"{self._next_id} was outstanding")
        if "cpu" not in str(response.get("backend", "")).lower():
            raise RuntimeError(
                f"engine fell back to {response.get('backend')!r}; a CPU render "
                "is required so the result is reproducible on any machine")
        if not output.is_file():
            raise RuntimeError("engine reported success but wrote no output")
        pixels = tifffile.imread(output)
        if pixels.dtype == np.uint16:
            return pixels.astype(np.float32) / 65535.0
        if pixels.dtype == np.uint8:
            return pixels.astype(np.float32) / 255.0
        return np.clip(np.asarray(pixels, dtype=np.float32), 0.0, 1.0)

    def _exchange(self, request: dict) -> dict:
        if self._process.poll() is not None:
            raise RuntimeError(
                f"engine exited {self._process.returncode} before this request")
        try:
            self._process.stdin.write(json.dumps(request) + "\n")
            self._process.stdin.flush()
            line = self._process.stdout.readline()
        except (BrokenPipeError, ValueError) as error:
            raise RuntimeError(f"engine closed its pipe: {error}") from error
        if not line:
            stderr = self._process.stderr.read() if self._process.stderr else ""
            raise RuntimeError(f"engine produced no response: {stderr[-500:]}")
        return json.loads(line)

    def close(self) -> None:
        process = self._process
        if process.poll() is None:
            try:
                process.stdin.close()
                process.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                process.kill()
                process.wait(timeout=5)
        for stream in (process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()


@contextmanager
def session(timeout: int = 180):
    """A resident engine for the length of a sweep, always shut down after."""
    engine = Session(timeout=timeout)
    try:
        yield engine
    finally:
        engine.close()


def render(source: Path, params: dict, output: Path, **_ignored) -> np.ndarray:
    """Render a single recipe in its own short-lived process."""
    with session() as engine:
        return engine.render(source, params, output)
