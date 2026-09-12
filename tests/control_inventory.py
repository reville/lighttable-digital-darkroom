# SPDX-License-Identifier: GPL-3.0-only
"""Derived inventory of every user-facing processing control.

Nothing here restates a limit that the application already declares. Numeric
bounds are *probed* from the production cleaning functions, enumerated choices
are read out of the validation source, and slider bounds are parsed from the
shipped markup. A changed clamp therefore updates the inventory instead of
silently disagreeing with it, and a control added to the schema or the UI
appears here without anyone remembering to add it.

The inventory is the shared vocabulary for two gates:

* `test_control_coverage.py` asserts every control is exercised somewhere.
* `test_control_semantics.py` asserts each control does what its name says.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache
import inspect
from pathlib import Path
import re
import sys

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import edits  # noqa: E402
import film_pipeline as fp  # noqa: E402
import grade  # noqa: E402

# A value far outside every plausible range. Probing with it returns whatever
# the production cleaner considers the extreme of the control.
BIG = 1e9

# Controls that exist in the markup but are not part of image processing, so a
# pixel gate cannot own them. Each needs a reason; an unexplained entry fails.
NON_PROCESSING_UI = {
    "cacheBudgetGB": "render cache size preference",
    "exQuality": "JPEG encoder quality, checked by the export suite",
    "modalExQuality": "JPEG encoder quality in the modal export dialog",
    "gridSize": "library thumbnail size",
    "lightsOutDim": "viewer background dimming",
    "uiFontScale": "interface font scale",
    "referenceOpacity": "reference-image overlay display",
    "referenceScale": "reference-image overlay display",
    "referenceX": "reference-image overlay display",
    "referenceY": "reference-image overlay display",
    "healVisualizeThreshold": "retouch visualisation overlay",
    "maskBrushTolerance": "Auto Mask edge detection, checked by mask tests",
    "maskColorHue": "colour-range mask picker input",
    "maskColorRange": "colour-range mask picker input",
    "maskColorAmount": "colour-range mask picker input",
    "maskLumaLow": "luminance-range mask picker input",
    "maskLumaHigh": "luminance-range mask picker input",
}

# Markup that edits the selected tone or hue band rather than one fixed
# control. The concrete keys are generated and audited by the colour-grading
# and HSL suites; a new slider anywhere else must still be classified.
NON_PROCESSING_TEMPLATE_SLIDERS = {
    "colorGrading.hue", "colorGrading.saturation", "colorGrading.luminance",
    "hsl.*.h", "hsl.*.s", "hsl.*.l",
}


@dataclass(frozen=True)
class Slider:
    """A range input as the shipped markup declares it."""

    element: str
    low: float
    high: float
    step: float


@dataclass(frozen=True)
class Control:
    """One controllable quantity, with the bounds the application enforces."""

    key: str
    surface: str
    kind: str
    default: object = None
    low: float | None = None
    high: float | None = None
    choices: tuple | None = None
    slider: Slider | None = None
    cyclic: bool = False
    optional: bool = False

    @property
    def numeric(self) -> bool:
        return self.kind == "number"

    def sweep(self, count: int = 7) -> list[float]:
        """Values spanning the control's full travel, including its default."""
        if not self.numeric or self.low is None or self.high is None:
            raise TypeError(f"{self.key} is not a numeric control")
        span = self.high - self.low
        # A cyclic control's two endpoints are the same position, so the last
        # sample would repeat the first and break a monotonic reading.
        divisor = count if self.cyclic else count - 1
        values = [self.low + span * index / divisor for index in range(count)]
        return sorted(set(round(value, 6) for value in values))


