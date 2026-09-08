"""Reusable export recipes, destinations, and collision-safe filenames."""
from __future__ import annotations

from server_localization import T

import re
import hashlib
import os
from datetime import datetime
from pathlib import Path


BUILTIN_RECIPES = [
    {
        "id": "builtin-web", "name": "Web JPEG", "builtin": True,
        "format": "jpeg", "quality": 88, "longEdge": 2560,
        "outputSpace": "srgb", "destination": "film-exports",
        "filenameTemplate": "{filename}_{stock}", "collision": "rename",
    },
    {
        "id": "builtin-print", "name": "Print TIFF", "builtin": True,
        "format": "tif", "quality": 100, "longEdge": None,
        "outputSpace": "display_p3", "destination": "film-exports",
        "filenameTemplate": "{filename}_{stock}_print", "collision": "rename",
    },
    {
        "id": "builtin-archive", "name": "Archive TIFF", "builtin": True,
        "format": "tif", "quality": 100, "longEdge": None,
        "outputSpace": "prophoto", "destination": "film-exports",
        "filenameTemplate": "{filename}_{stock}_master", "collision": "rename",
    },
    {
        # Client work wants copyright inside the file and nothing beside it.
        "id": "builtin-client", "name": "Client delivery", "builtin": True,
        "format": "jpeg", "quality": 92, "longEdge": 3000,
        "outputSpace": "srgb", "destination": "film-exports",
        "filenameTemplate": "{filename}", "collision": "rename",
        "metadata": "all-except-location", "sidecar": False,
        "watermark": {"enabled": False},
    },
]

TOKENS = {"filename", "stock", "rating", "date", "sequence"}

METADATA_POLICIES = ("none", "copyright", "all", "all-except-location")
DESTINATION_MODES = ("fixed", "original-folder-relative", "preserve-source-hierarchy")
CAPTURE_TIME_POLICIES = ("require-offset", "local")

WATERMARK_KINDS = ("text", "image")

WATERMARK_ANCHORS = (
    "top-left", "top-right", "bottom-left", "bottom-right", "center",
)

WATERMARK_DEFAULTS = {
    "enabled": False, "kind": "text", "text": "", "imagePath": "",
    "anchor": "bottom-right", "inset": 0.03, "scale": 0.12, "opacity": 0.7,
}

APP_ROOT = Path(__file__).resolve().parent


def _text(value, default: str, limit: int) -> str:
    result = " ".join(str(value or default).split()).strip()
    return (result or default)[:limit]


