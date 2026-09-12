# SPDX-License-Identifier: GPL-3.0-only
"""Does each control actually do what its name says?

Parity gates compare two implementations of the same model. They pass happily
when both implementations are wrong in the same way, and they say nothing at
all about a control that was never wired up. This module asks the other
question: move one control across its full travel and check that a named,
physically meaningful measurement of the image moves the way the control's
label promises.

Every control earns one of these verdicts:

``directional``  a named claim held: the metric moved the promised way at every
                 step of the sweep.
``monotonic``    no named claim, but the image diverges further from the
                 default the further the control is moved.
``responsive``   the control changes the image without a monotonic reading.
                 Expected for wrapping controls; suspicious otherwise.
``inert``        moving the control across its whole range changes nothing.
``leaky``        the control alters the image while sitting at its default.

``inert`` and ``leaky`` are defects. The rest are descriptions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys

import numpy as np

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import edits  # noqa: E402
import grade  # noqa: E402
from control_inventory import Control, controls  # noqa: E402

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
# Regions of the audit target, as (row slice, column slice).
FLAT_NOISY = (slice(64, 96), slice(0, 32))
FLAT_CLEAN = (slice(64, 96), slice(32, 64))
EDGE = (slice(0, 32), slice(96, 160))
CENTRE = (slice(48, 80), slice(80, 112))
CORNERS = ((slice(0, 16), slice(0, 16)), (slice(0, 16), slice(-16, None)),
           (slice(-16, None), slice(0, 16)), (slice(-16, None), slice(-16, None)))
BAND_HUES = {"red": 0, "orange": 30, "yellow": 60, "green": 120,
             "aqua": 180, "blue": 240, "purple": 280, "magenta": 320}


def audit_target(width: int = 192, height: int = 128) -> np.ndarray:
    """A target built so each claim has an uncontaminated place to measure.

    Deterministic: the noise patch uses a fixed seed so a variance reading is
    repeatable to the bit.
    """
    image = np.zeros((height, width, 3), dtype=np.float32)
    # A full-range luma ramp across the top third, for tone controls.
    ramp = np.linspace(0.0, 1.0, width, dtype=np.float32)
    image[0:32] = ramp[None, :, None]
    # A hard black/white edge inside the ramp band, for sharpening and CA.
    image[EDGE] = 0.0
    image[EDGE[0], slice(128, 160)] = 1.0
    # Saturated patches at every hue band centre, for colour controls.
    patch = width // len(BAND_HUES)
    for index, hue in enumerate(BAND_HUES.values()):
        left = index * patch
        image[32:64, left:left + patch] = _hsv_patch(hue, 0.85, 0.65)
    # Two flat mid-grey fields: one noisy, one clean. Noise reduction must
    # quiet the first; nothing may disturb the second.
    image[64:96] = 0.5
    noise = np.random.default_rng(20260910).normal(0, 0.05, (32, 32, 3))
    image[FLAT_NOISY] = np.clip(0.5 + noise, 0, 1)
    # A smooth radial falloff across the bottom, for vignette geometry.
    y, x = np.mgrid[0:height - 96, 0:width]
    radius = np.hypot((x / width - 0.5) * 2, (y / max(height - 96, 1) - 0.5) * 2)
    image[96:height] = np.clip(0.75 - 0.25 * radius, 0, 1)[..., None]
    # An asymmetric marker so a geometry change cannot cancel itself out.
    image[100:112, 20:32] = (1.0, 0.2, 0.1)
    return np.clip(image, 0, 1).astype(np.float32)


def _hsv_patch(hue, saturation, value):
    hue = (hue % 360) / 60.0
    index = int(hue) % 6
    fraction = hue - int(hue)
    p = value * (1 - saturation)
    q = value * (1 - saturation * fraction)
    t = value * (1 - saturation * (1 - fraction))
    return [(value, t, p), (q, value, p), (p, value, t),
            (p, q, value), (t, p, value), (value, p, q)][index]


# ---------------------------------------------------------------- metrics


def _luma(image):
    return image @ LUMA


def _chroma(image):
    return image.max(axis=-1) - image.min(axis=-1)


def _blur(image, radius=1):
    padded = np.pad(image, ((radius, radius), (radius, radius), (0, 0)), mode="edge")
    total = np.zeros_like(image)
    count = 0
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            total += padded[radius + dy:radius + dy + image.shape[0],
                            radius + dx:radius + dx + image.shape[1]]
            count += 1
    return total / count


def mean_luma(image, base):
    return float(_luma(image).mean())


def contrast(image, base):
    return float(_luma(image).std())


def highlight_mean(image, base):
    """Measured over the pixels that were bright *before* the edit."""
    select = _luma(base) > 0.75
    return float(_luma(image)[select].mean())


def shadow_mean(image, base):
    select = _luma(base) < 0.25
    return float(_luma(image)[select].mean())


def white_point(image, base):
    return float(np.percentile(_luma(image), 99.5))


def black_point(image, base):
    return float(np.percentile(_luma(image), 0.5))


def chroma_mean(image, base):
    return float(_chroma(image).mean())


def warm_balance(image, base):
    """Positive means the image moved toward amber and away from blue."""
    return float(image[..., 0].mean() - image[..., 2].mean())


def green_magenta(image, base):
    """Positive means the image moved toward green and away from magenta."""
    return float(image[..., 1].mean() - (image[..., 0].mean() + image[..., 2].mean()) / 2)


def fine_detail(image, base):
    """One-pixel local contrast: what Texture and fine sharpening move."""
    return float(np.abs(image - _blur(image, 1)).mean())


def coarse_detail(image, base):
    """Wider local contrast, the scale Clarity works at."""
    return float(np.abs(image - _blur(image, 4)).mean())


def edge_energy(image, base):
    grey = _luma(image)
    return float((np.abs(np.diff(grey, axis=0)).mean()
                  + np.abs(np.diff(grey, axis=1)).mean()) / 2)


def noisy_flat_variance(image, base):
    return float(_luma(image[FLAT_NOISY]).var())


def noisy_flat_chroma_variance(image, base):
    return float(_chroma(image[FLAT_NOISY]).var())


def clean_flat_deviation(image, base):
    """How far the untouched flat field drifted. Should stay at zero."""
    return float(np.abs(image[FLAT_CLEAN] - base[FLAT_CLEAN]).max())


def corner_brightness(image, base):
    """Corner luma relative to the centre, so exposure changes cancel out."""
    corners = np.mean([_luma(image[rows, columns]).mean()
                       for rows, columns in CORNERS])
    return float(corners / max(_luma(image[CENTRE]).mean(), 1e-6))


def vignette_hardness(image, base):
    """Steepest radial step in the corner falloff; a soft vignette is flat."""
    ratio = _luma(image) / np.maximum(_luma(base), 1e-6)
    return float(np.abs(np.diff(ratio, axis=1)).max())


def clear_area(image, base):
    """Fraction of the frame the vignette leaves alone.

    This is what "size of the clear central area" actually promises. A
    corner-to-centre ratio cannot measure it, because a small vignette
    darkens the centre too and moves both sides of the ratio at once.
    """
    ratio = _luma(image) / np.maximum(_luma(base), 1e-6)
    return float((ratio > 0.98).mean())


def channel_displacement(channel):
    """Sub-pixel shift of one channel against green at the contrast edge.

    First-order estimate: if C(x) = G(x - d) then C - G = -d * dG/dx, so
    d = -sum((C - G) * gx) / sum(gx * gx). Signed, so a red fringe and a cyan
    one read as opposite numbers instead of the same magnitude.
    """
    index = {"red": 0, "blue": 2}[channel]

    def metric(image, base):
        region = image[EDGE]
        green = region[..., 1]
        gradient = np.gradient(green, axis=1)
        # The channel difference itself, sampled where the gradient is, not
        # its derivative: differentiating it again throws the sign away and
        # leaves a magnitude that rises for either direction of fringing.
        difference = region[..., index] - green
        weight = float((gradient * gradient).sum())
        if weight < 1e-9:
            return 0.0
        return float(-(difference * gradient).sum() / weight)

    metric.__name__ = f"{channel}_displacement"
    return metric


def affected_area(image, base):
    """Fraction of the frame an edit actually touched."""
    return float((np.abs(image - base).max(axis=-1) > 2 / 255).mean())


def affected_strength(image, base):
    """Mean change over the pixels the edit touched, ignoring its extent."""
    difference = np.abs(image - base).max(axis=-1)
    touched = difference > 2 / 255
    return float(difference[touched].mean()) if touched.any() else 0.0


def horizontal_centroid(image, base):
    """Where the image's weight sits left-to-right, for geometry controls."""
    weight = _luma(image)
    columns = np.arange(image.shape[1])
    total = weight.sum()
    return float((weight * columns).sum() / total / image.shape[1]) if total else 0.5


