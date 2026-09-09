"""Non-destructive local corrections, healing, and optical geometry.

The schema in this module is shared by saved state, preview requests, presets,
versions, and export sidecars. Geometry is expressed in normalised image
coordinates so the same edit can be rendered at preview and export sizes.
"""
from __future__ import annotations

import base64
import math
import os
import re
from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

import grade
import mask_raster
from server_localization import T


MAX_MASKS = 16
MAX_MASK_COMPONENTS = 12
MAX_STROKES = 64
MAX_POINTS = 512
MAX_TOTAL_MASK_POINTS = 20_000
MAX_BITMAP_EDGE = 1024
MAX_HEALS = 50
# A linear gradient shorter than this fraction of the frame has no usable
# direction and contributes nothing. Mirrored by LINEAR_MIN_SPAN in
# web/editor-panels.js so the preview and the export agree at any resolution.
LINEAR_MIN_SPAN = 1e-4
LOCAL_GRADE_KEYS = (
    "exposure", "contrast", "highlights", "shadows",
    "whites", "blacks", "temp", "tint", "saturation", "texture", "clarity",
)
OPTICS_DEFAULTS = {
    "profileEnabled": False,
    "profileOverride": None,
    "profileDistortion": True,
    "profileVignette": True,
    "flipHorizontal": False,
    "flipVertical": False,
    "distortion": 0.0,
    "vignette": 0.0,
    "vertical": 0.0,
    "horizontal": 0.0,
    "rotate": 0.0,
    "scale": 1.0,
}
def _finite(value, default=0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _clamp(value, minimum=0.0, maximum=1.0, default=0.0) -> float:
    return round(max(minimum, min(maximum, _finite(value, default))), 5)


def _point(value, default=(0.5, 0.5)) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        value = default
    return [_clamp(value[0]), _clamp(value[1])]


def clean_local_grade(value) -> dict:
    raw = value if isinstance(value, dict) else {}
    cleaned = grade.clean(raw)
    return {key: cleaned[key] for key in (*LOCAL_GRADE_KEYS, *grade.CURVE_KEYS)
            if key in cleaned}


def _clean_strokes(values, point_budget: list[int] | None = None) -> list[dict]:
    result = []
    for raw in values[:MAX_STROKES] if isinstance(values, list) else []:
        if not isinstance(raw, dict):
            continue
        available = MAX_POINTS
        if point_budget is not None:
            available = min(available, max(0, point_budget[0]))
        points = [_point(point) for point in (
            raw.get("points", [])[:available]
            if isinstance(raw.get("points"), list) else [])]
        if not points:
            continue
        if point_budget is not None:
            point_budget[0] -= len(points)
        stroke = {
            "size": _clamp(raw.get("size"), 0.005, 0.5, 0.08),
            "feather": _clamp(raw.get("feather"), 0.0, 1.0, 0.65),
            "flow": _clamp(raw.get("flow"), 0.01, 1.0, 1.0),
            "points": points,
        }
        if raw.get("buildUp"):
            stroke["buildUp"] = True
            stroke["density"] = _clamp(raw.get("density"), 0.01, 1.0, 1.0)
            edge = _clean_bitmap(raw.get("edgeMask"))
            if edge:
                stroke["edgeMask"] = edge
            elif "edgeMask" in raw:
                raise ValueError(T("Saved Auto Mask data is missing or damaged. Restore the stroke from History before exporting."))
        result.append(stroke)
    return result


def _clean_bitmap(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    try:
        encoding = str(value.get("encoding", "raw")).casefold()
        maximum = MAX_BITMAP_EDGE if encoding == "png" else 256
        width = max(1, min(maximum, int(value.get("width", 0))))
        height = max(1, min(maximum, int(value.get("height", 0))))
        data = str(value.get("data", ""))
        decoded = base64.b64decode(data, validate=True)
    except (TypeError, ValueError):
        return None
    if encoding == "png":
        try:
            from io import BytesIO
            with Image.open(BytesIO(decoded)) as bitmap:
                if bitmap.format != "PNG" or bitmap.size != (width, height):
                    return None
                bitmap.verify()
        except (OSError, ValueError):
            return None
        return {"width": width, "height": height, "encoding": "png",
                "data": data}
    if len(decoded) != width * height:
        return None
    return {"width": width, "height": height, "data": data}


def _clean_mask_component(raw, index: int,
                          point_budget: list[int] | None = None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("type") if raw.get("type") in {
        "brush", "linear", "radial", "subject", "sky", "object",
        "depth", "person", "face-skin", "eyes", "eyebrows", "lips",
        "teeth", "hair",
    } else "radial"
    component = {
        "id": str(raw.get("id") or f"component-{index + 1}")[:100],
        "type": kind,
        "combine": raw.get("combine") if raw.get("combine") in {
            "add", "subtract", "intersect"
        } else "add",
        "invert": bool(raw.get("invert", False)),
    }
    if kind == "brush":
        component["strokes"] = _clean_strokes(raw.get("strokes"), point_budget)
    elif kind == "linear":
        component["start"] = _point(raw.get("start"), (0.25, 0.5))
        component["end"] = _point(raw.get("end"), (0.75, 0.5))
    elif kind == "radial":
        component["center"] = _point(raw.get("center"))
        component["radius"] = _clamp(raw.get("radius"), 0.01, 1.5, 0.25)
        for key in ("radiusX", "radiusY"):
            component[key] = _clamp(raw.get(key), 0.01, 1.5, component["radius"])
        component["angle"] = _clamp(raw.get("angle"), -180.0, 180.0, 0.0)
        component["feather"] = _clamp(raw.get("feather"), 0.0, 1.0, 0.65)
    else:
        bitmap = _clean_bitmap(raw.get("bitmap"))
        if not bitmap:
            return None
        component["bitmap"] = bitmap
        component["provider"] = str(raw.get("provider", "on-device"))[:40]
        if kind == "depth":
            component["depthLow"] = _clamp(raw.get("depthLow"), 0.0, 1.0, 0.55)
            component["depthHigh"] = _clamp(raw.get("depthHigh"), 0.0, 1.0, 1.0)
            if component["depthLow"] > component["depthHigh"]:
                component["depthLow"], component["depthHigh"] = (
                    component["depthHigh"], component["depthLow"])
    return component


def require_saved_mask_assets(values) -> None:
    """Delivery must never silently drop an accepted AI selection or refinement."""
    for mask in values if isinstance(values, list) else []:
        if not isinstance(mask, dict) or mask.get("enabled") is False:
            continue
        components = mask.get("components")
        components = components if isinstance(components, list) else [mask]
        # Browser refinements are migrated into components by clean_masks.
        # Validate their frozen edge selections before normalization as well.
        refinements = [{"strokes": mask.get(field)} for field in
                       ("addStrokes", "subtractStrokes", "intersectStrokes")]
        for component in [*components, *refinements]:
            if not isinstance(component, dict):
                continue
            for stroke in component.get("strokes", []) if isinstance(component.get("strokes"), list) else []:
                if isinstance(stroke, dict) and stroke.get("edgeMask") is not None:
                    if _clean_bitmap(stroke["edgeMask"]) is None:
                        raise ValueError(T("Saved Auto Mask data is missing or damaged. Restore the stroke from History before exporting."))
            kind = component.get("type")
            if kind in {"subject", "sky", "object", "depth", "person", "face-skin",
                        "eyes", "eyebrows", "lips", "teeth", "hair"}:
                if _clean_bitmap(component.get("bitmap")) is None:
                    label = str(mask.get("name") or mask.get("id") or kind)[:100]
                    raise ValueError(f"Saved AI data for '{label}' is missing or damaged. "
                                     "Restore it from History or regenerate and review the mask before exporting.")


def clean_masks(values) -> list[dict]:
    result = []
    point_budget = [MAX_TOTAL_MASK_POINTS]
    for index, raw in enumerate(values[:MAX_MASKS] if isinstance(values, list) else []):
        if not isinstance(raw, dict):
            continue
        component_values = raw.get("components")
        if not isinstance(component_values, list):
            # A flat mask's invert flag belongs to the entire selection.
            # Canonical components have an independent invert flag; copying
            # the legacy flag into both levels would cancel its inversion.
            component_values = [{**raw, "invert": False}]
        # The browser carries fresh refinements in flat stroke fields that this
        # migration folds into the component model. Reserve their slots before
        # taking stored components, so a mask already at the cap discards the
        # oldest stored component rather than the stroke just painted; filling
        # the cap first dropped that stroke with no report of any kind.
        refinements = [
            (field, combine, _clean_strokes(raw.get(field), point_budget))
            for field, combine in (("addStrokes", "add"),
                                   ("subtractStrokes", "subtract"),
                                   ("intersectStrokes", "intersect"))
        ]
        refinements = [item for item in refinements if item[2]]
        stored_budget = max(1, MAX_MASK_COMPONENTS - len(refinements))
        components = []
        for component_index, component_raw in enumerate(
                component_values[:stored_budget]):
            component = _clean_mask_component(
                component_raw, component_index, point_budget)
            if component:
                if not components:
                    component["combine"] = "add"
                components.append(component)
        for field, combine, strokes in refinements:
            if len(components) < MAX_MASK_COMPONENTS:
                components.append({
                    "id": f"{field}-{index + 1}", "type": "brush",
                    "combine": combine, "invert": False, "strokes": strokes,
                })
        if not components:
            continue
        kind = components[0]["type"]
        item = {
            "id": str(raw.get("id") or f"mask-{index + 1}")[:100],
            "name": " ".join(str(raw.get("name") or f"Mask {index + 1}").split())[:60],
            "type": kind,
            "enabled": raw.get("enabled") is not False,
            "invert": bool(raw.get("invert", False)),
            "opacity": _clamp(raw.get("opacity"), 0.0, 1.0, 1.0),
            "lumaLow": _clamp(raw.get("lumaLow"), 0.0, 1.0, 0.0),
            "lumaHigh": _clamp(raw.get("lumaHigh"), 0.0, 1.0, 1.0),
            "colorHue": None,
            "colorRange": _clamp(raw.get("colorRange"), 2.0, 90.0, 30.0),
            "colorAmount": _clamp(raw.get("colorAmount"), 0.0, 1.0, 1.0),
            "grade": clean_local_grade(raw.get("grade")),
            "components": components,
        }
        if item["lumaLow"] > item["lumaHigh"]:
            item["lumaLow"], item["lumaHigh"] = item["lumaHigh"], item["lumaLow"]
        if raw.get("colorHue") is not None:
            hue = _finite(raw.get("colorHue"), math.nan)
            if math.isfinite(hue):
                item["colorHue"] = round(hue % 360.0, 3)
        # Retain the first component's geometry for backwards-compatible
        # callers while the canonical schema is the ordered component list.
        for key in ("strokes", "start", "end", "center", "radius", "radiusX", "radiusY", "angle", "feather",
                    "bitmap", "provider", "depthLow", "depthHigh"):
            if key in components[0]:
                item[key] = components[0][key]
        result.append(item)
    return result


def clean_heals(values) -> list[dict]:
    result = []
    for index, raw in enumerate(values[:MAX_HEALS] if isinstance(values, list) else []):
        if not isinstance(raw, dict):
            continue
        result.append({
            "id": str(raw.get("id") or f"heal-{index + 1}")[:100],
            "mode": raw.get("mode") if raw.get("mode") in {"remove", "clone"} else "heal",
            "enabled": raw.get("enabled") is not False,
            "target": _point(raw.get("target")),
            "source": _point(raw.get("source"), (0.4, 0.4)),
            "radius": _clamp(raw.get("radius"), 0.005, 0.25, 0.04),
            "feather": _clamp(raw.get("feather"), 0.0, 1.0, 0.65),
            "opacity": _clamp(raw.get("opacity"), 0.0, 1.0, 1.0),
        })
    return result


def clean_optics(value) -> dict:
    raw = value if isinstance(value, dict) else {}
    result = dict(OPTICS_DEFAULTS)
    for key in ("profileEnabled", "profileDistortion", "profileVignette",
                "flipHorizontal", "flipVertical"):
        result[key] = bool(raw.get(key, OPTICS_DEFAULTS[key]))
    for key in ("distortion", "vignette", "vertical", "horizontal"):
        result[key] = _clamp(raw.get(key), -1.0, 1.0, OPTICS_DEFAULTS[key])
    override = raw.get("profileOverride")
    if isinstance(override, dict):
        keys = ("cameraMaker", "cameraModel", "lensMaker", "lensModel")
        if all(isinstance(override.get(key), str) and 0 < len(override[key]) <= 256 for key in keys):
            result["profileOverride"] = {key: override[key] for key in keys}
    result["rotate"] = _clamp(raw.get("rotate"), -15.0, 15.0, 0.0)
    result["scale"] = _clamp(raw.get("scale"), 1.0, 1.6, 1.0)
    return result


def optics_is_identity(value) -> bool:
    optics = clean_optics(value)
    return (not optics["profileEnabled"]
            and not optics["flipHorizontal"]
            and not optics["flipVertical"]
            and all(abs(optics[key] - OPTICS_DEFAULTS[key]) < 1e-7
                    for key in ("distortion", "vignette", "vertical",
                                "horizontal", "rotate", "scale")))


def base_edits_are_identity(optics, heals) -> bool:
    return optics_is_identity(optics) and not any(
        item["enabled"] for item in clean_heals(heals))


def _smoothstep(edge0, edge1, value):
    if edge1 <= edge0:
        return (value >= edge1).astype(np.float32)
    t = np.clip((value - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


@lru_cache(maxsize=1)
def _map_coordinates():
    """Load SciPy only when a resampling edit is actually used."""
    from scipy.ndimage import map_coordinates
    return map_coordinates


def _raster_component(component: dict, height: int, width: int) -> np.ndarray:
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    minimum = float(max(1, min(width, height)))
    if component["type"] == "radial":
        cx, cy = component["center"]
        dx, dy = xx - cx * (width - 1), yy - cy * (height - 1)
        angle = math.radians(component.get("angle", 0))
        rx = max(component.get("radiusX", component["radius"]) * minimum, 1.0)
        ry = max(component.get("radiusY", component["radius"]) * minimum, 1.0)
        normalised = np.hypot((dx * math.cos(angle) + dy * math.sin(angle)) / rx,
                             (-dx * math.sin(angle) + dy * math.cos(angle)) / ry)
        feather = component["feather"]
        weight = 1.0 - _smoothstep(max(0.0, 1.0 - feather), 1.0, normalised)
    elif component["type"] == "linear":
        sx, sy = component["start"]; ex, ey = component["end"]
        # Decide degeneracy in normalized coordinates. A pixel-sized threshold
        # answers differently at preview and at export resolution, which made a
        # collapsed gradient vanish on screen while the export clamped its
        # denominator into a step function and graded half the frame.
        if math.hypot(ex - sx, ey - sy) < LINEAR_MIN_SPAN:
            return np.zeros((height, width), dtype=np.float32)
        sx *= width - 1; ex *= width - 1
        sy *= height - 1; ey *= height - 1
        dx, dy = ex - sx, ey - sy
        # Projection and denominator both scale with the raster, so the ramp is
        # resolution independent once the degenerate case is out of the way.
        denominator = dx * dx + dy * dy
        weight = _smoothstep(0.0, 1.0, ((xx - sx) * dx + (yy - sy) * dy) / denominator)
    elif component["type"] == "brush":
        combined = np.zeros((height, width), dtype=np.float32)
        for stroke in component.get("strokes", []):
            if stroke.get("buildUp"):
                coverage = mask_raster.stroke_coverage(stroke, height, width)
                if stroke.get("edgeMask"):
                    coverage *= _raster_component({"type": "subject", "invert": False,
                                                   "bitmap": stroke["edgeMask"]}, height, width)
                combined = mask_raster.accumulate(combined, coverage, stroke)
                continue
            layer = Image.new("L", (width, height), 0)
            draw = ImageDraw.Draw(layer)
            points = [(round(x * (width - 1)), round(y * (height - 1)))
                      for x, y in stroke["points"]]
            diameter = max(1, round(stroke["size"] * minimum))
            fill = round(stroke["flow"] * 255)
            if len(points) == 1:
                x, y = points[0]; radius = diameter / 2
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)
            else:
                draw.line(points, fill=fill, width=diameter, joint="curve")
                radius = diameter / 2
                for x, y in (points[0], points[-1]):
                    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)
            blur = stroke["feather"] * diameter * 0.35
            if blur > 0.25:
                layer = layer.filter(ImageFilter.GaussianBlur(blur))
            combined = np.maximum(combined, np.asarray(layer, dtype=np.float32) / 255.0)
        weight = combined
    else:
        bitmap = component["bitmap"]
        decoded = base64.b64decode(bitmap["data"])
        if bitmap.get("encoding") == "png":
            from io import BytesIO
            with Image.open(BytesIO(decoded)) as source:
                values = np.asarray(source.convert("L"), dtype=np.uint8)
        else:
            values = np.frombuffer(decoded, dtype=np.uint8)
            values = values.reshape(bitmap["height"], bitmap["width"])
        layer = Image.fromarray(values, "L").resize(
            (width, height), Image.Resampling.BILINEAR)
        weight = np.asarray(layer, dtype=np.float32) / 255.0
        if component["type"] == "depth":
            low = component["depthLow"]
            high = component["depthHigh"]
            lower = 1.0 if low <= 0 else _smoothstep(
                low - 0.04, low + 0.04, weight)
            upper = 1.0 if high >= 1 else 1.0 - _smoothstep(
                high - 0.04, high + 0.04, weight)
            weight = lower * upper
    if component["invert"]:
        weight = 1.0 - weight
    return np.clip(weight, 0.0, 1.0).astype(np.float32)


def raster_mask(mask: dict, height: int, width: int) -> np.ndarray:
    mask = clean_masks([mask])[0]
    weight = np.zeros((height, width), dtype=np.float32)
    for index, component in enumerate(mask["components"]):
        layer = _raster_component(component, height, width)
        combine = component["combine"] if index else "add"
        if combine == "subtract":
            weight *= 1.0 - layer
        elif combine == "intersect":
            weight = np.minimum(weight, layer)
        else:
            weight = np.maximum(weight, layer)
    if mask["invert"]:
        weight = 1.0 - weight
    return np.clip(weight * mask["opacity"], 0.0, 1.0).astype(np.float32)


def _luminance_weight(image: np.ndarray, low: float, high: float) -> np.ndarray:
    luma = image @ grade.LUMA
    lower = np.ones_like(luma) if low <= 0 else _smoothstep(low - 0.04, low + 0.04, luma)
    upper = np.ones_like(luma) if high >= 1 else 1.0 - _smoothstep(high - 0.04, high + 0.04, luma)
    return lower * upper


def _color_weight(image: np.ndarray, hue: float | None,
                  color_range: float, amount: float) -> np.ndarray:
    """Point Color's hue weight, composed as a mask refinement.

    Keeping this expression identical to :func:`grade._apply_point_color`
    makes a sampled mask range and a Point Color range select the same pixels.
    """
    if hue is None or amount <= 0:
        return np.ones(image.shape[:2], dtype=np.float32)
    image_hue, saturation, _ = grade._rgb_to_hsv(image)
    difference = np.abs(((image_hue - hue + 180.0) % 360.0) - 180.0)
    selected = 1.0 - _smoothstep(color_range * 0.45,
                                 color_range, difference)
    selected *= saturation
    return np.clip(1.0 - amount * (1.0 - selected), 0.0, 1.0).astype(
        np.float32)


def apply_masks(image: np.ndarray, masks, *, accelerated: bool = False) -> np.ndarray:
    output = np.clip(image.astype(np.float32), 0.0, 1.0)
    for mask in clean_masks(masks):
        if not mask["enabled"] or grade.is_identity(mask["grade"]):
            continue
        weight = raster_mask(mask, *output.shape[:2])
        active = weight > 1e-6
        rows = np.flatnonzero(np.any(active, axis=1))
        columns = np.flatnonzero(np.any(active, axis=0))
        if not rows.size or not columns.size:
            continue
        y0, y1 = int(rows[0]), int(rows[-1]) + 1
        x0, x1 = int(columns[0]), int(columns[-1]) + 1
        region = output[y0:y1, x0:x1]
        region_weight = weight[y0:y1, x0:x1]
        region_weight *= _luminance_weight(
            region, mask["lumaLow"], mask["lumaHigh"])
        region_weight *= _color_weight(
            region, mask["colorHue"], mask["colorRange"],
            mask["colorAmount"])
        if not np.any(region_weight > 1e-6):
            continue
        apply_grade = grade.apply_accelerated if accelerated else grade.apply
        if mask["grade"]["texture"] or mask["grade"]["clarity"]:
            # Local detail uses the source's one-pixel cross neighbors.
            # Include them beyond the mask bounds, then discard the halo so
            # the selection limits changed pixels rather than sampled pixels.
            height, width = output.shape[:2]
            sy0, sy1 = max(0, y0 - 1), min(height, y1 + 1)
            sx0, sx1 = max(0, x0 - 1), min(width, x1 + 1)
            adjusted = apply_grade(output[sy0:sy1, sx0:sx1], mask["grade"])[
                y0 - sy0:y1 - sy0, x0 - sx0:x1 - sx0]
        else:
            adjusted = apply_grade(region, mask["grade"])
        output[y0:y1, x0:x1] = (
            region * (1.0 - region_weight[..., None])
            + adjusted * region_weight[..., None])
    return np.clip(output, 0.0, 1.0).astype(np.float32)


def apply_heals(image: np.ndarray, heals) -> np.ndarray:
    output = np.clip(image.astype(np.float32), 0.0, 1.0).copy()
    height, width = output.shape[:2]
    minimum = max(1, min(width, height))
    for spot in clean_heals(heals):
        if not spot["enabled"]:
            continue
        tx, ty = spot["target"]; sx, sy = spot["source"]
        radius = max(1.0, spot["radius"] * minimum)
        cx, cy = tx * (width - 1), ty * (height - 1)
        pad = math.ceil(radius + 2)
        x0, x1 = max(0, int(cx) - pad), min(width, int(cx) + pad + 1)
        y0, y1 = max(0, int(cy) - pad), min(height, int(cy) + pad + 1)
        if x1 <= x0 or y1 <= y0:
            continue
        yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        distance = np.hypot(xx - cx, yy - cy) / radius
        inner = max(0.0, 1.0 - spot["feather"])
        weight = (1.0 - _smoothstep(inner, 1.0, distance)) * spot["opacity"]
        patch = output[y0:y1, x0:x1]
        if spot["mode"] == "remove":
            try:
                from skimage.restoration import inpaint
                sampled = inpaint.inpaint_biharmonic(
                    patch, distance <= 1.0, channel_axis=-1).astype(np.float32)
            except Exception:
                # Keep removal usable in a minimal runtime: the deterministic
                # heal fallback still respects the explicit source coordinate.
                spot["mode"] = "heal"
        if spot["mode"] != "remove":
            source_x = xx + (sx - tx) * (width - 1)
            source_y = yy + (sy - ty) * (height - 1)
            sampled = np.stack([
                _map_coordinates()(output[..., channel], [source_y, source_x],
                                   order=1, mode="reflect")
                for channel in range(3)
            ], axis=2)
        if spot["mode"] == "heal":
            ring = (distance >= 0.72) & (distance <= 1.0)
            if np.any(ring):
                target_mean = patch[ring].mean(axis=0)
                source_mean = sampled[ring].mean(axis=0)
                sampled = np.clip(sampled + (target_mean - source_mean), 0.0, 1.0)
        output[y0:y1, x0:x1] = (
            patch * (1.0 - weight[..., None]) + sampled * weight[..., None])
    return np.clip(output, 0.0, 1.0).astype(np.float32)


def _normalise_text(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _metadata_number(value, default) -> float:
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", str(value or ""))
    return _finite(match.group(0), default) if match else default


@lru_cache(maxsize=1)
def _lens_database():
    import lensfunpy
    return lensfunpy.Database()


def _compatible_camera_alias(database, maker: str, model: str):
    """Find one unambiguous adjacent model without hard-coded brand data.

    This is useful for fixed-lens successors and mount-compatible bodies added
    after the bundled database release. A strict shared model prefix must beat
    every alternative; otherwise automatic correction stays off.
    """
    target = _normalise_text(model).replace(" ", "")
    if len(target) < 5:
        return None
    try:
        candidates = database.find_cameras(
            maker=maker or None, model=model, loose_search=True)
    except Exception:
        return None
    ranked = []
    for candidate in candidates:
        value = _normalise_text(candidate.model).replace(" ", "")
        prefix = len(os.path.commonprefix((target, value)))
        if prefix < 5 or abs(len(target) - len(value)) > 2:
            continue
        if not (target.startswith(value) or value.startswith(target)):
            continue
        ranked.append((prefix, int(getattr(candidate, "score", 0)), candidate))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not ranked or (len(ranked) > 1 and ranked[0][0] == ranked[1][0]):
        return None
    return ranked[0][2]


def lens_match_for(metadata: dict | None, override=None) -> dict:
    """Explain automatic matching and offer explicit compatible profile choices."""
    metadata = metadata or {}
    maker, model = str(metadata.get("Make", "")), str(metadata.get("Model", ""))
    result = {"found": False, "profile": None, "reason": "Camera model is missing from the photo metadata.", "candidates": []}
    if not model:
        return result
    try:
        database = _lens_database()
        cameras = database.find_cameras(maker=maker or None, model=model, loose_search=False)
        aliased = False
        if not cameras:
            compatible = _compatible_camera_alias(database, maker, model)
            cameras = [compatible] if compatible else []
            aliased = bool(compatible)
        if len(cameras) != 1:
            result["reason"] = "Camera profile is missing or ambiguous in the bundled lens database."
            return result
        camera = cameras[0]
        focal = _metadata_number(metadata.get("FocalLength"), 0.0)
        aperture = _metadata_number(metadata.get("FNumber"), 5.6)
        distance = _metadata_number(metadata.get("FocusDistance"), 1000.0)

        def compatible(lens):
            return not focal or lens.min_focal - 0.2 <= focal <= lens.max_focal + 0.2

        def spec(lens):
            return {"cameraMaker": camera.maker, "cameraModel": camera.model,
                    "lensMaker": lens.maker, "lensModel": lens.model,
                    "cropFactor": round(float(camera.crop_factor), 5),
                    "focal": round(focal or float(lens.min_focal), 4),
                    "aperture": round(aperture, 4), "distance": round(max(distance, 0.01), 4),
                    "hasDistortion": bool(lens.calib_distortion),
                    "hasVignette": bool(lens.calib_vignetting),
                    "hasTca": bool(lens.calib_tca), "aliasedCamera": aliased}

        candidates = [lens for lens in database.find_lenses(camera, lens=None, loose_search=False) if compatible(lens)]
        # A stable identity makes duplicate database records one choice.
        candidates = list({(lens.maker, lens.model): lens for lens in candidates}.values())
        candidates.sort(key=lambda lens: (lens.maker, lens.model))
        result["candidates"] = [spec(lens) for lens in candidates]
        if override:
            selected = [lens for lens in candidates if all(
                spec(lens)[key] == override.get(key)
                for key in ("cameraMaker", "cameraModel", "lensMaker", "lensModel"))]
            if len(selected) != 1:
                result["reason"] = "The selected profile is unavailable or incompatible with this camera and focal length. Choose another profile."
                return result
            lenses = selected
            reason = "Profile selected manually. Verify the correction against the original."
        else:
            lens_name = metadata.get("LensModel") or metadata.get("LensID")
            if lens_name:
                lenses = database.find_lenses(camera, lens=str(lens_name), loose_search=False)
                reason = "Exact camera and lens metadata match."
                if not lenses:
                    lenses = [lens for lens in database.find_lenses(camera, lens=str(lens_name), loose_search=True)
                              if lens.score >= 40]
                    reason = "One compatible lens name match. Verify the correction against the original."
                lenses = [lens for lens in lenses if compatible(lens)]
                lenses = list({(lens.maker, lens.model): lens for lens in lenses}.values())
            else:
                lenses = candidates if focal > 0 else []
                reason = "Only one camera-compatible profile matches the recorded focal length."
            if len(lenses) != 1:
                result["reason"] = ("Multiple lens profiles fit this photo; automatic correction is off. Select the lens used below."
                                    if len(lenses) > 1 else "No unambiguous camera, lens and focal-length match. Select a compatible profile or use manual controls.")
                return result
        profile = spec(lenses[0])
        profile["manualOverride"] = bool(override)
        profile["matchReason"] = reason
        result.update(found=True, profile=profile, reason=reason)
        return result
    except Exception:
        result["reason"] = "The bundled lens database could not be read. Manual controls remain available."
        return result


def lens_profile_for(metadata: dict | None, override=None) -> dict | None:
    return lens_match_for(metadata, override)["profile"]


def _resolve_lens_profile(spec):
    if not isinstance(spec, dict):
        return None
    try:
        database = _lens_database()
        cameras = database.find_cameras(
            maker=spec.get("cameraMaker"), model=spec.get("cameraModel"),
            loose_search=False)
        if not cameras:
            return None
        lenses = database.find_lenses(
            cameras[0], maker=spec.get("lensMaker"), lens=spec.get("lensModel"),
            loose_search=False)
        return (cameras[0], lenses[0]) if lenses else None
    except Exception:
        return None


def apply_lens_profile(image: np.ndarray, optics: dict, spec) -> np.ndarray:
    optics = clean_optics(optics)
    resolved = _resolve_lens_profile(spec)
    if not resolved or not optics["profileEnabled"]:
        return image
    import lensfunpy
    camera, lens = resolved
    height, width = image.shape[:2]
    flags = 0
    if optics["profileDistortion"]:
        flags |= lensfunpy.ModifyFlags.DISTORTION | lensfunpy.ModifyFlags.TCA \
            | lensfunpy.ModifyFlags.SCALE
    if optics["profileVignette"]:
        flags |= lensfunpy.ModifyFlags.VIGNETTING
    modifier = lensfunpy.Modifier(lens, float(camera.crop_factor), width, height)
    modifier.initialize(
        _finite(spec.get("focal"), lens.min_focal),
        _finite(spec.get("aperture"), 5.6),
        max(0.01, _finite(spec.get("distance"), 1000.0)),
        0.0, pixel_format=np.float32, flags=flags,
    )
    source = np.ascontiguousarray(image.astype(np.float32).copy())
    if optics["profileVignette"]:
        modifier.apply_color_modification(source)
    if optics["profileDistortion"]:
        coordinates = modifier.apply_subpixel_geometry_distortion()
        if coordinates is not None:
            source = np.stack([
                _map_coordinates()(source[..., channel],
                                   [coordinates[..., channel, 1],
                                    coordinates[..., channel, 0]],
                                   order=1, mode="constant", cval=0.0)
                for channel in range(3)
            ], axis=2)
    return np.clip(source, 0.0, 1.0).astype(np.float32)


def apply_manual_optics(image: np.ndarray, optics: dict) -> np.ndarray:
    optics = clean_optics(optics)
    distortion = optics["distortion"]
    vertical = optics["vertical"]
    horizontal = optics["horizontal"]
    rotation = math.radians(optics["rotate"])
    scale = optics["scale"]
    if not any((distortion, vertical, horizontal, rotation, scale - 1.0,
                optics["vignette"], optics["flipHorizontal"],
                optics["flipVertical"])):
        return image
    height, width = image.shape[:2]
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    half = max(min(width, height) / 2.0, 1.0)
    nx = (xx - (width - 1) / 2.0) / half / scale
    ny = (yy - (height - 1) / 2.0) / half / scale
    if optics["flipHorizontal"]:
        nx = -nx
    if optics["flipVertical"]:
        ny = -ny
    cosine, sine = math.cos(rotation), math.sin(rotation)
    rx = cosine * nx - sine * ny
    ry = sine * nx + cosine * ny
    px = rx * (1.0 + vertical * 0.45 * ry)
    py = ry * (1.0 + horizontal * 0.45 * rx)
    radius2 = px * px + py * py
    factor = 1.0 + distortion * 0.18 * radius2
    if any((distortion, vertical, horizontal, rotation, scale - 1.0)):
        source_x = px * factor * half + (width - 1) / 2.0
        source_y = py * factor * half + (height - 1) / 2.0
        warped = np.stack([
            _map_coordinates()(image[..., channel], [source_y, source_x],
                               order=1, mode="constant", cval=0.0)
            for channel in range(3)
        ], axis=2)
    else:
        # Discrete flips need no interpolation. Reconstructing their integer
        # coordinates through normalized float32 can put a border just below
        # zero and sample black; it also softens unchanged source pixels.
        warped = image[::(-1 if optics["flipVertical"] else 1),
                       ::(-1 if optics["flipHorizontal"] else 1)].copy()
    if optics["vignette"]:
        radial = np.clip(radius2 / 2.0, 0.0, 1.5)
        warped *= (1.0 + optics["vignette"] * 0.8 * radial)[..., None]
    return np.clip(warped, 0.0, 1.0).astype(np.float32)


def apply_base(image: np.ndarray, optics=None, heals=None,
               lens_profile=None) -> np.ndarray:
    cleaned_optics = clean_optics(optics)
    output = np.clip(image.astype(np.float32), 0.0, 1.0)
    output = apply_lens_profile(output, cleaned_optics, lens_profile)
    output = apply_manual_optics(output, cleaned_optics)
    return apply_heals(output, heals)
