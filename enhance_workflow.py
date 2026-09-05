"""Learned denoise and optional Enhance (super-resolution) inference.

WHAT IS REAL HERE AND WHAT IS NOT
---------------------------------
The macOS release bundles a converted SCUNet denoise model. Development builds
remain usable without that large local artifact and then report denoise as
unavailable. Super-resolution has no bundled model. There is never an identity
fallback that can make a missing model look like a successful operation.

Inference is delegated to ``build/LightTableEnhance``, a narrow Core ML helper
reached through the same argv-dispatch convention the Vision helper uses::

    LightTableEnhance --denoise in.tile out.tile --strength 0.60
    LightTableEnhance --denoise-batch manifest.tsv --strength 0.60

The exchange is bare float32 planar RGB, not TIFF, so no image reader can
colour-manage working pixels in transit. A whole image's tiles run in one
helper process to amortise Core ML compilation and model loading.

The seam between this module and inference is one injectable callable::

    runner(tile_array, mode, params) -> tile_array

``tile_array`` is float32 RGB in 0..1. The indirection keeps tiling, cache
identity, cancellation, and edge handling testable with an injected runner.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import selectors
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import tifffile
from skimage import transform as image_transform

import color_pipeline
import durable_io


MODES = ("denoise", "upscale")
DEFAULT_TILE = 512
DEFAULT_OVERLAP = 64

MIN_TILE = 64
MAX_TILE = 2048
MIN_OVERLAP = 8
SUPPORTED_SCALES = (1, 2, 4)

# A panorama-sized ceiling on the reassembled result, so a 4x request on a
# large master fails with a clear message instead of exhausting memory.
MAX_OUTPUT_PIXELS = 120_000_000

HELPER_TIMEOUT = 300.0
HELPER_ENV = "LIGHTTABLE_ENHANCE_HELPER"
DEFAULT_HELPER = "build/LightTableEnhance"
MODEL_DIR_ENV = "LIGHTTABLE_MODEL_DIR"
MODEL_FILES = {"denoise": "denoise.mlpackage", "upscale": "upscale.mlpackage"}
MODEL_INDEX_FILE = "models.json"

# Windows needs ONNX Runtime and DirectML in packaging/runtime-windows.lock
# before this can be enabled there; until then it reports as unsupported.
SUPPORTED_PLATFORMS = ("darwin",)

ENHANCED_FOLDER_NAME = "LightTable Enhanced"

# Bump with any change to denoise_fingerprint's meaning, and bump the
# matching token in color_pipeline.raw_decode_fingerprint at the same time.
DENOISE_FINGERPRINT_VERSION = "ld2"

DISCLOSURE = (
    "Enhance output is model-generated detail, not measured detail. "
    "The denoise model may be bundled; any model used is identified in this "
    "manifest. Super-resolution remains unavailable unless installed separately."
)

APP = Path(__file__).resolve().parent


class EnhanceUnavailable(RuntimeError):
    """Raised when no model, no helper, or an unsupported platform blocks a run."""


# ------------------------------------------------------------- discovery ----


def user_model_root() -> Path:
    """Writable directory for user-installed models.

    ``LIGHTTABLE_MODEL_DIR`` wins; otherwise Application Support on macOS and
    ``%LOCALAPPDATA%`` on Windows, matching ``catalog.default_catalog_path``.
    """
    override = os.environ.get(MODEL_DIR_ENV)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        return base / "LightTable" / "Models"
    return Path.home() / "Library/Application Support/LightTable" / "Models"


def bundled_model_root() -> Path:
    """Read-only model directory in a packaged app, or the source tree."""
    packaged = APP.parent / "models"
    return packaged if packaged.is_dir() else APP / "models"


def model_root() -> Path:
    """First model directory with an index or a known package.

    A user-installed model wins over the bundled package, which keeps local
    testing and future model replacement possible without modifying the app.
    """
    writable = user_model_root()
    if ((writable / MODEL_INDEX_FILE).is_file()
            or any((writable / name).exists() for name in MODEL_FILES.values())):
        return writable
    return bundled_model_root() if bundled_model_root().is_dir() else writable


def helper_path() -> Path:
    """Path to the inference helper binary."""
    override = os.environ.get(HELPER_ENV)
    if override:
        return Path(override).expanduser()
    return APP / DEFAULT_HELPER


def available_models() -> dict:
    """Filesystem truth: which models and which helper are actually present."""
    helper = helper_path()
    root = model_root()
    state = {mode: (root / MODEL_FILES[mode]).exists() for mode in MODES}
    state["helper"] = helper.is_file() and os.access(helper, os.X_OK)
    return state


def _model_identity(path: Path) -> str:
    """Short token that changes when the installed model file changes."""
    try:
        stat = path.stat()
    except OSError:
        return "none"
    return hashlib.sha256(
        f"{path.name}|{stat.st_size}|{stat.st_mtime_ns}".encode()
    ).hexdigest()[:12]


def _model_index() -> dict:
    """Optional ``models.json`` written by the conversion script."""
    try:
        data = json.loads((model_root() / MODEL_INDEX_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def model_info(mode: str) -> dict:
    """Identity of the model a mode would use, installed or not."""
    mode = _mode_or_error(mode)
    path = model_root() / MODEL_FILES[mode]
    installed = path.exists()
    entry = _model_index().get(mode)
    entry = entry if isinstance(entry, dict) else {}
    return {
        "mode": mode,
        "name": MODEL_FILES[mode],
        "path": str(path),
        "installed": installed,
        "identity": (str(entry.get("sha256", ""))[:12]
                     or (_model_identity(path) if installed else "none")),
        "version": str(entry.get("version", "")),
        "license": str(entry.get("license", "")),
        "source": str(entry.get("source", "")),
        "bundled": installed and path.parent == bundled_model_root(),
    }


def capabilities() -> dict:
    """What the UI shows, including a plain reason when Enhance is off."""
    state = available_models()
    supported = sys.platform in SUPPORTED_PLATFORMS
    helper = helper_path()
    modes = {mode: bool(state[mode] and state["helper"] and supported)
             for mode in MODES}
    reason = ""
    if not supported:
        reason = (
            f"Enhance needs the macOS inference helper; there is no build for "
            f"{sys.platform} yet"
        )
    elif not state["helper"]:
        reason = (
            f"The Enhance helper has not been built ({helper}); build it with "
            "build-app.sh before using Enhance"
        )
    elif not any(state[mode] for mode in MODES):
        reason = (
            f"No enhancement model is installed in {model_root()}. Run the "
            "model fetch and conversion scripts for this development build"
        )
    return {
        "available": bool(supported and state["helper"]
                          and any(state[mode] for mode in MODES)),
        "reason": reason,
        "platform": sys.platform,
        "platformSupported": supported,
        "modes": modes,
        "helper": {"path": str(helper), "installed": bool(state["helper"])},
        "modelRoot": str(model_root()),
        "models": {mode: model_info(mode) for mode in MODES},
        "bundledModel": any(info["bundled"] for info in
                            (model_info(mode) for mode in MODES)),
        "tile": DEFAULT_TILE,
        "overlap": DEFAULT_OVERLAP,
        "scales": list(SUPPORTED_SCALES),
        "disclosure": DISCLOSURE,
    }


# --------------------------------------------------------------- request ----


def _mode_or_error(mode) -> str:
    value = str(mode or "").strip().lower()
    if value not in MODES:
        raise ValueError(f"unknown enhance mode: {mode!r}")
    return value


def _clamp_float(value, low: float, high: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if not math.isfinite(number):
        return float(fallback)
    return float(min(max(number, low), high))


def _snap_scale(value) -> int:
    """Clamp to 1..4 then step down to the nearest supported factor (3 -> 2)."""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return 2
    number = min(max(number, SUPPORTED_SCALES[0]), SUPPORTED_SCALES[-1])
    return [factor for factor in SUPPORTED_SCALES if factor <= number][-1]


def clean_request(raw: dict | None) -> dict:
    """Validate and clamp an Enhance request from the client.

    Fields are clamped independently; ``scale`` is kept even for denoise so
    the shape is stable. :func:`run_model` is what forces the effective
    factor to 1 for denoise.
    """
    raw = raw if isinstance(raw, dict) else {}
    mode = str(raw.get("mode", MODES[0])).strip().lower()
    if mode not in MODES:
        mode = MODES[0]
    tile = int(_clamp_float(raw.get("tile", DEFAULT_TILE),
                            MIN_TILE, MAX_TILE, DEFAULT_TILE))
    overlap = int(_clamp_float(raw.get("overlap", DEFAULT_OVERLAP),
                               MIN_OVERLAP, max(MIN_OVERLAP, tile // 2),
                               min(DEFAULT_OVERLAP, tile // 2)))
    return {
        "mode": mode,
        "strength": round(_clamp_float(raw.get("strength", 1.0), 0.0, 1.0, 1.0), 4),
        "scale": _snap_scale(raw.get("scale", 2)),
        "tile": tile,
        "overlap": min(overlap, tile // 2),
    }


# ---------------------------------------------------------------- tiling ----


def _tile_starts(extent: int, tile: int, overlap: int) -> list[int]:
    """Evenly spaced tile origins that cover ``extent`` with at least ``overlap``.

    Spacing the origins evenly rather than striding and clamping the last one
    avoids a final tile that nearly duplicates its neighbour, and keeps every
    seam the same width.
    """
    extent, tile = int(extent), int(tile)
    if extent <= tile:
        return [0]
    stride = max(1, tile - int(overlap))
    count = max(2, math.ceil((extent - int(overlap)) / stride))
    span = extent - tile
    return sorted({int(round(index * span / (count - 1)))
                   for index in range(count)})


def tile_image(image, tile: int = DEFAULT_TILE,
               overlap: int = DEFAULT_OVERLAP) -> list[dict]:
    """Cut an RGB image into overlapping tiles for bounded-memory inference.

    Each returned dict is ``{"image", "y0", "x0", "y1", "x1", "row", "col",
    "scale"}``; the rectangle is in *source* pixels and half-open, and
    ``scale`` is the output magnification (1 until a runner upscales the
    tile). Tile pixels are copies, so a runner may write in place.
    """
    source = color_pipeline.as_float_rgb(image)
    height, width = source.shape[:2]
    tile = max(1, int(tile))
    overlap = max(0, min(int(overlap), tile // 2))
    rows = _tile_starts(height, tile, overlap)
    columns = _tile_starts(width, tile, overlap)
    tiles = []
    for row, y0 in enumerate(rows):
        y1 = min(height, y0 + tile)
        for column, x0 in enumerate(columns):
            x1 = min(width, x0 + tile)
            tiles.append({
                "image": source[y0:y1, x0:x1].copy(),
                "y0": y0, "x0": x0, "y1": y1, "x1": x1,
                "row": row, "col": column, "scale": 1,
            })
    return tiles


def _ramp(width: int) -> np.ndarray:
    """Raised-cosine ramp rising from just above 0 to just below 1.

    Half-sample offsets keep every weight strictly positive, so no pixel can
    end up with zero total weight even in a degenerate layout.
    """
    index = np.arange(max(1, int(width)), dtype=np.float64)
    return (0.5 - 0.5 * np.cos(np.pi * (index + 0.5) / max(1, int(width)))
            ).astype(np.float32)


def _axis_window(length: int, feather: int,
                 lead: bool, trail: bool) -> np.ndarray:
    """1-D blend window, feathered only on edges that face another tile."""
    window = np.ones(max(1, int(length)), dtype=np.float32)
    size = window.shape[0]
    feather = max(1, min(int(feather), size // 2)) if size > 1 else 1
    ramp = _ramp(feather)
    if lead:
        window[:feather] *= ramp
    if trail:
        window[size - feather:] *= ramp[::-1]
    return window


def merge_tiles(tiles, shape, overlap: int = DEFAULT_OVERLAP) -> np.ndarray:
    """Reassemble tiles with a raised-cosine feather across every seam.

    ``shape`` is the *source* image shape given to :func:`tile_image`; the
    result is ``scale`` times that in height and width, where ``scale`` is
    the magnification recorded on the tiles. Feathering uses a raised-cosine
    (Hann) ramp rather than a linear one so the blend has no derivative step
    at the ends of the ramp, which is what makes a seam visible in flat sky.

    Because each pixel is divided by its accumulated weight, an identity
    runner reconstructs the source exactly to float32 rounding
    (~1e-7 for values in 0..1), for any image size and any tile layout.
    """
    tiles = list(tiles)
    if not tiles:
        raise ValueError("merge_tiles needs at least one tile")
    height, width = int(shape[0]), int(shape[1])
    if height < 1 or width < 1:
        raise ValueError("merge_tiles needs a positive source shape")

    scales = {int(tile.get("scale", 1) or 1) for tile in tiles}
    if len(scales) != 1:
        raise ValueError("all tiles must share one output scale")
    scale = scales.pop()
    if scale < 1:
        raise ValueError("tile scale must be at least 1")

    out_height, out_width = height * scale, width * scale
    if out_height * out_width > MAX_OUTPUT_PIXELS:
        raise ValueError("the enhanced result is larger than the supported size")
    channels = int(np.asarray(tiles[0]["image"]).shape[2])

    accumulated = np.zeros((out_height, out_width, channels), dtype=np.float32)
    total = np.zeros((out_height, out_width), dtype=np.float32)
    feather = max(1, int(overlap)) * scale
    for tile in tiles:
        y0, x0 = int(tile["y0"]), int(tile["x0"])
        y1, x1 = int(tile["y1"]), int(tile["x1"])
        patch = np.asarray(tile["image"], dtype=np.float32)
        expected = ((y1 - y0) * scale, (x1 - x0) * scale, channels)
        if patch.shape != expected:
            raise ValueError(
                f"tile at ({y0},{x0}) is {patch.shape}, expected {expected}")
        window = (_axis_window(expected[0], feather, y0 > 0, y1 < height)[:, None]
                  * _axis_window(expected[1], feather, x0 > 0, x1 < width)[None, :])
        rows = slice(y0 * scale, y1 * scale)
        columns = slice(x0 * scale, x1 * scale)
        accumulated[rows, columns] += patch * window[..., None]
        total[rows, columns] += window
    result = accumulated / np.maximum(total, 1e-8)[..., None]
    return np.clip(result, 0.0, 1.0).astype(np.float32)


# ------------------------------------------------------------- inference ----


TILE_MAGIC = b"FLT0"


def write_tile_file(path: Path, tile: np.ndarray) -> None:
    """Write one working tile as planar float32 with a bare header.

    Deliberately not an image format. Core Image colour-manages anything it
    recognises, so handing the helper a TIFF meant the model received values
    that had been through an sRGB-to-linear conversion nobody asked for --
    measured as a 17 dB *loss* against a known-clean reference. These are
    mid-pipeline working pixels already in the caller's space, so the exchange
    carries no colour information and converts nothing.

        magic "FLT0" | int32 width | int32 height | int32 channels
        float32 samples, planar, channel-major, host byte order
    """
    array = np.ascontiguousarray(
        color_pipeline.as_float_rgb(tile).transpose(2, 0, 1).astype(np.float32))
    height, width = array.shape[1], array.shape[2]
    with path.open("wb") as handle:
        handle.write(TILE_MAGIC)
        handle.write(struct.pack("<iii", width, height, 3))
        handle.write(array.tobytes())


def read_tile_file(path: Path) -> np.ndarray:
    with path.open("rb") as handle:
        if handle.read(4) != TILE_MAGIC:
            raise RuntimeError(f"{path.name} is not a LightTable tile")
        width, height, channels = struct.unpack("<iii", handle.read(12))
        expected = width * height * channels
        data = np.frombuffer(handle.read(), dtype=np.float32)
    if data.size < expected:
        raise RuntimeError(f"{path.name} is truncated")
    return data[:expected].reshape(channels, height, width).transpose(1, 2, 0)


def _run_helper(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, check=False,
            timeout=HELPER_TIMEOUT,
            env=dict(os.environ, **{MODEL_DIR_ENV: str(model_root())}))
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"the Enhance helper timed out after {HELPER_TIMEOUT:.0f}s"
        ) from error
    except OSError as error:
        raise EnhanceUnavailable(
            f"the Enhance helper could not be run: {error}") from error


def _require_helper(mode: str) -> str:
    mode = _mode_or_error(mode)
    state = available_models()
    if not state["helper"]:
        raise EnhanceUnavailable(
            f"the Enhance helper has not been built ({helper_path()})")
    if not state[mode]:
        raise EnhanceUnavailable(
            f"no {mode} model is installed in {model_root()}")
    return mode


def helper_runner(tile, mode: str, params: dict):
    """Default runner: one helper subprocess per tile.

    Prefer :func:`helper_batch_runner` for a whole image. Loading a compiled
    model costs far more than running one tile through it, so per-tile
    invocation is only sensible for a single tile.

    Raises :class:`EnhanceUnavailable` when the helper or the model is missing,
    which is this repository's normal state: nothing is bundled.
    """
    mode = _require_helper(mode)
    with tempfile.TemporaryDirectory(prefix="lighttable-enhance-") as folder:
        source = Path(folder) / "in.tile"
        target = Path(folder) / "out.tile"
        original = color_pipeline.as_float_rgb(tile)
        padded = _pad_to_tile(original, int(params.get("tile", DEFAULT_TILE))) \
            if mode == "denoise" else original
        write_tile_file(source, padded)
        if mode == "denoise":
            argv = [str(helper_path()), "--denoise", str(source), str(target),
                    "--strength", f"{float(params.get('strength', 1.0)):.4f}"]
        else:
            argv = [str(helper_path()), "--upscale",
                    str(int(params.get("scale", 2))), str(source), str(target)]
        completed = _run_helper(argv)
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(detail[-300:] or f"{mode} failed")
        if not target.is_file():
            raise RuntimeError(f"the Enhance helper wrote no {mode} result")
        result = read_tile_file(target)
        if mode == "denoise":
            return result[:original.shape[0], :original.shape[1]].copy()
        return result


def _pad_to_tile(tile: np.ndarray, size: int) -> np.ndarray:
    """Reflect-pad a short fixed-graph tile without altering its real pixels."""
    source = color_pipeline.as_float_rgb(tile)
    height, width = source.shape[:2]
    if height > size or width > size:
        raise ValueError(f"tile {width}x{height} is larger than {size}")
    if height == size and width == size:
        return source
    mode = "reflect" if height > 1 and width > 1 else "edge"
    return np.pad(source, ((0, size - height), (0, size - width), (0, 0)),
                  mode=mode).astype(np.float32, copy=False)


def _cancel_requested(cancel) -> bool:
    if cancel is None:
        return False
    if callable(cancel):
        return bool(cancel())
    if hasattr(cancel, "is_set"):
        return bool(cancel.is_set())
    return bool(cancel.get("cancelled")) if isinstance(cancel, dict) else bool(cancel)


def _publish_progress(status, progress: int, total: int) -> None:
    if callable(status):
        status({"progress": int(progress), "total": int(total)})
    elif isinstance(status, dict):
        status.update(progress=int(progress), total=int(total))


def helper_batch_runner(tiles: list, mode: str, params: dict, *,
                        status=None, cancel=None) -> list:
    """Run every tile of an image through one helper process.

    Compiling and loading the model dominates: about thirty seconds the first
    time and a second afterwards, against roughly a tenth of a second per tile.
    Spawning a process per tile turned a twenty-four megapixel image into more
    than an hour of almost entirely wasted work.
    """
    if not tiles:
        return []          # nothing to do cannot fail, model or no model
    mode = _require_helper(mode)
    if mode != "denoise":
        raise EnhanceUnavailable("batch mode currently covers denoise only")
    with tempfile.TemporaryDirectory(prefix="lighttable-enhance-") as folder:
        root = Path(folder)
        manifest = root / "tiles.tsv"
        pairs = []
        shapes = []
        fixed = int(params.get("tile", DEFAULT_TILE))
        with manifest.open("w") as handle:
            for index, tile in enumerate(tiles):
                source = root / f"{index:05d}-in.tile"
                target = root / f"{index:05d}-out.tile"
                original = color_pipeline.as_float_rgb(tile)
                shapes.append(original.shape[:2])
                write_tile_file(source, _pad_to_tile(original, fixed))
                handle.write(f"{source}\t{target}\n")
                pairs.append(target)
        argv = [str(helper_path()), "--denoise-batch", str(manifest),
                "--strength", f"{float(params.get('strength', 1.0)):.4f}"]
        try:
            process = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
                env=dict(os.environ, **{MODEL_DIR_ENV: str(model_root())}))
        except OSError as error:
            raise EnhanceUnavailable(
                f"the Enhance helper could not be run: {error}") from error
        output_lines: list[str] = []
        error_lines: list[str] = []
        selector = selectors.DefaultSelector()
        if process.stdout is not None:
            selector.register(process.stdout, selectors.EVENT_READ)
        if process.stderr is not None:
            selector.register(process.stderr, selectors.EVENT_READ)
        started = time.monotonic()
        _publish_progress(status, 0, len(pairs))
        try:
            while process.poll() is None:
                if _cancel_requested(cancel):
                    process.kill()
                    process.wait()
                    raise RuntimeError("denoise cancelled")
                if time.monotonic() - started > HELPER_TIMEOUT:
                    process.kill()
                    process.wait()
                    raise RuntimeError(
                        f"the Enhance helper timed out after {HELPER_TIMEOUT:.0f}s")
                for key, _ in selector.select(timeout=0.2):
                    line = key.fileobj.readline()
                    if not line:
                        continue
                    if key.fileobj is process.stderr:
                        error_lines.append(line)
                        continue
                    output_lines.append(line)
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict) and "progress" in event:
                        _publish_progress(status, event["progress"],
                                          event.get("total", len(pairs)))
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.wait()
            selector.close()
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
            raise
        if process.stdout is not None:
            output_lines.extend(process.stdout.readlines())
        if process.stderr is not None:
            error_lines.extend(process.stderr.readlines())
        selector.close()
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()
        if process.returncode:
            detail = ("".join(error_lines) or "".join(output_lines)).strip()
            raise RuntimeError(detail[-300:] or "denoise failed")
        missing = [p.name for p in pairs if not p.is_file()]
        if missing:
            raise RuntimeError(
                f"the Enhance helper wrote no result for {len(missing)} tiles")
        _publish_progress(status, len(pairs), len(pairs))
        return [read_tile_file(target)[:height, :width].copy()
                for target, (height, width) in zip(pairs, shapes)]



def run_model(image, mode: str, *, strength: float = 1.0, scale: int = 2,
              runner=None, tile: int = DEFAULT_TILE,
              overlap: int = DEFAULT_OVERLAP, status=None,
              cancel=None) -> np.ndarray:
    """Tile an image, run every tile through ``runner``, and reassemble.

    ``runner`` is ``(tile_array, mode, params) -> tile_array`` with float32
    RGB in 0..1 both ways. With ``runner=None`` the default helper runner is
    used, which raises :class:`EnhanceUnavailable` when no model or helper is
    installed. There is no identity fallback: a missing model is an error,
    never a silent pass-through that looks like a successful enhancement.
    """
    mode = _mode_or_error(mode)
    request = clean_request({"mode": mode, "strength": strength,
                             "scale": scale, "tile": tile, "overlap": overlap})
    source = color_pipeline.as_float_rgb(image)
    factor = request["scale"] if mode == "upscale" else 1
    if mode == "denoise" and request["strength"] == 0:
        _publish_progress(status, 0, 0)
        return source.copy()

    batch = False
    if runner is None:
        report = capabilities()
        if not report["modes"][mode]:
            raise EnhanceUnavailable(
                report["reason"] or f"{mode} is unavailable")
        # One process for the whole image. Loading the compiled model costs
        # seconds while a tile costs a tenth of one, so per-tile processes
        # spend almost all their time loading the same model again.
        batch = mode == "denoise"
        runner = helper_runner

    params = {"mode": mode, "strength": request["strength"], "scale": factor,
              "tile": request["tile"], "overlap": request["overlap"]}
    tiles = tile_image(source, request["tile"], request["overlap"])

    if batch:
        produced_tiles = helper_batch_runner(
            [patch["image"] for patch in tiles], mode, dict(params),
            status=status, cancel=cancel)
        if len(produced_tiles) != len(tiles):
            raise RuntimeError(
                f"the denoise runner returned {len(produced_tiles)} tiles "
                f"for {len(tiles)}")
    else:
        produced_tiles = []
        _publish_progress(status, 0, len(tiles))
        for index, patch in enumerate(tiles):
            if _cancel_requested(cancel):
                raise RuntimeError("denoise cancelled")
            produced_tiles.append(runner(patch["image"], mode, dict(params)))
            _publish_progress(status, index + 1, len(tiles))

    for patch, produced in zip(tiles, produced_tiles):
        produced = np.asarray(produced, dtype=np.float32)
        expected = ((patch["y1"] - patch["y0"]) * factor,
                    (patch["x1"] - patch["x0"]) * factor)
        if produced.ndim != 3 or produced.shape[:2] != expected:
            raise ValueError(
                f"the {mode} runner returned {produced.shape}, "
                f"expected {expected} plus channels")
        patch["image"] = produced
        patch["scale"] = factor
    return merge_tiles(tiles, source.shape, request["overlap"])


def preserve_low_frequency_color(source, denoised, *, block: int = 64) -> np.ndarray:
    """Restore locally averaged scene colour without restoring sensor noise.

    A blind RGB denoiser can move broad colour slightly.  The source remains
    an unbiased colour reference once averaged over a modest block, so a
    smooth field of block-mean residuals removes that drift while leaving the
    model responsible for high-frequency noise and detail.
    """
    original = color_pipeline.as_float_rgb(source)
    result = color_pipeline.as_float_rgb(denoised).copy()
    if original.shape != result.shape:
        raise ValueError("colour preservation needs matching image dimensions")
    height, width = original.shape[:2]
    block = max(8, int(block))
    rows = max(1, math.ceil(height / block))
    columns = max(1, math.ceil(width / block))
    grid = np.empty((rows, columns, 3), dtype=np.float32)
    for row in range(rows):
        y0, y1 = row * block, min(height, (row + 1) * block)
        for column in range(columns):
            x0, x1 = column * block, min(width, (column + 1) * block)
            grid[row, column] = np.mean(
                original[y0:y1, x0:x1] - result[y0:y1, x0:x1],
                axis=(0, 1))
    order = 1 if min(rows, columns) < 4 else 3
    for channel in range(3):
        correction = image_transform.resize(
            grid[..., channel], (height, width), order=order,
            mode="reflect", anti_aliasing=False, preserve_range=True)
        result[..., channel] += correction.astype(np.float32, copy=False)
    return np.clip(result, 0.0, 1.0).astype(np.float32, copy=False)


def denoise_linear_prophoto(image, *, strength: float = 1.0, runner=None,
                            tile: int = DEFAULT_TILE,
                            overlap: int = DEFAULT_OVERLAP, status=None,
                            cancel=None) -> np.ndarray:
    """Denoise linear ProPhoto pixels through the model's gamma convention."""
    source = color_pipeline.as_float_rgb(image)
    if float(strength) <= 0:
        return source.copy()
    encoded = np.clip(color_pipeline._srgb_encode(source), 0.0, 1.0).astype(
        np.float32)
    cleaned = run_model(
        encoded, "denoise", strength=strength, runner=runner,
        tile=tile, overlap=overlap, status=status, cancel=cancel)
    decoded = np.clip(color_pipeline._srgb_decode(cleaned), 0.0, 1.0).astype(
        np.float32)
    return preserve_low_frequency_color(source, decoded)