def _probe(clean, key, template=None):
    """Return (low, high, cyclic, optional) as the production cleaner sees them.

    A wrapping control such as a hue angle has no clamp to find: probing it
    with an extreme returns that extreme modulo its period. Detect the wrap
    and report the period instead of the meaningless residue.
    """
    def value(sent):
        return float(clean({**(template or {}), key: sent})[key])

    # A field the cleaner drops when it is not supplied has no default value
    # to be a no-op at; "absent" is its resting state.
    try:
        optional = key not in clean(dict(template or {}))
    except (KeyError, IndexError, TypeError):
        optional = False

    # Wrapping shows up as a sample that survives untouched *and* returns to
    # itself one period later. A clamped control fails the second half, so it
    # is not mistaken for a cyclic one.
    inside = 10.0
    for period in (360.0,):
        if (abs(value(inside) - inside) < 1e-6
                and abs(value(inside + period) - inside) < 1e-6):
            return 0.0, period, True, optional
    return value(-BIG), value(BIG), False, optional


def _grade_controls() -> list[Control]:
    controls = []
    for key, default in grade.DEFAULTS.items():
        low, high, cyclic, optional = _probe(grade.clean, key)
        controls.append(Control(f"grade.{key}", "grade", "number",
                                default, low, high, cyclic=cyclic))
    for key in grade.CURVE_KEYS:
        controls.append(Control(f"grade.{key}", "grade", "curve", None))
    for band in grade.HSL_BANDS:
        for axis in ("h", "s", "l"):
            def clean_band(values, _band=band):
                return grade.clean({"hsl": {_band: values}})["hsl"][_band]
            low, high, cyclic, optional = _probe(clean_band, axis)
            controls.append(Control(f"hsl.{band}.{axis}", "hsl", "number",
                                    0.0, low, high, cyclic=cyclic))
    point_defaults = {"hue": 0.0, "range": 30.0, "hueShift": 0.0,
                      "saturation": 0.0, "luminance": 0.0, "uniformHue": 0.0,
                      "uniformSaturation": 0.0, "uniformLuminance": 0.0,
                      "refSaturation": 0.5, "refLuminance": 0.5}
    # Fields the cleaner only keeps when they are supplied: their resting
    # state is absence, not a value, so they carry no default.
    minimal = grade.clean({"pointColor": [{"hue": 30.0, "saturation": 0.5}]})
    supplied_only = {key for key in point_defaults
                     if key not in minimal["pointColor"][0]}
    for key, default in point_defaults.items():
        def clean_point(values, _key=key):
            entry = grade.clean({"pointColor": [{**point_defaults, **values}]})
            return entry["pointColor"][0]
        low, high, cyclic, _ = _probe(clean_point, key)
        optional = key in supplied_only
        controls.append(Control(f"pointColor.{key}", "pointColor", "number",
                                None if optional else default, low, high,
                                cyclic=cyclic, optional=optional))
    def keeper(tone):
        """A second tone carrying saturation, so the block survives cleaning.

        An all-zero colourGrading block is dropped as inactive, which would
        otherwise make probing a tone's own saturation impossible.
        """
        other = "shadows" if tone != "shadows" else "midtones"
        return {other: {"saturation": 0.5}}

    for tone in grade.COLOR_GRADING_TONES:
        for axis, default in (("hue", 0.0), ("saturation", 0.0), ("luminance", 0.0)):
            def clean_tone(values, _tone=tone):
                result = grade.clean({"colorGrading": {**keeper(_tone),
                                                       _tone: values}})
                return result["colorGrading"][_tone]
            low, high, cyclic, optional = _probe(clean_tone, axis)
            controls.append(Control(f"colorGrading.{tone}.{axis}", "colorGrading",
                                    "number", default, low, high, cyclic=cyclic))
    for key, default in (("balance", 0.0), ("blending", 0.5)):
        def clean_master(values):
            return grade.clean({"colorGrading": {**keeper("global"),
                                                 **values}})["colorGrading"]
        low, high, cyclic, optional = _probe(clean_master, key)
        controls.append(Control(f"colorGrading.{key}", "colorGrading", "number",
                                default, low, high, cyclic=cyclic))
    parametric = {"highlights": 0.0, "lights": 0.0, "darks": 0.0,
                  "shadows": 0.0, "splitSD": 0.25, "splitDL": 0.5, "splitLH": 0.75}
    for key, default in parametric.items():
        def clean_parametric(values, _key=key):
            return grade.clean({"parametricCurve": values})["parametricCurve"]
        low, high, cyclic, optional = _probe(clean_parametric, key)
        controls.append(Control(f"parametricCurve.{key}", "parametricCurve",
                                "number", default, low, high, cyclic=cyclic))
    return controls


