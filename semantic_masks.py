"""Private, on-device semantic selections for local adjustment masks.

Apple Vision supplies foreground instances and reads embedded capture depth
when its helper is available. The portable implementations below keep Subject,
Sky, point-seeded Object, and relative Depth usable where that optional helper
is absent. Results are compact 8-bit fields in image coordinates.
"""
from __future__ import annotations

import base64
import io
import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import (binary_propagation, gaussian_filter,
                           label as connected_components, sobel)
from skimage import color, segmentation


MAX_MASK_EDGE = 1024
MAX_ANALYSIS_EDGE = 512
MAX_PART_EDGE = 1024
MAX_DEPTH_EDGE = 512
PARTS_CACHE_VERSION = 2
PERSON_PARTS = ("person", "face-skin", "eyes", "eyebrows", "lips",
                "teeth", "hair")
PEOPLE_MASK_UNAVAILABLE = (
    "People masks need the Vision helper (macOS 14 or later)")


def _working_image(image: np.ndarray, max_edge: int = MAX_MASK_EDGE) -> np.ndarray:
    rgb = np.clip(np.asarray(image)[..., :3], 0, 255).astype(np.uint8)
    height, width = rgb.shape[:2]
    scale = min(1.0, max_edge / max(height, width))
    if scale < 1:
        rgb = np.asarray(Image.fromarray(rgb, "RGB").resize(
            (max(1, round(width * scale)), max(1, round(height * scale))),
            Image.Resampling.LANCZOS))
    return rgb.astype(np.float32) / 255.0


def _refine_edges(mask: np.ndarray, rgb: np.ndarray, *, strength: float = 1.0
                  ) -> np.ndarray:
    """Feather coverage without averaging across contrasting image edges.

    A small joint bilateral kernel uses all three colour channels, including
    boundaries with similar luminance. Unlike thresholding or hole filling,
    this keeps soft model alpha and openings between branches and hair.
    """
    coverage = np.clip(np.asarray(mask, dtype=np.float32), 0, 1)
    height, width = rgb.shape[:2]
    if coverage.shape != (height, width):
        coverage = np.asarray(Image.fromarray(coverage).resize(
            (width, height), Image.Resampling.BILINEAR))
    radius = 2
    guide = np.pad(rgb, ((radius, radius), (radius, radius), (0, 0)), mode="edge")
    padded = np.pad(coverage, radius, mode="edge")
    total = np.zeros_like(coverage)
    weights = np.zeros_like(coverage)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            y, x = radius + dy, radius + dx
            colour_delta = guide[y:y + height, x:x + width] - rgb
            colour_distance = np.sum(colour_delta * colour_delta, axis=2)
            weight = np.exp(-colour_distance / (2 * 0.08 ** 2)
                            - (dx * dx + dy * dy) / (2 * 1.1 ** 2))
            total += weight * padded[y:y + height, x:x + width]
            weights += weight
    return np.clip(coverage * (1 - strength) + total / weights * strength, 0, 1)


def _finish(mask: np.ndarray, rgb: np.ndarray, *, strength: float = 1.0
            ) -> np.ndarray:
    refined = _refine_edges(mask, rgb, strength=strength)
    return np.clip(refined * 255.0 + 0.5, 0, 255).astype(np.uint8)


def _largest_relevant(mask: np.ndarray, point: tuple[int, int] | None = None) -> np.ndarray:
    labels, count = connected_components(mask)
    if not count:
        return mask
    if point is not None:
        target = labels[point[1], point[0]]
        if target:
            return labels == target
    sizes = np.bincount(labels.ravel())
    sizes[0] = 0
    return labels == int(np.argmax(sizes))


