"""Offline hair selection using Google's Apache-2.0 SelfieMulticlass model.

Assets are prepared by fetch-hair-model.py during builds. Inference never
downloads a model or sends a photograph anywhere. Keep the full image in the
input: a face crop would discard precisely the long hair we want to select.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
from pathlib import Path
import threading

import numpy as np
from PIL import Image

import platform_paths
from .hair_model_asset import MODEL_FILE, MODEL_BYTES, MODEL_SHA256, MODEL_ID

APP = Path(__file__).resolve().parent.parent
_lock = threading.Lock()


class HairModelUnavailable(RuntimeError):
    """The optional local model or its interpreter cannot be used."""


def model_candidates() -> list[Path]:
    # Resolve this file independently of denoise models. A user-installed
    # denoiser must not hide a hair model included in the application bundle.
    roots = (platform_paths.model_directory(), APP.parent / "models",
             APP / "models", APP / "scripts" / "models")
    return list(dict.fromkeys(root / MODEL_FILE for root in roots))


def model_bytes(path: Path) -> bytes:
    """Only load the bounded, pinned model that this code was validated with."""
    if path.stat().st_size != MODEL_BYTES:
        raise HairModelUnavailable("Hair model has an unexpected size")
    with path.open("rb") as handle:
        data = handle.read(MODEL_BYTES + 1)
    if len(data) != MODEL_BYTES or hashlib.sha256(data).hexdigest() != MODEL_SHA256:
        raise HairModelUnavailable("Hair model failed its integrity check")
    return data


@lru_cache(maxsize=1)
def _interpreter(path: Path, identity: tuple):
    # identity invalidates the cache if a file is replaced at the same path.
    del identity
    data = model_bytes(path)
    from ai_edge_litert.interpreter import Interpreter

    engine = Interpreter(model_content=data, num_threads=2)
    engine.allocate_tensors()
    inputs, outputs = engine.get_input_details(), engine.get_output_details()
    if (len(inputs) != 1 or len(outputs) != 1
            or tuple(inputs[0]["shape"]) != (1, 256, 256, 3)
            or tuple(outputs[0]["shape"]) != (1, 256, 256, 6)
            or inputs[0]["dtype"] != np.float32
            or outputs[0]["dtype"] != np.float32):
        raise HairModelUnavailable("Hair model tensor contract changed")
    return engine, inputs[0]["index"], outputs[0]["index"]


def _input_tensor(image: np.ndarray) -> np.ndarray:
    rgb = np.asarray(image)
    if rgb.ndim != 3 or rgb.shape[2] < 3 or min(rgb.shape[:2]) < 1:
        raise ValueError("Hair selection needs a nonempty RGB image")
    rgb = np.clip(rgb[..., :3], 0, 255).astype(np.uint8)
    resized = np.asarray(Image.fromarray(rgb).resize(
        (256, 256), Image.Resampling.BILINEAR), dtype=np.float32)
    # The embedded model metadata specifies mean=std=127.5, and SOFTMAX
    # activation. Its raw output contains logits, despite the model card's
    # description of the postprocessed probability output.
    return ((resized - 127.5) / 127.5)[None, ...]


def hair_probability(logits: np.ndarray) -> np.ndarray:
    values = np.asarray(logits, dtype=np.float32)
    if values.shape != (1, 256, 256, 6) or not np.isfinite(values).all():
        raise HairModelUnavailable("Hair model returned invalid values")
    scores = np.exp(values[0] - values[0].max(axis=-1, keepdims=True))
    probability = scores[..., 1] / scores.sum(axis=-1)
    # A low-confidence field is not evidence of hair (for example a bald
    # head or a scene without people). Keep soft edges when hair is present.
    if not np.any(probability >= 0.5):
        return np.zeros((256, 256), dtype=np.float32)
    # Suppress the model's weak hair response throughout non-hair regions
    # (often around 1%). Keep fractional coverage above that noise floor.
    return np.clip((probability - 0.05) / 0.95, 0, 1)


def hair_mask(image: np.ndarray, *, model_path: Path | None = None) -> np.ndarray:
    """Return soft full-image hair coverage, or raise if inference is unavailable.

    LiteRT interpreters are not thread-safe. Reuse one instance with two CPU
    threads, and serialize allocation/invocation across background requests.
    """
    tensor = _input_tensor(image)
    candidates = [Path(model_path)] if model_path is not None else model_candidates()
    error = None
    with _lock:
        for path in candidates:
            try:
                stat = path.stat()
                identity = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
                engine, input_index, output_index = _interpreter(path, identity)
                engine.set_tensor(input_index, tensor)
                engine.invoke()
                return hair_probability(engine.get_tensor(output_index))
            except (OSError, ImportError, RuntimeError, ValueError) as failure:
                error = failure
    raise HairModelUnavailable("Local hair model is unavailable") from error