def _flag(value, default: bool) -> bool:
    """Accept JSON booleans and the string forms a form post can send."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def _clamped(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN never survives a round-trip through the UI.
        return default
    return max(low, min(high, number))


def clean_watermark(raw) -> dict:
    """Validate one watermark spec, filling every key with a safe default."""
    raw = raw if isinstance(raw, dict) else {}
    kind = str(raw.get("kind", WATERMARK_DEFAULTS["kind"])).lower()
    if kind not in WATERMARK_KINDS:
        kind = WATERMARK_DEFAULTS["kind"]
    anchor = str(raw.get("anchor", WATERMARK_DEFAULTS["anchor"])).lower()
    if anchor not in WATERMARK_ANCHORS:
        anchor = WATERMARK_DEFAULTS["anchor"]
    return {
        "enabled": _flag(raw.get("enabled"), WATERMARK_DEFAULTS["enabled"]),
        "kind": kind,
        "text": " ".join(str(raw.get("text") or "").split())[:120],
        "imagePath": str(raw.get("imagePath") or "")[:500],
        "anchor": anchor,
        "inset": _clamped(raw.get("inset"), WATERMARK_DEFAULTS["inset"],
                          0.0, 0.25),
        "scale": _clamped(raw.get("scale"), WATERMARK_DEFAULTS["scale"],
                          0.02, 0.5),
        "opacity": _clamped(raw.get("opacity"), WATERMARK_DEFAULTS["opacity"],
                            0.0, 1.0),
    }


def clean_recipe(raw: dict | None, *, builtin: bool = False) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    format_name = str(raw.get("format", "jpeg")).lower()
    if format_name not in ("jpeg", "png", "tif", "heif"):
        format_name = "jpeg"
    output_space = str(raw.get("outputSpace", "srgb")).lower()
    if output_space not in ("srgb", "display_p3", "prophoto"):
        output_space = "srgb"
    try:
        quality = max(1, min(100, int(raw.get("quality", 92))))
    except (TypeError, ValueError):
        quality = 92
    try:
        edge = int(raw.get("longEdge") or 0)
        long_edge = max(320, min(40000, edge)) if edge else None
    except (TypeError, ValueError):
        long_edge = None
    collision = str(raw.get("collision", "rename"))
    if collision not in ("rename", "skip", "overwrite"):
        collision = "rename"
    template = str(raw.get("filenameTemplate", "{filename}_{stock}"))[:160]
    if not template.strip():
        template = "{filename}_{stock}"
    metadata_value = raw.get("metadata", "all-except-location")
    metadata = ("all" if metadata_value else "none") if isinstance(metadata_value, bool) else str(metadata_value).lower()
    if metadata not in METADATA_POLICIES:
        metadata = "all-except-location"
    destination_mode = str(raw.get("destinationMode", "fixed"))
    if destination_mode not in DESTINATION_MODES:
        raise ValueError(T("unknown export destination mode"))
    timestamp_policy = str(raw.get("captureTimePolicy", "require-offset"))
    if timestamp_policy not in CAPTURE_TIME_POLICIES:
        raise ValueError(T("unknown capture-time timezone policy"))
    return {
        "id": _text(raw.get("id"), "recipe", 100),
        "name": _text(raw.get("name"), "Export recipe", 80),
        "builtin": bool(builtin or raw.get("builtin", False)),
        "format": format_name, "quality": quality, "longEdge": long_edge,
        "outputSpace": output_space,
        "destination": str(raw.get("destination", "film-exports"))[:500],
        "destinationMode": destination_mode,
        "preserveCaptureTime": _flag(raw.get("preserveCaptureTime"), False),
        "captureTimePolicy": timestamp_policy,
        "filenameTemplate": template, "collision": collision,
        "metadata": metadata,
        "sidecar": _flag(raw.get("sidecar"), True),
        "watermark": clean_watermark(raw.get("watermark")),
    }


def clean_custom_recipes(values) -> list[dict]:
    result = []
    seen = set()
    for raw in values[:20] if isinstance(values, list) else []:
        recipe = clean_recipe(raw)
        if recipe["id"] in seen or recipe["id"].startswith("builtin-"):
            continue
        seen.add(recipe["id"])
        recipe["builtin"] = False
        result.append(recipe)
    return result


def all_recipes(custom) -> list[dict]:
    return [clean_recipe(item, builtin=True) for item in BUILTIN_RECIPES] + \
        clean_custom_recipes(custom)


def resolve_destination(library_root: Path, value: str | None) -> Path:
    raw = str(value or "film-exports").strip()
    destination = Path(raw).expanduser()
    if not destination.is_absolute():
        destination = library_root / destination
    destination = destination.resolve()
    if destination == Path(destination.anchor):
        raise ValueError(T("choose a specific export folder"))
    return destination


def photo_destination(recipe: dict, *, library_root: Path,
                      source: Path, source_root: Path) -> Path:
    """Resolve one photo with the same rules for a single or mixed-source batch."""
    mode = recipe.get("destinationMode", "fixed")
    if mode == "fixed":
        return resolve_destination(library_root, recipe.get("destination"))
    source, root = source.resolve(), source_root.resolve()
    relative = source.relative_to(root)
    if mode == "original-folder-relative":
        value = str(recipe.get("destination") or "film-exports")
        part = Path(value)
        if not part.parts or part.is_absolute() or ".." in part.parts or "\\" in value or ":" in value:
            raise ValueError(T("Use a relative subfolder without '..' for original-folder exports"))
        destination = (source.parent / part).resolve()
        if destination != source.parent and source.parent not in destination.parents:
            raise ValueError(T("The export subfolder must stay inside the original folder"))
        return destination
    base = resolve_destination(library_root, recipe.get("destination"))
    # A stable root suffix prevents identically named sources from merging.
    namespace = _safe_piece(root.name, "source") + "-" + hashlib.sha256(
        str(root).encode("utf-8")).hexdigest()[:8]
    destination = (base / namespace / relative.parent).resolve()
    if base not in destination.parents:
        raise ValueError(T("The export hierarchy must stay inside its destination"))
    return destination


def capture_timestamp(metadata: dict, policy: str = "require-offset") -> tuple[float | None, str | None]:
    """EXIF time is wall time: use its offset, or an explicit local-time opt-in."""
    raw = str(metadata.get("DateTimeOriginal") or "").strip()
    if not raw:
        return None, T("Capture time is missing; kept the export file timestamp.")
    raw = re.sub(r"^(\d{4}):(\d{2}):(\d{2})", r"\1-\2-\3", raw)
    offset = str(metadata.get("OffsetTimeOriginal") or "").strip()
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if value.tzinfo is None and offset:
            value = datetime.fromisoformat(raw + offset)
        if value.tzinfo is None and policy != "local":
            return None, T("Capture timezone is missing; kept the export file timestamp.")
        if value.tzinfo is None:
            # datetime.timestamp applies the system timezone at the capture date,
            # including its historical DST offset, not today's UTC offset.
            return value.timestamp(), T("Capture timezone is missing; used this computer's local timezone.")
        return value.timestamp(), None
    except (ValueError, OverflowError, OSError):
        return None, T("Capture time or timezone is invalid; kept the export file timestamp.")


def apply_capture_timestamp(path: Path, metadata: dict, policy: str) -> str | None:
    timestamp, warning = capture_timestamp(metadata, policy)
    if timestamp is not None:
        try:
            os.utime(path, (timestamp, timestamp))
        except (OSError, OverflowError, ValueError):
            return T("The filesystem could not preserve capture time; kept its export timestamp.")
    return warning


def _safe_piece(value, default="untitled") -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", str(value or ""))
    value = re.sub(r"\s+", " ", value).strip(" ._")
    # Names must also fit destinations that count UTF-8 bytes, with room for
    # a recipe sidecar, collision suffix and the atomic staging filename.
    return (value or default).encode("utf-8")[:120].decode("utf-8", errors="ignore")


def render_filename(template: str, context: dict, extension: str) -> str:
    def replace(match):
        token = match.group(1)
        return _safe_piece(context.get(token, ""), token)

    rendered = re.sub(r"\{([a-zA-Z]+)\}", replace, str(template or ""))
    rendered = _safe_piece(rendered)
    extension = _safe_piece(extension, "jpg").lower()
    return f"{rendered}.{extension}"


def collision_path(path: Path, policy: str,
                   reserved: set[Path] | None = None,
                   companions: tuple[str, ...] = ()) -> Path | None:
    reserved = reserved or set()
    def occupied(candidate: Path) -> bool:
        return candidate.exists() or candidate in reserved or any(
            Path(str(candidate) + suffix).exists() for suffix in companions)

    if (not occupied(path)) or (
            policy == "overwrite" and path not in reserved):
        return path
    if policy == "skip":
        return None
    for index in range(2, 10000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not occupied(candidate):
            return candidate
    raise ValueError(T("could not find a free name for {name}", name=f'{path.name}'))


def output_dimensions(width: int, height: int, *, rotate=0, crop=None,
                      long_edge=None) -> tuple[int, int]:
    """Mirror export's rotation, pixel-rounded crop, then downsize geometry."""
    if int(round(float(rotate) / 90)) % 2:
        width, height = height, width
    if crop:
        x0 = int(round(crop["x"] * width))
        y0 = int(round(crop["y"] * height))
        width = min(width - x0, max(1, int(round(crop["w"] * width))))
        height = min(height - y0, max(1, int(round(crop["h"] * height))))
    if width < 1 or height < 1:
        raise ValueError(T("Crop has no output pixels"))
    if long_edge and max(width, height) > int(long_edge):
        scale = int(long_edge) / max(width, height)
        width, height = max(1, round(width * scale)), max(1, round(height * scale))
    return width, height