def _local_controls() -> list[Control]:
    controls = []
    for key in edits.LOCAL_GRADE_KEYS:
        low, high, cyclic, optional = _probe(grade.clean, key)
        controls.append(Control(f"local.{key}", "local", "number",
                                grade.DEFAULTS[key], low, high, cyclic=cyclic))
    for key in grade.CURVE_KEYS:
        controls.append(Control(f"local.{key}", "local", "curve", None))
    return controls


def _optics_controls() -> list[Control]:
    controls = []
    for key, default in edits.OPTICS_DEFAULTS.items():
        if isinstance(default, bool):
            controls.append(Control(f"optics.{key}", "optics", "boolean", default))
        elif isinstance(default, (int, float)):
            low, high, cyclic, optional = _probe(edits.clean_optics, key)
            controls.append(Control(f"optics.{key}", "optics", "number",
                                    float(default), low, high, cyclic=cyclic))
        else:
            controls.append(Control(f"optics.{key}", "optics", "structure", default))
    return controls


def _accepted(function, key: str) -> tuple:
    """The literal set a cleaner tests one field against, read from its source."""
    for node in ast.walk(ast.parse(inspect.getsource(function))):
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.ops[0], ast.In)):
            continue
        left = node.left
        if not (isinstance(left, ast.Call) and isinstance(left.func, ast.Attribute)
                and left.func.attr == "get" and left.args
                and getattr(left.args[0], "value", None) == key):
            continue
        return tuple(sorted(ast.literal_eval(node.comparators[0])))
    raise AssertionError(f"{function.__name__} no longer validates {key!r}")


def _mask_controls() -> list[Control]:
    """Mask component types and the geometry each type exposes."""
    kinds = _accepted(edits._clean_mask_component, "type")
    controls = [Control(f"mask.type.{kind}", "mask", "structure", None)
                for kind in kinds]
    for combine in _accepted(edits._clean_mask_component, "combine"):
        controls.append(Control(f"mask.combine.{combine}", "mask", "structure", None))
    controls.append(Control("mask.invert", "mask", "boolean", False))
    controls.append(Control("mask.enabled", "mask", "boolean", True))
    controls.append(Control("mask.opacity", "mask", "number", 1.0, 0.0, 1.0))

    def clean_component(values, kind="radial"):
        return edits.clean_masks([{"type": kind, **values}])[0]["components"][0]

    for key, default in (("radius", 0.25), ("radiusX", 0.25), ("radiusY", 0.25),
                         ("angle", 0.0), ("feather", 0.65)):
        low, high, cyclic, _ = _probe(clean_component, key)
        # A field whose resting value follows another field has no fixed
        # default: the ellipse axes fall back to the circle's radius.
        derived = (clean_component({"radius": 0.2})[key]
                   != clean_component({"radius": 0.9})[key])
        controls.append(Control(f"mask.radial.{key}", "mask", "number",
                                None if derived else default, low, high,
                                cyclic=cyclic, optional=derived))
    solid = {"width": 1, "height": 1, "data": "/w=="}
    for key, default in (("depthLow", 0.55), ("depthHigh", 1.0)):
        def clean_depth(values, _key=key):
            # The pair is ordered after cleaning, so probe one bound with the
            # other pinned to the extreme that cannot swap them.
            partner = "depthHigh" if _key == "depthLow" else "depthLow"
            return clean_component({"bitmap": solid,
                                    partner: 1.0 if _key == "depthLow" else 0.0,
                                    **values}, kind="depth")
        low, high, cyclic, optional = _probe(clean_depth, key)
        controls.append(Control(f"mask.depth.{key}", "mask", "number",
                                default, low, high, cyclic=cyclic))

    def clean_stroke(values):
        return edits._clean_strokes([{"points": [[0.5, 0.5]], "buildUp": True,
                                      **values}])[0]

    for key, default in (("size", 0.08), ("feather", 0.65), ("flow", 1.0),
                         ("density", 1.0)):
        low, high, cyclic, optional = _probe(clean_stroke, key)
        controls.append(Control(f"mask.brush.{key}", "mask", "number",
                                default, low, high, cyclic=cyclic))
    controls.append(Control("mask.brush.buildUp", "mask", "boolean", False))
    controls.append(Control("mask.brush.edgeMask", "mask", "structure", None))
    controls.append(Control("mask.linear.start", "mask", "structure", None))
    controls.append(Control("mask.linear.end", "mask", "structure", None))
    return controls


