# SPDX-License-Identifier: GPL-3.0-only
"""Every control must be exercised somewhere, and the UI must not out-range it.

The pixel gates in `scripts/check-processing.py` are driven by hand-written
case lists. That is fine until someone adds a slider: the new control renders,
ships, and is checked by nothing, because no list mentions it. This test
derives the full control inventory from the schemas and the shipped markup,
derives the exercised set from the case lists themselves, and fails when the
two disagree.

It also checks the sliders against the limits the server enforces. A slider
that travels past the server's clamp is a control that stops responding part
way along its own track, with no error anywhere.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

APP = Path(__file__).resolve().parents[1]
for path in (str(APP), str(APP / "tests")):
    if path not in sys.path:
        sys.path.insert(0, path)

import control_inventory as inventory  # noqa: E402
import control_semantics  # noqa: E402
import film_semantics  # noqa: E402
import processing_edit_cases  # noqa: E402
import processing_reference  # noqa: E402
import processing_support  # noqa: E402


def _keys_from_grade(values, prefix="grade") -> set[str]:
    """Canonical control keys a grade dictionary actually sets."""
    found = set()
    for key, value in (values or {}).items():
        if key == "hsl" and isinstance(value, dict):
            found |= {f"hsl.{band}.{axis}" for band, trio in value.items()
                      for axis in (trio or {})}
        elif key == "pointColor" and isinstance(value, list):
            found |= {f"pointColor.{field}" for entry in value for field in entry}
        elif key == "colorGrading" and isinstance(value, dict):
            for tone, entry in value.items():
                if isinstance(entry, dict):
                    found |= {f"colorGrading.{tone}.{axis}" for axis in entry}
                else:
                    found.add(f"colorGrading.{tone}")
        elif key == "parametricCurve" and isinstance(value, dict):
            found |= {f"parametricCurve.{field}" for field in value}
        else:
            found.add(f"{prefix}.{key}")
    return found


def _keys_from_mask(mask) -> set[str]:
    found = set()
    for component in (mask.get("components") or [mask]):
        kind = component.get("type", "radial")
        found.add(f"mask.type.{kind}")
        if component.get("combine"):
            found.add(f"mask.combine.{component['combine']}")
        for field in ("radius", "radiusX", "radiusY", "angle", "feather"):
            if field in component:
                found.add(f"mask.radial.{field}")
        for field in ("depthLow", "depthHigh"):
            if field in component:
                found.add(f"mask.depth.{field}")
        for stroke in component.get("strokes") or []:
            found |= {f"mask.brush.{field}" for field in stroke
                      if field not in ("points",)}
    for field in ("invert", "enabled", "opacity"):
        if field in mask:
            found.add(f"mask.{field}")
    found |= _keys_from_grade(mask.get("grade"), "local")
    return found


def exercised() -> dict[str, set[str]]:
    """Which suite exercises which control, derived from the case lists."""
    where: dict[str, set[str]] = {}

    def record(keys, suite):
        for key in keys:
            where.setdefault(key, set()).add(suite)

    for case in processing_support.grade_cases():
        record(_keys_from_grade(case.get("grade")), "webgl/app")
    for case in processing_edit_cases.edit_cases():
        record(_keys_from_grade(case.get("grade")), "export")
        record({f"optics.{key}" for key in case.get("optics", {})}, "export")
        for mask in case.get("masks", []):
            record(_keys_from_mask(mask), "export")
        for heal in case.get("heals", []):
            record({f"heal.mode.{heal.get('mode', 'heal')}"}, "export")
            record({f"heal.{field}" for field in heal
                    if field in ("radius", "feather", "opacity", "enabled")}, "export")
            record({f"heal.{field}" for field in ("target", "source")
                    if field in heal}, "export")
    film_cases = {**processing_reference.CASES, **processing_reference.BW_CASES}
    for overrides in film_cases.values():
        record({f"film.{key}" for key in overrides}, "film")
    # The semantics audits sweep every control they can render, so they count
    # as coverage in their own right.
    for finding in control_semantics.run():
        if finding.verdict not in ("unaudited",):
            record({finding.key}, "semantics")
    for control in film_semantics.auditable():
        record({control.key}, "film-semantics")
    return where


# Controls a pixel gate cannot own, each with the reason and the suite that
# does own it. An entry with no reason is a failure, not an excuse.
DELEGATED = {
    **{key: reason for key, reason in control_semantics.CLIENT_DERIVED.items()},
    **{key: reason for key, reason in control_semantics.COVERED_ELSEWHERE.items()},
    "grade.curveR": "swept by the WebGL curve cases",
    "grade.curveG": "swept by the WebGL curve cases",
    "grade.curveL": "swept by the WebGL curve cases",
    "grade.curveB": "swept by the WebGL curve cases",
    "local.curveL": "swept by the local-curve mask case",
    "local.curveR": "no local red-curve case; the shared curve path is covered globally",
    "local.curveG": "no local green-curve case; the shared curve path is covered globally",
    "local.curveB": "no local blue-curve case; the shared curve path is covered globally",
    "optics.profileOverride": "lens profile selection, covered by test_lens_matching.py",
    "film.film_tuning_version": "guards the tuning format; covered by test_film_tuning.py",
    "film.input_color_space": "single permitted value, enforced by clean_params",
    "film.profile_enabled": "film bypass, covered by the app suite's Film on/off step",
    "film.paper_locked": "paper auto-selection policy, covered by test_film_pipeline.py",
    "film.linear_input": "set by the decoder, not by a person",
    "mask.brush.edgeMask": "Auto Mask bitmaps, covered by the saved-auto-mask case",
    "mask.linear.start": "linear mask geometry, covered by tests/mask-shape.test.mjs",
    "mask.linear.end": "linear mask geometry, covered by tests/mask-shape.test.mjs",
}


class ControlCoverage(unittest.TestCase):
    def test_every_control_is_exercised(self):
        covered = exercised()
        missing = sorted(control.key for control in inventory.controls()
                         if control.key not in covered
                         and control.key not in DELEGATED)
        self.assertEqual(missing, [], "\n".join([
            "These controls are not exercised by any case list or audit.",
            "Add a case, or add an entry to DELEGATED naming the suite that",
            "does cover it and why a pixel gate cannot:", *missing]))

    def test_delegated_controls_still_exist(self):
        """A waiver for a control that has been removed is dead weight."""
        known = {control.key for control in inventory.controls()}
        stale = sorted(key for key in DELEGATED if key not in known)
        self.assertEqual(stale, [], f"DELEGATED names controls that no longer exist: {stale}")

    def test_sliders_stay_inside_the_server_range(self):
        """A slider must not travel past the clamp the server applies."""
        problems = []
        for control in inventory.controls():
            slider = control.slider
            if slider is None or not control.numeric or control.cyclic:
                continue
            if control.low is not None and slider.low < control.low - 1e-9:
                problems.append(f"{control.key}: slider starts at {slider.low}, "
                                f"server clamps to {control.low}")
            if control.high is not None and slider.high > control.high + 1e-9:
                problems.append(f"{control.key}: slider reaches {slider.high}, "
                                f"server clamps to {control.high}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_every_slider_has_a_finite_server_clamp(self):
        """An unbounded control accepts nonsense from a preset or the CLI."""
        unbounded = sorted(
            control.key for control in inventory.controls()
            if control.slider and control.numeric and not control.cyclic
            and (control.low is None or control.high is None
                 or abs(control.low) >= inventory.BIG
                 or abs(control.high) >= inventory.BIG))
        self.assertEqual(unbounded, [], "\n".join([
            "These controls have a slider but no server-side clamp, so a",
            "preset, sidecar or command-line edit can send any value:",
            *unbounded]))

    def test_unmatched_markup_sliders_are_declared_non_processing(self):
        """A new slider must be classified, not silently ignored."""
        known = {control.key for control in inventory.controls()}
        stray = sorted(
            key for key in inventory.sliders()
            if key not in known
            and key not in inventory.NON_PROCESSING_TEMPLATE_SLIDERS
            and key.split(".", 1)[1] not in inventory.NON_PROCESSING_UI)
        self.assertEqual(stray, [], "\n".join([
            "These range inputs match no control in the schema. Wire them up,",
            "or add them to NON_PROCESSING_UI (or the template waiver) with",
            "the reason they are not part of image processing:", *stray]))


if __name__ == "__main__":
    unittest.main()