def _watermark_font(app_root: Path, size: float):
    """Prefer a bundled face so exports look the same on every machine."""
    from PIL import ImageFont

    points = max(6, int(round(size)))
    folder = Path(app_root) / "web" / "fonts"
    try:
        faces = sorted(
            [*folder.glob("*.ttf"), *folder.glob("*.otf")],
            key=lambda item: item.name.lower())
    except OSError:
        faces = []
    for face in faces:
        try:
            return ImageFont.truetype(str(face), points)
        except (OSError, ValueError):
            continue
    try:
        return ImageFont.load_default(size=points)
    except TypeError:  # Pillow before the sized default face
        return ImageFont.load_default()


def _premultiplied(mask_rgb, alpha):
    """Return the ``(premultiplied colour, alpha)`` pair to composite."""
    import numpy as np

    return (np.asarray(mask_rgb, dtype=np.float32) * alpha,
            np.asarray(alpha, dtype=np.float32))


def _text_mark(spec: dict, height: int, width: int, app_root: Path):
    import numpy as np
    from PIL import Image, ImageDraw

    text = spec["text"].strip()
    if not text:
        return None
    font = _watermark_font(app_root, spec["scale"] * min(height, width))
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    box = probe.textbbox((0, 0), text, font=font)
    text_w = max(1, box[2] - box[0])
    text_h = max(1, box[3] - box[1])
    offset = max(1, int(round(text_h / 16.0)))
    size = (text_w + offset, text_h + offset)

    def stencil(shift: int):
        layer = Image.new("L", size, 0)
        ImageDraw.Draw(layer).text(
            (shift - box[0], shift - box[1]), text, font=font, fill=255)
        return np.asarray(layer, dtype=np.float32) / 255.0

    ink = stencil(0)[..., None]
    shadow = stencil(offset)[..., None] * 0.55
    # White glyphs over a dark drop shadow: legible on light and dark frames.
    alpha = ink + shadow * (1.0 - ink)
    colour = np.divide(ink, alpha, out=np.zeros_like(ink), where=alpha > 0.0)
    return _premultiplied(np.repeat(colour, 3, axis=2), alpha)