def subject_mask(image: np.ndarray) -> np.ndarray:
    """Select the most salient central foreground from local image evidence."""
    rgb = _working_image(image, MAX_ANALYSIS_EDGE)
    height, width = rgb.shape[:2]
    lab = color.rgb2lab(rgb)
    labels = segmentation.slic(
        rgb, n_segments=150, compactness=12, sigma=0.8,
        start_label=0, channel_axis=-1,
    )
    border = np.unique(np.concatenate((
        labels[0], labels[-1], labels[:, 0], labels[:, -1],
    )))
    border_set = set(int(value) for value in border)
    background = np.median(np.concatenate((
        lab[0], lab[-1], lab[:, 0], lab[:, -1],
    ), axis=0), axis=0)
    saturation_map = color.rgb2hsv(rgb)[..., 1]
    yy, xx = np.mgrid[0:height, 0:width]
    selected = np.zeros((height, width), dtype=bool)
    scores = []
    for ident in np.unique(labels):
        region = labels == ident
        area = int(region.sum())
        if not area:
            continue
        cx = float(xx[region].mean()) / max(width - 1, 1)
        cy = float(yy[region].mean()) / max(height - 1, 1)
        centre = np.exp(-(((cx - 0.5) / 0.48) ** 2 +
                          ((cy - 0.53) / 0.52) ** 2))
        delta = float(np.linalg.norm(lab[region].mean(axis=0) - background))
        saturation = float(saturation_map[region].mean())
        touches = 1.0 if int(ident) in border_set else 0.0
        score = centre * 0.9 + min(delta / 45.0, 1.5) * 0.65 + \
            saturation * 0.25 - touches * 0.5
        scores.append((score, int(ident), area))
    if not scores:
        return np.zeros((height, width), dtype=np.uint8)
    threshold = max(0.72, float(np.percentile([item[0] for item in scores], 67)))
    for score, ident, area in scores:
        if score >= threshold and area >= max(5, rgb.shape[0] * rgb.shape[1] // 1500):
            selected |= labels == ident
    centre_label = int(labels[height // 2, width // 2])
    selected |= labels == centre_label
    # A semantic subject should be coherent rather than scattered saliency.
    return _finish(_largest_relevant(selected, (width // 2, height // 2)),
                   _working_image(image))


def sky_mask(image: np.ndarray) -> np.ndarray:
    """Select a smooth, top-connected sky region using colour and texture."""
    rgb = _working_image(image)
    height, width = rgb.shape[:2]
    lab = color.rgb2lab(rgb)
    top_depth = max(1, height // 12)
    top_colour = np.median(lab[:top_depth], axis=(0, 1))
    colour_distance = np.linalg.norm(lab - top_colour, axis=2)
    hsv = color.rgb2hsv(rgb)
    blue_like = ((rgb[..., 2] >= rgb[..., 0] * 0.92) &
                 (rgb[..., 2] >= rgb[..., 1] * 0.82) &
                 (hsv[..., 2] > 0.25))
    light_neutral = (hsv[..., 1] < 0.22) & (hsv[..., 2] > 0.62)
    luma = color.rgb2gray(rgb)
    gy = sobel(luma, axis=0) / 8.0
    gx = sobel(luma, axis=1) / 8.0
    smooth = np.hypot(gx, gy) < 0.13
    # Extra blue/cloud colours are evidence only when they also occur near the
    # top. Otherwise a connected blue object lower in a warm scene can leak
    # into the selection merely because it happens to resemble a clear sky.
    sky_colours = colour_distance < 28.0
    if float(blue_like[:top_depth].mean()) >= 0.02:
        sky_colours |= blue_like
    if float(light_neutral[:top_depth].mean()) >= 0.02:
        sky_colours |= light_neutral
    candidate = smooth & sky_colours
    seeds = np.zeros_like(candidate)
    seeds[0] = candidate[0]
    connected = binary_propagation(seeds, mask=candidate)
    return _finish(connected, rgb)


def object_mask(image: np.ndarray, point: tuple[float, float]) -> np.ndarray:
    """Grow a point selection toward image boundaries using local colour cues.

    Watershed boundaries admit gradual shading inside the clicked region while
    distant colours supply background seeds. This is still an image heuristic,
    not an object-recognition model.
    """
    rgb = _working_image(image)
    height, width = rgb.shape[:2]
    x = int(np.clip(round(point[0] * (width - 1)), 0, width - 1))
    y = int(np.clip(round(point[1] * (height - 1)), 0, height - 1))
    lab = color.rgb2lab(rgb)
    radius = max(2, min(height, width) // 80)
    local = lab[max(0, y - radius):y + radius + 1,
                max(0, x - radius):x + radius + 1]
    # A patch median alone can replace a clicked thin detail with its backdrop.
    # Estimate the target only from neighbours resembling the actual seed.
    similar = local[np.linalg.norm(local - lab[y, x], axis=2) < 12.0]
    target = np.median(similar, axis=0)
    channel_weights = np.array((0.45, 1.0, 1.0), dtype=np.float32)
    distance = np.linalg.norm((lab - target) * channel_weights, axis=2)
    local_distance = np.linalg.norm((similar - target) * channel_weights, axis=1)
    tolerance = float(np.clip(np.percentile(local_distance, 90) * 2.5, 12, 24))
    candidate = distance <= tolerance
    seed = np.zeros_like(candidate)
    seed[y, x] = True
    connected = binary_propagation(seed, mask=candidate)
    background = distance >= max(30.0, tolerance * 2.2)
    if np.any(background):
        markers = np.zeros((height, width), dtype=np.int32)
        markers[background] = 2
        markers[connected] = 1
        gradient = np.sqrt(np.sum(
            (sobel(lab, axis=0) / 8.0) ** 2
            + (sobel(lab, axis=1) / 8.0) ** 2, axis=2))
        regions = segmentation.watershed(gradient, markers)
        connected = regions == 1
    return _finish(_largest_relevant(connected, (x, y)), rgb)


def estimated_depth_map(image: np.ndarray) -> np.ndarray:
    """Estimate relative depth from local subject, sky, detail, and layout cues.

    The result is continuous rather than a binary selection: black is farther
    and white is nearer.  Keeping that continuous field lets the mask range be
    adjusted non-destructively after analysis.
    """
    rgb = _working_image(image, MAX_DEPTH_EDGE)
    height, width = rgb.shape[:2]
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    vertical = np.power(yy / max(height - 1, 1), 1.2)
    centre = np.exp(-(((xx / max(width - 1, 1) - 0.5) / 0.48) ** 2
                      + ((yy / max(height - 1, 1) - 0.56) / 0.55) ** 2))
    luma = color.rgb2gray(rgb)
    detail = np.abs(luma - gaussian_filter(luma, 2.2))
    detail_scale = max(float(np.percentile(detail, 96)), 1e-4)
    detail = np.clip(detail / detail_scale, 0.0, 1.0)
    source = np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8)
    subject = subject_mask(source).astype(np.float32) / 255.0
    sky = sky_mask(source).astype(np.float32) / 255.0
    if subject.shape != (height, width):
        subject = np.asarray(Image.fromarray(subject).resize(
            (width, height), Image.Resampling.BILINEAR))
    if sky.shape != (height, width):
        sky = np.asarray(Image.fromarray(sky).resize(
            (width, height), Image.Resampling.BILINEAR))
    estimate = (0.42 * vertical + 0.37 * subject + 0.12 * detail
                + 0.09 * centre - 0.42 * sky)
    estimate = gaussian_filter(estimate.astype(np.float32), 1.15)
    low, high = np.percentile(estimate, (2, 98))
    if high - low < 1e-5:
        return np.clip(vertical * 255.0 + 0.5, 0, 255).astype(np.uint8)
    estimate = np.clip((estimate - low) / (high - low), 0.0, 1.0)
    return np.clip(estimate * 255.0 + 0.5, 0, 255).astype(np.uint8)


def _embedded_depth(source_path: Path, helper: Path,
                    provider=None) -> np.ndarray | None:
    if provider is None and not helper.is_file():
        return None
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "depth.png"
        try:
            if provider is not None:
                if not provider.depth_map(source_path, output):
                    return None
            else:
                completed = subprocess.run(
                    [str(helper), "--depth-map", str(source_path), str(output)],
                    capture_output=True, text=True, timeout=45, check=False)
                if completed.returncode or not output.is_file():
                    return None
            values = np.asarray(Image.open(output).convert("L"))
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return None
        if max(values.shape) > MAX_DEPTH_EDGE:
            scale = MAX_DEPTH_EDGE / max(values.shape)
            values = np.asarray(Image.fromarray(values).resize(
                (max(1, round(values.shape[1] * scale)),
                 max(1, round(values.shape[0] * scale))),
                Image.Resampling.LANCZOS))
        return values.astype(np.uint8, copy=False)


def _vision_subject(source_path: Path, helper: Path,
                    provider=None, *, image: np.ndarray) -> np.ndarray | None:
    if provider is None and not helper.is_file():
        return None
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "foreground.png"
        if provider is not None:
            try:
                if not provider.foreground_mask(source_path, output):
                    return None
            except Exception:
                return None
        else:
            completed = subprocess.run(
                [str(helper), "--foreground-mask", str(source_path), str(output)],
                capture_output=True, text=True, timeout=90, check=False,
            )
            if completed.returncode or not output.is_file():
                return None
        try:
            with Image.open(output) as result:
                mask = np.asarray(result.convert("L"), dtype=np.float32) / 255.0
        except (OSError, ValueError):
            return None
        # The caller's decoded, oriented image also works for RAW files that
        # Pillow cannot read directly. Keep Vision's fractional alpha values.
        return _finish(mask, _working_image(image), strength=0.35)


def _person_parts(source_path: Path, helper: Path, provider=None,
                  cache: Path | None = None) -> tuple[dict[str, np.ndarray], dict]:
    available = (bool(getattr(provider, "available", True))
                 if provider is not None else helper.is_file())
    if not available:
        raise ValueError(PEOPLE_MASK_UNAVAILABLE)

    def load(root: Path) -> tuple[dict[str, np.ndarray], dict] | None:
        try:
            summary = json.loads((root / "parts.json").read_text())
            if not isinstance(summary, dict) or \
                    summary.get("maskVersion") != PARTS_CACHE_VERSION:
                return None
            masks = {name: np.asarray(Image.open(root / f"{name}.png").convert("L"))
                     for name in PERSON_PARTS if (root / f"{name}.png").is_file()}
        except (OSError, ValueError):
            return None
        return (masks, summary) if masks else None

    root_context = None
    if cache is None:
        root_context = tempfile.TemporaryDirectory(prefix="lighttable-parts-")
        root = Path(root_context.name)
    else:
        root = Path(cache)
        root.mkdir(parents=True, exist_ok=True)
        existing = load(root)
        if existing is not None:
            return existing
        # Only remove files owned by this generated cache. If the new helper
        # omits a part, its old bitmap must not survive the version change.
        for name in (*PERSON_PARTS, "parts"):
            (root / (f"{name}.json" if name == "parts" else f"{name}.png")).unlink(
                missing_ok=True)
    try:
        if provider is not None:
            summary = provider.person_parts(source_path, root)
        else:
            completed = subprocess.run(
                [str(helper), "--person-parts", str(source_path), str(root)],
                capture_output=True, text=True, timeout=120, check=False)
            if completed.returncode:
                raise ValueError(PEOPLE_MASK_UNAVAILABLE)
            try:
                summary = json.loads(completed.stdout.splitlines()[-1])
            except (ValueError, IndexError):
                summary = {}
        summary = summary if isinstance(summary, dict) else {}
        summary["maskVersion"] = PARTS_CACHE_VERSION
        (root / "parts.json").write_text(json.dumps(summary))
        loaded = load(root)
        if loaded is None:
            raise ValueError(PEOPLE_MASK_UNAVAILABLE)
        return loaded
    finally:
        if root_context is not None:
            # Arrays are materialised by Image.open above before cleanup.
            root_context.cleanup()


def generate(image: np.ndarray, kind: str, point: tuple[float, float] | None = None,
             *, source_path: Path | None = None,
             vision_helper: Path | None = None,
             vision_provider=None,
             parts_cache: Path | None = None) -> tuple[np.ndarray, str]:
    kind = str(kind).lower()
    if kind in PERSON_PARTS:
        if not source_path or not vision_helper:
            if kind == "person":
                return subject_mask(image), "local-segmentation"
            raise ValueError(PEOPLE_MASK_UNAVAILABLE)
        try:
            masks, summary = _person_parts(
                source_path, vision_helper, vision_provider, parts_cache)
        except ValueError:
            if kind == "person":
                return subject_mask(image), "local-segmentation"
            raise
        mask = masks.get(kind)
        if mask is None or not np.any(mask):
            if kind == "person":
                return subject_mask(image), "local-segmentation"
            raise ValueError(f"No {kind.replace('-', ' ')} found")
        provider = "vision-estimated" if kind in ("face-skin", "hair") \
            else str(summary.get("provider", "vision"))
        return _finish(mask.astype(np.float32) / 255.0,
                       _working_image(image, MAX_PART_EDGE), strength=0.25), provider
    if kind == "subject" and source_path and vision_helper:
        vision = _vision_subject(source_path, vision_helper, vision_provider,
                                 image=image)
        if vision is not None and np.any(vision):
            return vision, "vision"
    if kind == "subject":
        return subject_mask(image), "local-segmentation"
    if kind == "sky":
        return sky_mask(image), "local-segmentation"
    if kind == "object":
        if point is None:
            raise ValueError("object selection requires a point")
        return object_mask(image, point), "local-segmentation"
    if kind == "depth":
        if source_path and vision_helper:
            embedded = _embedded_depth(
                source_path, vision_helper, vision_provider)
            if embedded is not None and np.any(embedded):
                return embedded, "embedded-depth"
        return estimated_depth_map(image), "local-depth-estimate"
    raise ValueError("unknown semantic mask type")


def encode_bitmap(mask: np.ndarray, *, png: bool = False,
                  max_edge: int | None = None) -> dict:
    values = np.ascontiguousarray(mask, dtype=np.uint8)
    if max_edge and max(values.shape) > max_edge:
        scale = max_edge / max(values.shape)
        values = np.asarray(Image.fromarray(values, "L").resize(
            (max(1, round(values.shape[1] * scale)),
             max(1, round(values.shape[0] * scale))),
            Image.Resampling.LANCZOS))
    if png:
        stream = io.BytesIO()
        Image.fromarray(values, "L").save(stream, "PNG", optimize=True)
        return {"width": int(values.shape[1]), "height": int(values.shape[0]),
                "encoding": "png",
                "data": base64.b64encode(stream.getvalue()).decode("ascii")}
    return {
        "width": int(values.shape[1]),
        "height": int(values.shape[0]),
        "data": base64.b64encode(values.tobytes()).decode("ascii"),
    }