def _heal_controls() -> list[Control]:
    modes = sorted(set(_accepted(edits.clean_heals, "mode")) | {"heal"})
    controls = [Control(f"heal.mode.{mode}", "heal", "structure", None)
                for mode in modes]

    def clean_heal(values):
        return edits.clean_heals([values])[0]

    for key, default in (("radius", 0.04), ("feather", 0.65), ("opacity", 1.0)):
        low, high, cyclic, optional = _probe(clean_heal, key)
        controls.append(Control(f"heal.{key}", "heal", "number", default,
                                low, high, cyclic=cyclic))
    controls.append(Control("heal.enabled", "heal", "boolean", True))
    controls.append(Control("heal.target", "heal", "structure", None))
    controls.append(Control("heal.source", "heal", "structure", None))
    return controls


@lru_cache(maxsize=1)
def _film_enum_choices() -> dict[str, tuple]:
    """Read the allowed strings out of clean_params rather than restating them."""
    tree = ast.parse(inspect.getsource(fp.clean_params))
    found: dict[str, tuple] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.ops[0], ast.NotIn)):
            continue
        target = node.left
        if not (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                and target.value.id == "out" and isinstance(target.slice, ast.Constant)):
            continue
        key = target.slice.value
        comparator = node.comparators[0]
        try:
            found[key] = tuple(sorted(ast.literal_eval(comparator)))
            continue
        except ValueError:
            pass
        if isinstance(comparator, ast.Name):
            resolved = {"FILM_FORMATS_MM": fp.FILM_FORMATS_MM,
                        "OUTPUT_RECIPES": fp.OUTPUT_RECIPES,
                        "valid_papers": fp.PAPERS}.get(comparator.id)
            if resolved is not None:
                found[key] = tuple(sorted(resolved))
    found["stock"] = tuple(sorted(profile["id"] for profile in fp.FILM_PROFILES))
    found["paper"] = tuple(sorted(fp.PAPERS))
    found["input_color_space"] = ("ProPhoto RGB",)
    found["film_tuning"] = ("original", "lighttable")
    found["film_tuning_version"] = (fp.film_tuning.VERSION,)
    return found


def _film_controls() -> list[Control]:
    choices = _film_enum_choices()
    controls = []
    for key, default in fp.DEFAULT_PARAMS.items():
        if isinstance(default, bool):
            controls.append(Control(f"film.{key}", "film", "boolean", default))
        elif isinstance(default, (int, float)):
            low, high = fp.NUMERIC_RANGES.get(key, (None, None))
            controls.append(Control(f"film.{key}", "film", "number",
                                    float(default), low, high))
        else:
            controls.append(Control(f"film.{key}", "film", "enum", default,
                                    choices=choices.get(key)))
    return controls


# Sliders whose element id does not spell out the schema path. This is UI
# wiring, not a restatement of any limit, and the coverage test fails on any
# range input that is neither listed here nor matched by name.
ELEMENT_CONTROL = {
    "healRadius": "heal.radius", "healFeather": "heal.feather",
    "healOpacity": "heal.opacity",
    "maskOpacity": "mask.opacity", "maskAngle": "mask.radial.angle",
    "maskRadiusX": "mask.radial.radiusX", "maskRadiusY": "mask.radial.radiusY",
    "maskShapeFeather": "mask.radial.feather",
    "maskBrushSize": "mask.brush.size", "maskBrushFeather": "mask.brush.feather",
    "maskBrushFlow": "mask.brush.flow", "maskBrushDensity": "mask.brush.density",
    "maskDepthLow": "mask.depth.depthLow", "maskDepthHigh": "mask.depth.depthHigh",
    "paramCurveHighlights": "parametricCurve.highlights",
    "paramCurveLights": "parametricCurve.lights",
    "paramCurveDarks": "parametricCurve.darks",
    "paramCurveShadows": "parametricCurve.shadows",
    # The three split sliders are shown in percent and divided by 100 in
    # app.js before they reach the schema, so their declared travel has to be
    # scaled before it can be compared with the server's clamp.
    "paramCurveSplitSD": ("parametricCurve.splitSD", 0.01),
    "paramCurveSplitDL": ("parametricCurve.splitDL", 0.01),
    "paramCurveSplitLH": ("parametricCurve.splitLH", 0.01),
}


