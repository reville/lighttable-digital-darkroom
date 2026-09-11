# SPDX-License-Identifier: GPL-3.0-only
"""Assisted culling: turn one photo's pixels and Vision cues into verdicts.

The index already runs Apple Vision over every photo. This module adds the
measurements a first pass of culling needs and converts them into six plain
answers: three reasons to keep a frame and three reasons to drop it.

Nothing here decides anything on its own. It reports "yes", "no", or
"unknown" per criterion with the number behind the call, and the interface
applies flags only when someone asks for them. "unknown" is used wherever a
criterion cannot honestly be judged -- no face in the frame, no subject
segmentation on this platform, a face too small to say anything about eyes --
so a missing answer never reads as a negative one.

Coordinate convention: every normalized point and box arriving from the
Vision helper has its origin at the top left, matching how the preview is
indexed here.
"""

from __future__ import annotations

import base64
import io

import numpy as np
from PIL import Image
from scipy import ndimage

# Bumping this re-analyzes every photo, because stored results carry the
# version they were produced by. Raise it whenever a measurement or a
# threshold changes enough that old verdicts would be misleading.
ANALYSIS_VERSION = 3

SELECT_CRITERIA = ("subjectSharpness", "eyeSharpness", "eyesOpen")
REJECT_CRITERIA = ("exposure", "misfire", "document")
CRITERIA = SELECT_CRITERIA + REJECT_CRITERIA

YES, NO, UNKNOWN = "yes", "no", "unknown"

# Thresholds live together so they can be read, argued with, and tuned in one
# place. Values are calibrated against 1024 px previews in 0..1 luminance.
THRESHOLDS = {
    # Subject sharpness: does the subject hold detail as fine as anything else
    # in the frame? A high percentile is used on both sides because skin and
    # fabric are smooth even when perfectly focused -- a mean would rank every
    # portrait below every brick wall.
    "subjectRatio": 0.80,
    "subjectFloor": 0.055,
    "subjectCoverageMin": 0.015,
    # Eye sharpness: the eyes carry the finest detail in a face, so they are
    # measured against the frame, with a floor to catch a uniformly soft face.
    "eyeRatio": 0.30,
    "eyeFloor": 0.020,
    "eyeSharpFaceMin": 0.09,
    # Eyes open: eye-contour aspect ratio, short axis over long axis.
    "eyesOpenRatio": 0.20,
    "eyesOpenFaceMin": 0.045,
    # Exposure: clipping and overall level. Thresholds are deliberately high;
    # a blown sky behind a well-exposed subject is a photograph, not a fault.
    "clippedHigh": 0.35,
    "clippedHighSubject": 0.10,
    "clippedLow": 0.45,
    "crushedMean": 0.10,
    "meanHigh": 0.88,
    "meanLow": 0.10,
    # Misfire: nothing in the frame is in focus, and there is little to see.
    "misfireFocus": 0.016,
    "misfireContrast": 0.10,
    "misfireHardFocus": 0.006,
    # Document: how much of the frame is covered in recognized text.
    "textCoverage": 0.055,
    "textLines": 8,
    "textLinesCoverage": 0.022,
    "documentFaceMax": 0.08,
}

# Vision scene labels that describe a page rather than a photograph.
DOCUMENT_TAGS = frozenset({
    "document", "text", "paper", "book_jacket", "menu", "receipt",
    "newspaper", "magazine", "screenshot", "business_card", "envelope",
    "handwriting", "letter", "notebook", "whiteboard", "blackboard",
})


def _verdict(state: str, score: float | None, detail: str) -> dict:
    return {"verdict": state,
            "score": None if score is None else round(float(score), 4),
            "detail": detail}


def preview_array(preview: bytes | np.ndarray) -> np.ndarray:
    """Return the preview as float RGB in 0..1, long edge capped for speed."""
    if isinstance(preview, np.ndarray):
        array = preview.astype(np.float32)
        if array.max(initial=0.0) > 1.5:
            array = array / 255.0
        return np.clip(array, 0.0, 1.0)
    image = Image.open(io.BytesIO(preview)).convert("RGB")
    if max(image.size) > 1400:
        scale = 1400 / max(image.size)
        image = image.resize(
            (max(1, round(image.width * scale)),
             max(1, round(image.height * scale))), Image.LANCZOS)
    return np.asarray(image, dtype=np.float32) / 255.0