def vertical_centroid(image, base):
    weight = _luma(image)
    rows = np.arange(image.shape[0])[:, None]
    total = weight.sum()
    return float((weight * rows).sum() / total / image.shape[0]) if total else 0.5


def keystone(axis):
    """Trapezoid asymmetry: how much wider one end of the frame is.

    Perspective correction scales one axis by position along the other, so
    the frame becomes a trapezoid. A centroid cannot see that; the difference
    in filled width between the two ends can.
    """
    def metric(image, base):
        filled = _luma(image) > 1e-4
        eighth = max(image.shape[0] // 8, 1), max(image.shape[1] // 8, 1)
        if axis == "vertical":
            return float(filled[:eighth[0]].mean() - filled[-eighth[0]:].mean())
        return float(filled[:, :eighth[1]].mean() - filled[:, -eighth[1]:].mean())

    metric.__name__ = f"{axis}_keystone"
    return metric


def frame_coverage(image, base):
    """Fraction of the frame still carrying original content, not fill.

    Rotation, distortion and perspective push content out of frame and pull
    empty area in; scale does the reverse.
    """
    return float((_luma(image) > 1e-4).mean())


def divergence(image, base):
    """Distance from the untouched render. The universal fallback metric."""
    return float(np.abs(image - base).mean())


def band_chroma(band):
    def metric(image, base):
        select = _band_select(base, band)
        return float(_chroma(image)[select].mean())
    return metric


def band_luma(band):
    def metric(image, base):
        select = _band_select(base, band)
        return float(_luma(image)[select].mean())
    return metric


def band_hue(band):
    def metric(image, base):
        select = _band_select(base, band)
        centre = BAND_HUES[band]
        hue = _hue(image)[select]
        # Unwrap around the band centre so a shift reads as a signed move.
        return float(((hue - centre + 180) % 360 - 180).mean())
    return metric


def _hue(image):
    high = image.max(axis=-1)
    low = image.min(axis=-1)
    span = np.maximum(high - low, 1e-6)
    red, green, blue = image[..., 0], image[..., 1], image[..., 2]
    hue = np.where(high == red, ((green - blue) / span) % 6,
                   np.where(high == green, (blue - red) / span + 2,
                            (red - green) / span + 4)) * 60.0
    return np.where(high - low > 1e-5, hue % 360.0, 0.0)


def _band_select(base, band):
    centre = BAND_HUES[band]
    distance = np.abs((_hue(base) - centre + 180) % 360 - 180)
    return (distance < 12) & (_chroma(base) > 0.3)


# ------------------------------------------------------------------ claims

# key -> (metric, direction, sentence the control promises)
# direction is +1 when the metric must rise with the control, -1 when it falls.
CLAIMS: dict[str, tuple] = {
    "exposure": (mean_luma, +1, "raises overall brightness"),
    "contrast": (contrast, +1, "widens the spread between light and dark"),
    "highlights": (highlight_mean, +1, "lifts the already-bright tones"),
    "shadows": (shadow_mean, +1, "lifts the already-dark tones"),
    "whites": (white_point, +1, "moves the white point"),
    "blacks": (black_point, +1, "moves the black point"),
    "temp": (warm_balance, +1, "warms toward amber, away from blue"),
    "tint": (green_magenta, -1, "moves toward magenta, away from green"),
    "vibrance": (chroma_mean, +1, "increases colourfulness"),
    "saturation": (chroma_mean, +1, "increases colourfulness"),
    "texture": (fine_detail, +1, "increases one-pixel local detail"),
    "clarity": (coarse_detail, +1, "increases midtone local contrast"),
    "dehaze": (contrast, +1, "cuts haze by expanding contrast"),
    "vignette": (corner_brightness, -1, "darkens the corners"),
    "vignetteSize": (clear_area, +1, "enlarges the clear central area"),
    "vignetteFeather": (vignette_hardness, -1, "softens the vignette transition"),
    "sharpness": (edge_energy, +1, "sharpens edges"),
    "sharpenDetail": (fine_detail, +1, "emphasises fine texture"),
    "sharpenMasking": (fine_detail, -1, "confines sharpening to stronger edges"),
    "luminanceNoise": (noisy_flat_variance, -1, "reduces luminance noise"),
    "colorNoise": (noisy_flat_chroma_variance, -1, "reduces colour noise"),
    "monochrome": (chroma_mean, -1, "removes colour"),
    # Positive values sample the channel from further out radially, so its
    # content moves inward; the audit edge sits right of centre, hence -1.
    "chromaticAberrationRedCyan": (channel_displacement("red"), -1,
                                   "slides red against cyan to close a fringe"),
    "chromaticAberrationBlueYellow": (channel_displacement("blue"), -1,
                                      "slides blue against yellow to close a fringe"),
}
# A few controls only make sense against a companion setting.
COMPANION: dict[str, dict] = {
    "vignetteSize": {"vignette": 0.8},
    "vignetteFeather": {"vignette": 0.8},
    "sharpenDetail": {"sharpness": 0.8},
    "sharpenMasking": {"sharpness": 0.8},
    "sharpenRadius": {"sharpness": 0.8},
}
# Point Color and Colour Grading entries need an effect to modulate before a
# selection or strength control has anything to change.
# The uniformity controls are skipped entirely unless the entry carries a
# reference colour, which is how the feature works: they pull the selected
# hue toward that reference.
POINT_COLOR_COMPANION = {"hue": 30.0, "range": 40.0, "hueShift": 25.0,
                         "saturation": 0.5, "luminance": 0.2,
                         "uniformHue": 0.5, "uniformSaturation": 0.5,
                         "uniformLuminance": 0.5,
                         "refSaturation": 0.9, "refLuminance": 0.8}
COLOR_GRADING_COMPANION = {"saturation": 0.6, "hue": 40.0}
# Controls the renderer never sees directly: the editor samples them into the
# 256-entry tone curve that actually travels to every engine. Their effect is
# real but belongs to the client, so a server-side sweep correctly does
# nothing and the browser gate has to own them.
COVERED_ELSEWHERE = {
    "film.camera_profile":
        "needs a .dcp file resolved from the user's profile folder; covered by "
        "tests/test_camera_profile_develop.py",
    "optics.profileEnabled":
        "needs a matched lens profile; covered by tests/test_lens_matching.py",
    "optics.profileVignette":
        "needs a matched lens profile; covered by tests/test_lens_matching.py",
    "optics.profileDistortion":
        "needs a matched lens profile; covered by tests/test_lens_matching.py",
    "optics.profileOverride":
        "needs a matched lens profile; covered by tests/test_lens_matching.py",
}

CLIENT_DERIVED = {
    f"parametricCurve.{key}":
        "sampled into curveL by the editor; covered by the browser curve gate"
    for key in ("highlights", "lights", "darks", "shadows",
                "splitSD", "splitDL", "splitLH")
}

# Verified defects, kept visible instead of blessed. Each entry states what
# the control does today and why that is wrong; removing the inversion should
# make the matching claim pass and this entry must then be deleted.
TRACKED_DEFECTS = {
    "local.whites":
        "Same inverted endpoint as grade.whites, reached through a mask.",
    "grade.whites":
        "Whites is inverted: the endpoint is 1 + whites * 0.35, so a positive "
        "value lowers the white point and darkens the highlights. Blacks, in "
        "the same block, lifts with a positive value, and every comparable "
        "editor brightens on +Whites. Python, the WebGL shader and the Rust "
        "engine all agree, so no parity gate can see it.",
}

# Controls whose claim is inherently non-monotonic, with the reason.
WRAPPING = {
    "pointColor.hue": "selects a hue to act on; wraps at 360 degrees",
    "colorGrading.shadows.hue": "chooses a tint angle; wraps at 360 degrees",
    "colorGrading.midtones.hue": "chooses a tint angle; wraps at 360 degrees",
    "colorGrading.highlights.hue": "chooses a tint angle; wraps at 360 degrees",
    "colorGrading.global.hue": "chooses a tint angle; wraps at 360 degrees",
}


def _hsl_claims():
    for band in BAND_HUES:
        yield f"hsl.{band}.h", (band_hue(band), +1, f"shifts {band} hues")
        yield f"hsl.{band}.s", (band_chroma(band), +1, f"saturates {band}")
        yield f"hsl.{band}.l", (band_luma(band), +1, f"brightens {band}")


CLAIMS.update(dict(_hsl_claims()))

# Local controls reuse the global claim of the same name, measured through a
# mask that covers the whole frame, so the two must agree.
CLAIMS.update({f"local.{key}": CLAIMS[key] for key in
               ("exposure", "contrast", "highlights", "shadows", "whites",
                "blacks", "temp", "tint", "saturation", "texture", "clarity")
               if key in CLAIMS})

CLAIMS.update({
    "optics.scale": (frame_coverage, +1, "enlarges the image inside the frame"),
    # Lens *correction*, so a positive value lifts the corners. This is the
    # opposite sign from the creative grade.vignette, which darkens them.
    "optics.vignette": (corner_brightness, +1, "corrects lens falloff in the corners"),
    "optics.horizontal": (keystone("horizontal"), +1,
                          "corrects horizontal converging lines"),
    "optics.vertical": (keystone("vertical"), +1,
                        "corrects vertical converging lines"),
    "mask.opacity": (affected_strength, +1, "strengthens the masked edit"),
    "mask.radial.radius": (affected_area, +1, "grows the selected area"),
    "mask.radial.radiusX": (affected_area, +1, "widens the selected ellipse"),
    "mask.radial.radiusY": (affected_area, +1, "heightens the selected ellipse"),
    "mask.brush.size": (affected_area, +1, "widens the brush"),
    "mask.brush.flow": (affected_strength, +1, "lays down more per stroke"),
    "mask.brush.density": (affected_strength, +1, "builds up to a higher limit"),
    "heal.radius": (affected_area, +1, "treats a larger spot"),
    "heal.opacity": (affected_strength, +1, "blends the repair in more strongly"),
})

# Sign conventions verified against the implementation, recorded so a future
# reader does not have to rediscover them.
WRAPPING.update({
    "mask.radial.angle": "rotates the ellipse; symmetric about zero",
    "optics.rotate": "rotates the frame; symmetric about zero",
    "optics.distortion": "barrel one way, pincushion the other",
})


# ------------------------------------------------------------------ runner


@dataclass
class Finding:
    key: str
    verdict: str
    detail: str
    claim: str = ""
    metric: str = ""
    readings: list = field(default_factory=list)
    values: list = field(default_factory=list)

    @property
    def surface_order(self) -> int:
        """Group the report the way the panels are laid out."""
        order = ("grade", "hsl", "pointColor", "colorGrading", "parametricCurve",
                 "local", "mask", "heal", "optics", "film")
        head = self.key.split(".", 1)[0]
        return order.index(head) if head in order else len(order)

    @property
    def defect(self) -> bool:
        """A verdict the gate fails on. Tracked defects are already known."""
        return self.verdict in ("inert", "leaky", "contradicted")

    @property
    def known(self) -> bool:
        return self.verdict == "tracked-defect"


OMIT = object()


def _without(companion: dict, key: str) -> dict:
    """A companion setting must never supply the control under audit.

    Otherwise omitting the control still leaves the companion's value in
    place, the schema default replaces it, and an honest no-op default reads
    as a leak.
    """
    return {name: value for name, value in companion.items() if name != key}


def _apply(control: Control, value) -> np.ndarray:
    """Render the audit target with exactly one control set.

    Passing ``OMIT`` leaves the control out entirely while keeping any
    companion settings, which gives the identity check the right baseline: a
    strength control is only "leaky" if it differs from *not being sent*, not
    if the companion it modulates is visible.
    """
    base = audit_target()
    surface, _, name = control.key.partition(".")
    sent = {} if value is OMIT else {name: value}
    if surface == "grade":
        return grade.apply(base, {**_without(COMPANION.get(name, {}), name), **sent})
    if surface == "hsl":
        band, axis = name.split(".")
        block = {} if value is OMIT else {axis: value}
        return grade.apply(base, {"hsl": {band: {"s": 0.0, **block}}})
    if surface == "pointColor":
        entry = {**_without(POINT_COLOR_COMPANION, name),
                 **({} if value is OMIT else {name: value})}
        return grade.apply(base, {"pointColor": [entry]})
    if surface == "colorGrading":
        parts = name.split(".")
        if len(parts) == 2:
            tone = {**_without(COLOR_GRADING_COMPANION, parts[1]),
                    **({} if value is OMIT else {parts[1]: value})}
            block = {parts[0]: tone}
        else:
            block = {"shadows": dict(COLOR_GRADING_COMPANION),
                     "highlights": {**COLOR_GRADING_COMPANION, "hue": 200.0},
                     **({} if value is OMIT else {parts[0]: value})}
        return grade.apply(base, {"colorGrading": block})
    if surface == "parametricCurve":
        return grade.apply(base, {"parametricCurve": sent})
    if surface == "local":
        return _apply_local(base, name, value)
    if surface == "optics":
        return _apply_optics(base, name, value)
    if surface in ("mask", "heal"):
        return _apply_edit(base, control.key, value)
    raise NotImplementedError(f"{control.key} has no audit renderer")


def _full_mask(values):
    import base64
    return {"type": "subject", "opacity": 1.0, "grade": values,
            "bitmap": {"width": 1, "height": 1,
                       "data": base64.b64encode(b"\xff").decode()}}


def _apply_local(base, name, value):
    sent = {} if value is OMIT else {name: value}
    grade_values = {**_without(COMPANION.get(name, {}), name), **sent}
    return edits.apply_masks(base, [_full_mask(grade_values)])


def _apply_optics(base, name, value):
    sent = {} if value is OMIT else {name: value}
    return edits.apply_manual_optics(base, edits.clean_optics(sent))


# A mask that selects the left half, so a geometry or strength control has a
# visible boundary to move and an untouched half to compare against.
def _shape_mask(component, grade_values=None):
    return {"type": component.pop("type", "radial"), "opacity": 1.0,
            "grade": grade_values or {"exposure": 1.0}, **component}


def _apply_mask(base, name, value):
    # The inventory key is dotted ("radial.radius"); the mask schema field is
    # only the last segment.
    field = name.rsplit(".", 1)[-1]
    sent = {} if value is OMIT else {field: value}
    if name == "opacity":
        return edits.apply_masks(base, [_shape_mask(
            {"type": "radial", "center": [0.35, 0.5], "radius": 0.3}) | sent])
    family, _, field = name.partition(".")
    if family == "radial":
        # The explicit axes override the circular radius, so auditing radius
        # has to leave them out. Everything else wants a deliberately
        # eccentric ellipse: a circle would make the angle control genuinely
        # inert and the audit would blame the code for the fixture.
        shape = {"center": [0.35, 0.5], "radius": 0.3, "feather": 0.4}
        if field != "radius":
            shape |= {"radiusX": 0.4, "radiusY": 0.15, "angle": 20.0}
        shape = _without(shape, field)
        return edits.apply_masks(base, [_shape_mask(
            {"type": "radial", **shape, **sent})])
    if family == "brush":
        stroke = {**_without({"size": 0.2, "feather": 0.5, "flow": 1.0,
                              "buildUp": True, "density": 1.0}, field),
                  "points": [[0.25, 0.4], [0.6, 0.6]], **sent}
        return edits.apply_masks(base, [_shape_mask(
            {"type": "brush", "strokes": [stroke]})])
    if family == "depth":
        return edits.apply_masks(base, [_shape_mask(
            {"type": "depth", "bitmap": _ramp_bitmap(), **sent})])
    if name == "invert":
        return edits.apply_masks(base, [_shape_mask(
            {"type": "radial", "center": [0.35, 0.5], "radius": 0.3,
             "invert": bool(value) if value is not OMIT else False})])
    if name == "enabled":
        return edits.apply_masks(base, [_shape_mask(
            {"type": "radial", "center": [0.35, 0.5], "radius": 0.3,
             "enabled": bool(value) if value is not OMIT else True})])
    raise NotImplementedError(f"mask.{name}")


def _apply_heal(base, name, value):
    sent = {} if value is OMIT else {name: value}
    companion = _without({"mode": "clone", "target": [0.35, 0.55],
                          "source": [0.7, 0.55], "radius": 0.12,
                          "feather": 0.4, "opacity": 1.0}, name)
    return edits.apply_heals(base, [{**companion, **sent}])


def _ramp_bitmap():
    """An eight-bit depth ramp, so a depth threshold has a gradient to cut."""
    import base64
    ramp = (np.linspace(0, 255, 64).astype(np.uint8)[None, :]
            .repeat(64, axis=0).tobytes())
    return {"width": 64, "height": 64, "data": base64.b64encode(ramp).decode()}


def _apply_edit(base, key, value):
    surface, _, name = key.partition(".")
    if surface == "mask":
        return _apply_mask(base, name, value)
    return _apply_heal(base, name, value)


# Half the frame, as an AI-provided selection bitmap.
def _half_bitmap(left=True):
    import base64
    bitmap = np.zeros((32, 32), dtype=np.uint8)
    bitmap[:, :16 if left else 16:] = 255
    return {"width": 32, "height": 32,
            "data": base64.b64encode(bitmap.tobytes()).decode()}


BITMAP_TYPES = {"subject", "sky", "object", "depth", "person", "face-skin",
                "eyes", "eyebrows", "lips", "teeth", "hair"}


def _typed_mask(kind, **extra):
    """A minimal working selection of the given type; ``extra`` wins."""
    if kind in BITMAP_TYPES:
        shape = {"bitmap": _half_bitmap()}
    elif kind == "linear":
        shape = {"start": [0.3, 0.5], "end": [0.7, 0.5]}
    elif kind == "brush":
        shape = {"strokes": [{"points": [[0.3, 0.4], [0.6, 0.6]],
                              "size": 0.2, "feather": 0.5, "flow": 1.0}]}
    else:
        shape = {"center": [0.35, 0.5], "radius": 0.3, "feather": 0.4}
    return {"type": kind, **shape, **extra}


def audit_mask_type(kind: str) -> Finding:
    """A selection type must select something, and not the whole frame."""
    base = audit_target()
    rendered = edits.apply_masks(base, [{"opacity": 1.0, "grade": {"exposure": 1.0},
                                         **_typed_mask(kind)}])
    area = affected_area(rendered, base)
    if area <= 0.001:
        return Finding(f"mask.type.{kind}", "inert",
                       "the selection changed no pixels")
    if area >= 0.999:
        return Finding(f"mask.type.{kind}", "contradicted",
                       "the selection covered the entire frame, so it is not "
                       "selecting anything", metric="affected_area",
                       readings=[area])
    return Finding(f"mask.type.{kind}", "directional",
                   f"selects {area:.0%} of the frame",
                   "restricts the edit to a region", "affected_area", [area])


def audit_mask_combine(mode: str) -> Finding:
    """add grows the selection, subtract shrinks it, intersect keeps overlap."""
    base = audit_target()

    def area(components):
        rendered = edits.apply_masks(base, [{"opacity": 1.0,
                                             "grade": {"exposure": 1.0},
                                             "components": components}])
        return affected_area(rendered, base)

    # Two shapes with a substantial but partial overlap, so union,
    # difference and intersection are each clearly distinguishable from
    # either shape alone. Nearly disjoint shapes would make subtract a no-op
    # and the audit would blame the code for the fixture.
    first = _typed_mask("radial", center=[0.42, 0.5], radius=0.28)
    second = _typed_mask("radial", center=[0.58, 0.5], radius=0.28)
    alone = area([first])
    combined = area([first, {**second, "combine": mode}])
    expected = {"add": lambda: combined > alone + 0.01,
                "subtract": lambda: combined < alone - 0.01,
                "intersect": lambda: combined < alone - 0.01}[mode]
    detail = f"one shape covers {alone:.0%}, with a second {mode} {combined:.0%}"
    if expected():
        return Finding(f"mask.combine.{mode}", "directional", detail,
                       f"{mode} composes the two selections",
                       "affected_area", [alone, combined])
    return Finding(f"mask.combine.{mode}", "contradicted", detail,
                   f"{mode} composes the two selections",
                   "affected_area", [alone, combined])


def _sweep_values(control: Control) -> list:
    if control.kind == "boolean":
        return [False, True]
    return control.sweep()


def _monotone(readings, direction, tolerance=0.02):
    """Every step moves the metric the promised way.

    A step may drift the wrong way by up to ``tolerance`` of the metric's own
    total travel. That absorbs floating-point wobble and the flattening at the
    end of a control's range without hiding a genuine reversal, which is
    always a large fraction of the travel.
    """
    steps = [b - a for a, b in zip(readings, readings[1:])]
    span = max(readings) - min(readings)
    if span < 1e-9 or all(abs(step) < 1e-12 for step in steps):
        return False
    slack = span * tolerance
    return all(step * direction >= -slack for step in steps)


def audit(control: Control) -> Finding:
    base = audit_target()
    surface, _, name = control.key.partition(".")
    claim_key = name if surface in ("grade", "local") else control.key
    claim = CLAIMS.get(claim_key)

    try:
        without = _apply(control, OMIT)
        at_default = _apply(control, control.default)
    except NotImplementedError as error:
        return Finding(control.key, "unaudited", str(error))
    if control.key in CLIENT_DERIVED:
        return Finding(control.key, "client-derived", CLIENT_DERIVED[control.key])
    if control.key in COVERED_ELSEWHERE:
        return Finding(control.key, "covered-elsewhere", COVERED_ELSEWHERE[control.key])
    leak = float(np.abs(at_default - without).max())
    # An optional field has no default value to be a no-op at: leaving it out
    # is its resting state, and supplying any value switches the feature on.
    if leak > 1e-6 and not control.optional:
        return Finding(control.key, "leaky",
                       f"default value {control.default} changes the image "
                       f"by up to {leak:.4f} against the same edit without it")

    values = _sweep_values(control)
    renders = [_apply(control, value) for value in values]
    spread = max(float(np.abs(render - without).max()) for render in renders)
    if spread < 1e-6:
        return Finding(control.key, "inert",
                       f"no pixel changed anywhere across {values}",
                       values=values)

    if claim:
        metric, direction, sentence = claim
        readings = [metric(render, base) for render in renders]
        if _monotone(readings, direction):
            return Finding(control.key, "directional", "claim holds across the sweep",
                           sentence, metric.__name__, readings, values)
        verdict = "tracked-defect" if control.key in TRACKED_DEFECTS else "contradicted"
        detail = TRACKED_DEFECTS.get(
            control.key,
            f"{metric.__name__} did not move "
            f"{'up' if direction > 0 else 'down'} across the sweep")
        return Finding(control.key, verdict, detail,
                       sentence, metric.__name__, readings, values)

    if control.key in WRAPPING:
        return Finding(control.key, "responsive", WRAPPING[control.key],
                       metric="divergence",
                       readings=[divergence(r, without) for r in renders], values=values)

    # No named claim: require the image to diverge further as the control
    # moves further from its default.
    anchor = float(control.default if control.default is not None else values[0])
    order = sorted(range(len(values)), key=lambda index: abs(float(values[index]) - anchor))
    readings = [divergence(renders[index], without) for index in order]
    verdict = "monotonic" if _monotone(readings, +1) else "responsive"
    return Finding(control.key, verdict,
                   "divergence from default grows with the control"
                   if verdict == "monotonic" else
                   "changes the image without a monotonic reading",
                   metric="divergence", readings=readings,
                   values=[values[index] for index in order])


ALL_SURFACES = ("grade", "hsl", "pointColor", "colorGrading",
                "parametricCurve", "local", "optics", "mask", "heal")


def auditable(surfaces=ALL_SURFACES) -> list[Control]:
    return [control for control in controls()
            if control.surface in surfaces and control.kind in ("number", "boolean")]


def run(surfaces=None) -> list[Finding]:
    chosen = auditable() if surfaces is None else auditable(surfaces)
    findings = [audit(control) for control in chosen]
    known = {control.key for control in controls()}
    findings += [audit_mask_type(key.rsplit(".", 1)[1]) for key in sorted(known)
                 if key.startswith("mask.type.")]
    findings += [audit_mask_combine(key.rsplit(".", 1)[1]) for key in sorted(known)
                 if key.startswith("mask.combine.")]
    return findings


if __name__ == "__main__":  # pragma: no cover - operator convenience
    findings = run()
    width = max(len(finding.key) for finding in findings)
    for finding in sorted(findings, key=lambda f: (not f.defect, f.verdict, f.key)):
        marker = "FAIL" if finding.defect else "ok  "
        print(f"{marker} {finding.key:{width}} {finding.verdict:14} {finding.detail}")
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.verdict] = counts.get(finding.verdict, 0) + 1
    print("\n" + ", ".join(f"{count} {verdict}" for verdict, count in sorted(counts.items())))