@lru_cache(maxsize=1)
def sliders() -> dict[str, Slider]:
    """Every range input in the shipped markup, keyed by control name."""
    markup = (APP / "web/index.html").read_text(encoding="utf-8")
    binding = ("data-g", "data-local", "data-optics", "data-hsl",
               "data-point-color", "data-color-grade", "data-color-grade-master", "id")
    prefix = {"data-g": "grade", "data-local": "local", "data-optics": "optics",
              "data-hsl": "hsl", "data-point-color": "pointColor",
              "data-color-grade": "colorGrading", "data-color-grade-master": "colorGrading"}
    found: dict[str, Slider] = {}
    for tag in re.findall(r"<input[^>]*type=\"range\"[^>]*>", markup):
        attributes = dict(re.findall(r"([a-zA-Z-]+)=\"([^\"]*)\"", tag))
        name = next((attributes[key] for key in binding if attributes.get(key)), None)
        if name is None:
            continue
        source = next(key for key in binding if attributes.get(key))
        element = attributes.get("id") or f'{source}="{name}"'
        scale = 1.0
        if source == "id" and name not in fp.DEFAULT_PARAMS:
            mapped = ELEMENT_CONTROL.get(name)
            if isinstance(mapped, tuple):
                _, scale = mapped
        try:
            slider = Slider(element, float(attributes["min"]) * scale,
                            float(attributes["max"]) * scale,
                            float(attributes.get("step", 0)) * scale)
        except (KeyError, ValueError):
            continue
        if source == "id":
            if name in fp.DEFAULT_PARAMS:
                key = f"film.{name}"
            else:
                mapped = ELEMENT_CONTROL.get(name, f"ui.{name}")
                key = mapped[0] if isinstance(mapped, tuple) else mapped
        elif source == "data-hsl":
            key = f"hsl.*.{name}"
        else:
            key = f"{prefix[source]}.{name}"
        found[key] = slider
    return found


@lru_cache(maxsize=1)
def controls() -> tuple[Control, ...]:
    """Every processing control the application exposes."""
    found = (_grade_controls() + _local_controls() + _optics_controls()
             + _mask_controls() + _heal_controls() + _film_controls())
    markup = sliders()
    attached = []
    for control in found:
        slider = markup.get(control.key)
        if slider is None and control.surface == "hsl":
            slider = markup.get("hsl.*." + control.key.rsplit(".", 1)[1])
        attached.append(Control(control.key, control.surface, control.kind,
                                control.default, control.low, control.high,
                                control.choices, slider, control.cyclic,
                                control.optional))
    keys = [control.key for control in attached]
    duplicates = {key for key in keys if keys.count(key) > 1}
    if duplicates:
        raise AssertionError(f"duplicate control keys: {sorted(duplicates)}")
    return tuple(attached)


def by_key() -> dict[str, Control]:
    return {control.key: control for control in controls()}


def numeric(surface: str | None = None) -> list[Control]:
    return [control for control in controls() if control.numeric
            and (surface is None or control.surface == surface)]


if __name__ == "__main__":  # pragma: no cover - operator convenience
    for control in controls():
        bounds = (f"[{control.low}, {control.high}]{' cyclic' if control.cyclic else ''}"
                  if control.numeric else (control.choices or control.kind))
        marker = "slider" if control.slider else "      "
        print(f"{marker} {control.key:44} {control.kind:9} {bounds}")
    print(f"\n{len(controls())} controls, "
          f"{sum(1 for c in controls() if c.slider)} with sliders")
