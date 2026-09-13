# SPDX-License-Identifier: GPL-3.0-only
"""Offline person/subject selection using the same Apache-2.0 model as hair.

Google's SelfieMulticlass model already separates a photo into background,
hair, body-skin, face-skin, clothes, and other-person-adjacent pixels. Hair
selection (film_lab_ai.hair_segmentation) uses channel 1 in isolation; this
module reuses the identical pinned asset and LiteRT runtime and unions
channels 1-4 (everything that is not background or "accessories/others") to
produce a full-image person silhouette. No second model download, licence
file, or packaging change is required: the asset, its integrity check, and
its place in every release/build script are already shared with hair
selection.

This gives the Vision-less path (any platform, including macOS builds that
have not built the Swift helper) a real learned person mask instead of the
generic saliency heuristic in semantic_masks.subject_mask, for both the
"subject" and "person" semantic mask kinds.
"""
from __future__ import annotations

import numpy as np

from . import hair_segmentation as _hair

APP = _hair.APP


class SubjectModelUnavailable(RuntimeError):
    """The optional local model or its interpreter cannot be used."""


# Channel layout of selfie_multiclass_256x256, output shape (1, 256, 256, 6):
#   0 background, 1 hair, 2 body-skin, 3 face-skin, 4 clothes, 5 other.
# "Other" (accessories, carried objects) is deliberately excluded: on its own
# it is a weak, noisy signal and is not needed to trace a person's silhouette.
_PERSON_CHANNELS = (1, 2, 3, 4)


def model_candidates():
    """Reuse hair's model search path; it is the exact same pinned file."""
    return _hair.model_candidates()


def person_probability(logits: np.ndarray) -> np.ndarray:
    values = np.asarray(logits, dtype=np.float32)
    if values.shape != (1, 256, 256, 6) or not np.isfinite(values).all():
        raise SubjectModelUnavailable("Subject model returned invalid values")
    scores = np.exp(values[0] - values[0].max(axis=-1, keepdims=True))
    total = scores.sum(axis=-1)
    probability = scores[..., _PERSON_CHANNELS].sum(axis=-1) / total
    # A photo with no person should stay exactly empty, not gain a faint
    # silhouette from the model's background noise floor.
    if not np.any(probability >= 0.5):
        return np.zeros((256, 256), dtype=np.float32)
    return np.clip((probability - 0.05) / 0.95, 0, 1)


def person_mask(image: np.ndarray, *, model_path=None) -> np.ndarray:
    """Return soft full-image person coverage, or raise if inference is unavailable.

    A valid, confidently empty result (no person detected) is returned as an
    all-zero array rather than raising, so callers can fall back to a
    generic saliency heuristic only when that heuristic is actually needed.
    """
    tensor = _hair._input_tensor(image)
    candidates = [model_path] if model_path is not None else model_candidates()
    error = None
    with _hair._lock:
        for path in candidates:
            try:
                stat = path.stat()
                identity = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
                engine, input_index, output_index = _hair._interpreter(path, identity)
                engine.set_tensor(input_index, tensor)
                engine.invoke()
                return person_probability(engine.get_tensor(output_index))
            except (OSError, ImportError, RuntimeError, ValueError) as failure:
                error = failure
    raise SubjectModelUnavailable("Local subject model is unavailable") from error