def _luminance(rgb: np.ndarray) -> np.ndarray:
    return (0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1]
            + 0.0722 * rgb[..., 2]).astype(np.float32)


def _detail_energy(luma: np.ndarray) -> np.ndarray:
    """Local high-frequency energy, the focus measure everything else uses.

    A light pre-blur keeps sensor noise from reading as detail; the post-blur
    makes the result regional rather than per-pixel, so a percentile taken
    inside a mask describes an area instead of a few stray edges.
    """
    smoothed = ndimage.gaussian_filter(luma, 0.6)
    return ndimage.gaussian_filter(
        np.abs(ndimage.laplace(smoothed)), 1.5).astype(np.float32)


def _region_percentile(energy: np.ndarray, selection: np.ndarray,
                       percentile: float) -> float:
    values = energy[selection]
    if values.size < 12:
        return 0.0
    return float(np.percentile(values, percentile))


def _decode_mask(subject: dict, shape: tuple[int, int]) -> np.ndarray | None:
    """Expand the coarse subject mask the Vision helper sends back."""
    if not isinstance(subject, dict):
        return None
    edge = int(subject.get("edge") or 0)
    encoded = subject.get("mask")
    if edge <= 0 or not isinstance(encoded, str):
        return None
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None
    if len(raw) != edge * edge:
        return None
    coarse = np.frombuffer(raw, dtype=np.uint8).reshape(edge, edge)
    zoom = (shape[0] / edge, shape[1] / edge)
    grown = ndimage.zoom(coarse.astype(np.float32), zoom, order=1)
    # zoom can land a pixel short or long; pad or crop to the exact shape.
    output = np.zeros(shape, dtype=np.float32)
    rows = min(shape[0], grown.shape[0])
    columns = min(shape[1], grown.shape[1])
    output[:rows, :columns] = grown[:rows, :columns]
    return output >= 128.0


def _box_slice(box: dict, shape: tuple[int, int]) -> tuple[slice, slice] | None:
    try:
        x = float(box["x"]); y = float(box["y"])
        width = float(box["width"]); height = float(box["height"])
    except (KeyError, TypeError, ValueError):
        return None
    height_pixels, width_pixels = shape
    top = max(0, min(height_pixels - 1, int(round(y * height_pixels))))
    left = max(0, min(width_pixels - 1, int(round(x * width_pixels))))
    bottom = max(top + 1, min(height_pixels, int(round((y + height) * height_pixels))))
    right = max(left + 1, min(width_pixels, int(round((x + width) * width_pixels))))
    return slice(top, bottom), slice(left, right)