def denoise_display_srgb(image, *, strength: float = 1.0, runner=None,
                         tile: int = DEFAULT_TILE,
                         overlap: int = DEFAULT_OVERLAP, status=None,
                         cancel=None) -> np.ndarray:
    """Denoise encoded sRGB while stabilising colour in linear light."""
    source = color_pipeline.as_float_rgb(image)
    if float(strength) <= 0:
        return source.copy()
    cleaned = run_model(
        source, "denoise", strength=strength, runner=runner,
        tile=tile, overlap=overlap, status=status, cancel=cancel)
    original_linear = color_pipeline._srgb_decode(source).astype(np.float32)
    cleaned_linear = color_pipeline._srgb_decode(cleaned).astype(np.float32)
    corrected = preserve_low_frequency_color(original_linear, cleaned_linear)
    return np.clip(color_pipeline._srgb_encode(corrected), 0.0, 1.0).astype(
        np.float32)


# --------------------------------------------------------------- outputs ----


def enhanced_folder(root: Path | str) -> Path:
    """The Enhance output folder, beside ``LightTable Merges``."""
    return Path(root) / ENHANCED_FOLDER_NAME


def enhanced_destination(root: Path | str, name: str, mode: str) -> Path:
    """A free, filesystem-safe master path inside ``LightTable Enhanced``."""
    mode = _mode_or_error(mode)
    stem = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", str(name or ""))
    stem = re.sub(r"\s+", " ", stem).strip(" ._")[:100] or mode.title()
    folder = enhanced_folder(root)
    candidate = folder / f"{stem}.tif"
    for index in range(2, 10000):
        if (not candidate.exists()
                and not candidate.with_suffix(
                    candidate.suffix + ".lighttable.json").exists()):
            return candidate
        candidate = folder / f"{stem}-{index}.tif"
    raise ValueError("could not choose a free enhanced filename")


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def manifest_for(source, destination, mode: str, request: dict | None) -> dict:
    """Sidecar contents describing exactly how a master was produced.

    ``modeled`` and ``measured`` are the honesty fields, and ``model.bundled``
    records whether this exact model came from the app or a user override.
    """
    mode = _mode_or_error(mode)
    source, destination = Path(source), Path(destination)
    cleaned = clean_request({**(request or {}), "mode": mode})
    try:
        size = source.stat().st_size
    except OSError:
        size = 0
    return {
        "kind": "enhance",
        "mode": mode,
        "source": str(source),
        "sourceName": source.name,
        "sourceSha256": _file_digest(source),
        "sourceBytes": int(size),
        "output": destination.name,
        "outputSpace": "prophoto",
        "bitDepth": 16,
        "parameters": cleaned,
        "tiling": {"tile": cleaned["tile"], "overlap": cleaned["overlap"],
                   "feather": "raised-cosine"},
        "model": model_info(mode),
        "helper": {"path": str(helper_path()),
                   "installed": bool(available_models()["helper"])},
        "runner": "helper",
        "measured": False,
        "modeled": True,
        "disclosure": DISCLOSURE,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def enhance_file(source, destination, mode: str, *, request: dict | None = None,
                 runner=None) -> dict:
    """Enhance one render into a 16-bit ProPhoto TIFF master plus a manifest.

    The source is read as display-referred RGB, the same convention as the
    Photo Merge masters, and written through
    ``color_pipeline.save_export_image`` with the ProPhoto profile embedded.
    Returns ``{"ok", "destination", "error", ...}`` and never raises for an
    expected condition, including a missing model, so a caller can surface
    the reason instead of a traceback.
    """
    destination = Path(destination)
    result = {"ok": False, "destination": str(destination), "error": None}
    staged = None
    try:
        mode = _mode_or_error(mode)
        cleaned = clean_request({**(request or {}), "mode": mode})
        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(f"cannot read {source}")
        image = color_pipeline.load_float_rgb(source)
        if mode == "denoise":
            enhanced = denoise_display_srgb(
                image, strength=cleaned["strength"], runner=runner,
                tile=cleaned["tile"], overlap=cleaned["overlap"])
        else:
            enhanced = run_model(
                image, mode, strength=cleaned["strength"],
                scale=cleaned["scale"], runner=runner,
                tile=cleaned["tile"], overlap=cleaned["overlap"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged = durable_io.temporary_path(destination, "enhance")
        width, height = color_pipeline.save_export_image(
            enhanced, staged, fmt="tif", quality=100,
            output_space="prophoto")
        durable_io.publish_file_no_replace(staged, destination)
        staged.unlink(missing_ok=True)
        staged = None
        manifest = manifest_for(source, destination, mode, cleaned)
        manifest["runner"] = "helper" if runner is None else "injected"
        manifest["width"], manifest["height"] = int(width), int(height)
        sidecar = destination.with_suffix(destination.suffix + ".lighttable.json")
        durable_io.atomic_create_json(sidecar, manifest, indent=2)
        result.update(ok=True, manifest=str(sidecar),
                      width=int(width), height=int(height))
    except (EnhanceUnavailable, ValueError, OSError, RuntimeError) as error:
        result["error"] = str(error)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    return result


# ----------------------------------------------------------- fingerprint ----


def denoise_fingerprint(params: dict | None) -> str:
    """Cache-identity token for a learned-denoise RAW decode setting.

    ``server.py`` folds this into ``color_pipeline.raw_decode_fingerprint``
    so a cached decode made with learned denoise off is never reused with it
    on. The installed model's identity is part of the token, so swapping the
    model invalidates the cache too.

    CALLER CONTRACT: whenever the meaning of this token changes, bump
    ``DENOISE_FINGERPRINT_VERSION`` here **and** the ``linear-prophoto-v5``
    token in ``raw_decode_fingerprint``, or stale decodes survive the change.
    """
    params = params or {}
    enabled = bool(params.get("learnedDenoise",
                              params.get("learned_denoise", False)))
    if not enabled:
        return f"{DENOISE_FINGERPRINT_VERSION}-off"
    strength = _clamp_float(
        params.get("learnedDenoiseStrength",
                   params.get("learned_denoise_strength", 1.0)), 0.0, 1.0, 1.0)
    return (f"{DENOISE_FINGERPRINT_VERSION}-on-{strength:.2f}-"
            f"{model_info('denoise')['identity']}")