def _image_mark(spec: dict, height: int, width: int):
    import numpy as np
    from PIL import Image

    path = Path(spec["imagePath"]).expanduser()
    if not spec["imagePath"] or not path.is_file():
        return None
    with Image.open(path) as opened:
        source = opened.convert("RGBA")
    long_edge = max(1, int(round(spec["scale"] * max(height, width))))
    ratio = long_edge / max(source.size)
    resized = source.resize(
        (max(1, int(round(source.size[0] * ratio))),
         max(1, int(round(source.size[1] * ratio)))),
        Image.Resampling.LANCZOS)
    pixels = np.asarray(resized, dtype=np.float32) / 255.0
    return _premultiplied(pixels[..., :3], pixels[..., 3:4])


def _mark_origin(spec: dict, height: int, width: int,
                 mark_h: int, mark_w: int) -> tuple[int, int]:
    inset = int(round(spec["inset"] * min(height, width)))
    if spec["anchor"] == "center":
        left, top = (width - mark_w) // 2, (height - mark_h) // 2
    else:
        vertical, horizontal = spec["anchor"].split("-")
        left = inset if horizontal == "left" else width - inset - mark_w
        top = inset if vertical == "top" else height - inset - mark_h
    return (max(0, min(top, max(0, height - mark_h))),
            max(0, min(left, max(0, width - mark_w))))


def apply_watermark(rgb, spec, app_root: Path | None = None):
    """Composite a text or image watermark onto a resized export.

    ``rgb`` is the display-referred, sRGB-encoded float32 image that
    ``color_pipeline.save_export_image()`` receives, so the mark is composited
    in that same encoding and returned in it unchanged -- no linearisation.
    Returns ``rgb`` itself whenever the spec is disabled or yields nothing to
    draw; a broken font or a missing overlay file must never fail an export.
    """
    try:
        import numpy as np

        spec = clean_watermark(spec)
        if not spec["enabled"] or spec["opacity"] <= 0.0:
            return rgb
        base = np.asarray(rgb, dtype=np.float32)
        if base.ndim != 3 or base.shape[2] < 3:
            return rgb
        height, width = base.shape[:2]
        mark = (_image_mark(spec, height, width) if spec["kind"] == "image"
                else _text_mark(spec, height, width, app_root or APP_ROOT))
        if mark is None:
            return rgb
        colour, alpha = mark
        mark_h = min(colour.shape[0], height)
        mark_w = min(colour.shape[1], width)
        if mark_h < 1 or mark_w < 1:
            return rgb
        top, left = _mark_origin(spec, height, width, mark_h, mark_w)
        colour = colour[:mark_h, :mark_w] * spec["opacity"]
        alpha = alpha[:mark_h, :mark_w] * spec["opacity"]
        result = base.copy()
        window = result[top:top + mark_h, left:left + mark_w, :3]
        result[top:top + mark_h, left:left + mark_w, :3] = np.clip(
            window * (1.0 - alpha) + colour, 0.0, 1.0)
        return result.astype(np.float32, copy=False)
    except Exception:  # noqa: BLE001 - a watermark must not lose the export
        return rgb