def eye_aspect_ratio(points) -> float | None:
    """Short axis over long axis of an eye contour, robust to head tilt.

    A principal-axis fit is used rather than a plain bounding box so a tilted
    head does not read as a squint.
    """
    try:
        cloud = np.asarray(points, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if cloud.ndim != 2 or cloud.shape[0] < 4 or cloud.shape[1] != 2:
        return None
    centred = cloud - cloud.mean(axis=0)
    try:
        _, singular, _ = np.linalg.svd(centred, full_matrices=False)
    except np.linalg.LinAlgError:
        return None
    if singular.size < 2 or singular[0] <= 1e-12:
        return None
    return float(singular[1] / singular[0])


def _eye_disc(points, shape: tuple[int, int]) -> np.ndarray | None:
    """A filled disc over one eye, sized from the eye's own contour."""
    try:
        cloud = np.asarray(points, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if cloud.ndim != 2 or cloud.shape[0] < 4:
        return None
    height, width = shape
    xs = cloud[:, 0] * width
    ys = cloud[:, 1] * height
    span = max(xs.max() - xs.min(), 3.0)
    radius = max(3.0, span * 0.62)
    centre_x, centre_y = xs.mean(), ys.mean()
    if not (0 <= centre_x < width and 0 <= centre_y < height):
        return None
    top = max(0, int(centre_y - radius)); bottom = min(height, int(centre_y + radius) + 1)
    left = max(0, int(centre_x - radius)); right = min(width, int(centre_x + radius) + 1)
    if bottom - top < 2 or right - left < 2:
        return None
    grid_y, grid_x = np.ogrid[top:bottom, left:right]
    disc = np.zeros(shape, dtype=bool)
    disc[top:bottom, left:right] = (
        (grid_x - centre_x) ** 2 + (grid_y - centre_y) ** 2) <= radius ** 2
    return disc


def measure(preview: bytes | np.ndarray, vision: dict | None = None) -> dict:
    """Every number the verdicts are drawn from, with no thresholds applied."""
    cull = {}
    if isinstance(vision, dict):
        raw = vision.get("cull")
        if isinstance(raw, dict):
            cull = raw
    rgb = preview_array(preview)
    luma = _luminance(rgb)
    energy = _detail_energy(luma)
    shape = luma.shape

    frame_reference = float(np.percentile(energy, 99))
    measurements = {
        "exposure": {
            "clippedHigh": float((luma >= 0.996).mean()),
            "clippedLow": float((luma <= 0.004).mean()),
            "mean": float(luma.mean()),
            "contrast": float(luma.std()),
        },
        "focus": {
            "frame": frame_reference,
            "peak": float(np.percentile(energy, 99.5)),
        },
        "subject": {"available": False},
        "faces": [],
        "faceMax": 0.0,
        "text": {
            "coverage": float(cull.get("textCoverage") or 0.0),
            "lines": int(cull.get("textLines") or 0),
        },
    }

    mask = _decode_mask(cull.get("subject"), shape)
    if mask is not None:
        coverage = float(mask.mean())
        subject_focus = _region_percentile(energy, mask, 99)
        subject_luma = luma[mask]
        measurements["subject"] = {
            "available": True,
            "coverage": coverage,
            "focus": subject_focus,
            "ratio": (subject_focus / frame_reference
                      if frame_reference > 1e-9 else 0.0),
            "clippedHigh": (float((subject_luma >= 0.996).mean())
                            if subject_luma.size else 0.0),
            "clippedLow": (float((subject_luma <= 0.004).mean())
                           if subject_luma.size else 0.0),
        }
    elif isinstance(cull.get("subject"), dict):
        measurements["subject"] = {"available": False,
                                   "reason": "no subject found"}

    for face in cull.get("faces") or []:
        if not isinstance(face, dict):
            continue
        box = face.get("box") if isinstance(face.get("box"), dict) else {}
        entry = {
            "width": float(box.get("width") or 0.0),
            "quality": (float(face["quality"])
                        if isinstance(face.get("quality"), (int, float))
                        and float(face["quality"]) >= 0 else None),
            "eyeOpen": None,
            "eyeFocus": None,
            "eyeRatio": None,
        }
        measurements["faceMax"] = max(measurements["faceMax"], entry["width"])
        ratios = [value for value in
                  (eye_aspect_ratio(face.get("leftEye")),
                   eye_aspect_ratio(face.get("rightEye")))
                  if value is not None]
        if ratios:
            # The tighter eye decides; one closed eye is still a blink.
            entry["eyeOpen"] = float(min(ratios))
        discs = [disc for disc in
                 (_eye_disc(face.get("leftEye"), shape),
                  _eye_disc(face.get("rightEye"), shape)) if disc is not None]
        if discs:
            combined = discs[0]
            for disc in discs[1:]:
                combined = combined | disc
            eye_focus = _region_percentile(energy, combined, 90)
            entry["eyeFocus"] = eye_focus
            # Measured against the frame, not the face: a soft face would
            # otherwise score well simply by being uniformly soft.
            entry["eyeRatio"] = (eye_focus / frame_reference
                                 if frame_reference > 1e-9 else 0.0)
        measurements["faces"].append(entry)
    return measurements


def _subject_verdict(measurements: dict) -> dict:
    subject = measurements["subject"]
    if not subject.get("available"):
        return _verdict(UNKNOWN, None,
                        "No subject segmentation for this photo")
    if subject["coverage"] < THRESHOLDS["subjectCoverageMin"]:
        return _verdict(UNKNOWN, subject["ratio"],
                        "The subject found is too small to judge")
    ratio = subject["ratio"]
    # Two tests, because either one alone is fooled: the ratio cannot see a
    # photo that is blurred end to end, and the floor cannot tell a soft
    # scene from a missed focus.
    if ratio < THRESHOLDS["subjectRatio"]:
        return _verdict(NO, ratio, "Something behind the subject is sharper")
    if subject["focus"] < THRESHOLDS["subjectFloor"]:
        return _verdict(NO, ratio, "The subject holds no fine detail")
    return _verdict(YES, ratio, "The subject holds the sharpest detail")


def _eye_sharpness_verdict(measurements: dict) -> dict:
    faces = [face for face in measurements["faces"]
             if face["eyeRatio"] is not None]
    if not measurements["faces"]:
        return _verdict(UNKNOWN, None, "No face in this photo")
    big = [face for face in faces
           if face["width"] >= THRESHOLDS["eyeSharpFaceMin"]]
    if not big:
        return _verdict(UNKNOWN, None,
                        "The faces are too small to judge eye sharpness")
    best = max(big, key=lambda face: face["eyeRatio"])
    if best["eyeFocus"] < THRESHOLDS["eyeFloor"]:
        return _verdict(NO, best["eyeRatio"], "The eyes hold no fine detail")
    if best["eyeRatio"] < THRESHOLDS["eyeRatio"]:
        return _verdict(NO, best["eyeRatio"], "Focus did not land on the eyes")
    return _verdict(YES, best["eyeRatio"], "The eyes are sharp")


def _eyes_open_verdict(measurements: dict) -> dict:
    faces = measurements["faces"]
    if not faces:
        return _verdict(UNKNOWN, None, "No face in this photo")
    usable = [face for face in faces
              if face["eyeOpen"] is not None
              and face["width"] >= THRESHOLDS["eyesOpenFaceMin"]]
    if not usable:
        return _verdict(UNKNOWN, None,
                        "The faces are too small to judge open eyes")
    # Every face has to pass: one blink in a group shot still spoils it.
    tightest = min(usable, key=lambda face: face["eyeOpen"])
    count = len(usable)
    if tightest["eyeOpen"] >= THRESHOLDS["eyesOpenRatio"]:
        return _verdict(YES, tightest["eyeOpen"],
                        f"Eyes open on {'both' if count == 2 else 'all'} "
                        f"{count} faces" if count > 1 else "Eyes are open")
    return _verdict(NO, tightest["eyeOpen"],
                    "A subject's eyes are closed or nearly closed"
                    if count > 1 else "The eyes look closed")


def _exposure_verdict(measurements: dict) -> dict:
    exposure = measurements["exposure"]
    subject = measurements["subject"]
    high = exposure["clippedHigh"]
    low = exposure["clippedLow"]
    mean = exposure["mean"]

    # Two ways to be over-exposed. Clipping that reaches the subject counts
    # at once, because that detail is gone for good. Clipping anywhere else
    # only counts once it has taken over the frame: a blown sky behind a
    # correctly exposed subject is a choice, and the commonest false alarm
    # a rule like this can raise.
    subject_high = subject.get("clippedHigh", 0.0) if subject.get("available") else 0.0
    if subject_high >= THRESHOLDS["clippedHighSubject"]:
        return _verdict(YES, subject_high, "The subject itself is blown out")
    if high >= THRESHOLDS["clippedHigh"]:
        return _verdict(YES, high, f"{high * 100:.0f}% of the frame is blown out")
    if mean >= THRESHOLDS["meanHigh"]:
        return _verdict(YES, mean, "The whole frame is very bright")
    if mean <= THRESHOLDS["meanLow"]:
        return _verdict(YES, mean, "The whole frame is very dark")
    if low >= THRESHOLDS["clippedLow"] and mean <= THRESHOLDS["crushedMean"]:
        return _verdict(YES, low, f"{low * 100:.0f}% of the frame is crushed black")
    return _verdict(NO, max(high, low), "Exposure is within range")


def _misfire_verdict(measurements: dict) -> dict:
    focus = measurements["focus"]["frame"]
    contrast = measurements["exposure"]["contrast"]
    if focus <= THRESHOLDS["misfireHardFocus"]:
        return _verdict(YES, focus, "Nothing in the frame resolves at all")
    # Legitimately soft photographs -- a dark stage, a macro that is mostly
    # bokeh -- sit close above this line, so it is set low on purpose and
    # catches only frames with no plane of focus anywhere.
    if focus < THRESHOLDS["misfireFocus"]:
        return _verdict(YES, focus, "Nothing in the frame is in focus")
    if (focus < THRESHOLDS["misfireFocus"] * 1.8
            and contrast < THRESHOLDS["misfireContrast"]):
        return _verdict(YES, focus, "Soft and flat throughout, likely a misfire")
    return _verdict(NO, focus, "The frame has real detail in it")


def _document_verdict(measurements: dict, tags: list[str]) -> dict:
    text = measurements["text"]
    coverage = text["coverage"]
    lines = text["lines"]
    labels = {str(tag).strip().lower().replace(" ", "_") for tag in tags or []}
    documentish = bool(labels & DOCUMENT_TAGS)
    # Someone's face fills the frame: this is a portrait with print in it,
    # not a page. Street signs and market stalls trip every text rule.
    if measurements.get("faceMax", 0.0) >= THRESHOLDS["documentFaceMax"]:
        return _verdict(NO, coverage, "A face this large means a photograph")
    if coverage >= THRESHOLDS["textCoverage"]:
        return _verdict(YES, coverage, f"Text covers {coverage * 100:.0f}% of the frame")
    if lines >= THRESHOLDS["textLines"] and coverage >= THRESHOLDS["textLinesCoverage"]:
        return _verdict(YES, coverage, f"{lines} lines of text across the frame")
    if documentish and lines >= 5 and coverage >= 0.015:
        return _verdict(YES, coverage, "Reads as a page rather than a photograph")
    return _verdict(NO, coverage, "Not a document")


def score(measurements: dict, tags: list[str] | None = None) -> dict:
    """Apply thresholds. Separated from `measure` so tuning is cheap."""
    return {
        "subjectSharpness": _subject_verdict(measurements),
        "eyeSharpness": _eye_sharpness_verdict(measurements),
        "eyesOpen": _eyes_open_verdict(measurements),
        "exposure": _exposure_verdict(measurements),
        "misfire": _misfire_verdict(measurements),
        "document": _document_verdict(measurements, tags or []),
    }


def similarity_signature(rgb: np.ndarray) -> dict:
    """Small local scene fingerprint; never an identity or a keep decision.

    A difference hash captures edge layout; coarse RGB and aspect ratio keep
    unrelated scenes with similar edges apart. Spatial contrast lets clients
    decline flat/empty frames whose hashes carry too little information.
    """
    pixels = np.rint(np.clip(rgb, 0.0, 1.0) * 255).astype(np.uint8)
    image = Image.fromarray(pixels)
    gray = np.asarray(image.convert("L").resize((9, 8), Image.Resampling.BOX))
    bits = (gray[:, 1:] > gray[:, :-1]).reshape(-1)
    packed = np.packbits(bits).tobytes().hex()
    layout = np.asarray(image.resize((4, 4), Image.Resampling.BOX))
    contrast = float(_luminance(layout.astype(np.float32) / 255.0).std())
    return {"version": 1, "hash": packed, "layout": layout.tobytes().hex(),
            "contrast": round(contrast, 4),
            "aspect": round(image.width / image.height, 4)}


def analyze(preview: bytes | np.ndarray, vision: dict | None = None) -> dict:
    """The record stored per photo: verdicts plus the numbers behind them."""
    rgb = preview_array(preview)
    measurements = measure(rgb, vision)
    tags = (vision or {}).get("tags") if isinstance(vision, dict) else []
    return {
        "version": ANALYSIS_VERSION,
        "criteria": score(measurements, tags if isinstance(tags, list) else []),
        "metrics": measurements,
        "similarity": similarity_signature(rgb),
    }


def matches(record: dict | None, enabled: list[str] | tuple[str, ...]) -> bool:
    """True when any enabled criterion answered yes for this photo."""
    if not isinstance(record, dict):
        return False
    criteria = record.get("criteria")
    if not isinstance(criteria, dict):
        return False
    for name in enabled:
        entry = criteria.get(name)
        if isinstance(entry, dict) and entry.get("verdict") == YES:
            return True
    return False
