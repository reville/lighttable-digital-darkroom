# SPDX-License-Identifier: GPL-3.0-only
"""Does each film control do what its name says?

The companion to `control_semantics`, for the physical-simulation stage. The
grade runs in NumPy and can be swept in milliseconds; the film stage runs in
the Rust engine, so this sweeps fewer values on a smaller target and is kept
out of the fast unit suite.

The verdict vocabulary is shared with `control_semantics`, so both halves of
the audit read as one report.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np
import tifffile

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import film_pipeline as fp  # noqa: E402
from control_inventory import Control, controls  # noqa: E402
from control_semantics import (  # noqa: E402
    Finding, _monotone, chroma_mean, contrast, edge_energy, mean_luma,
    warm_balance,
)

import engine_runner  # noqa: E402
# Fewer samples than the grade audit: each one is a full engine render.
SAMPLES = 5
# A baseline that switches off every stage the audit is not measuring, so one
# control's sweep cannot be masked by another's contribution.
BASELINE = {
    "stock": "kodak_portra_400", "paper": "kodak_portra_endura",
    "workflow_mode": "creative", "linear_input": True, "auto_exposure": False,
    "grain_on": False, "halation_on": False, "glare_on": False,
    "couplers_on": False, "camera_diffusion_strength": 0.0,
    "scan_softness": 0.0, "scan_sharpen": False, "output_recipe": "clean_scan",
}
# Controls that need their own switch, or another stage, turned on first.
COMPANION = {
    "couplers_amount": {"couplers_on": True},
    "halation_amount": {"halation_on": True},
    "grain_amount": {"grain_on": True},
    "glare_amount": {"glare_on": True, "output_recipe": "neutral_print_scan"},
    "glare_on": {"glare_amount": 3.0, "output_recipe": "neutral_print_scan"},
    "halation_on": {"halation_amount": 3.0},
    "scan_sharpness": {"scan_sharpen": True},
    "camera_diffusion_family": {"camera_diffusion_strength": 0.8},
    "film_format": {"grain_on": True, "grain_amount": 2.0},
    # The measured development-time families belong to the black-and-white
    # stocks; a colour negative ignores the value entirely.
    "development_time": {"stock": "kodak_doublex", "paper": "kodak_2302"},
    "print_development_time": {"stock": "kodak_doublex", "paper": "kodak_2302"},
}
# The measured minute families, rather than a linear sweep of a range whose
# ends the profile never uses.
EXPLICIT_VALUES = {
    "film.development_time": [4.0, 5.0, 6.5, 9.0, 12.0],
    "film.print_development_time": [2.0, 3.5, 5.0, 7.0, 9.0],
}
# Capture-stage controls resolved by the RAW decoder before the film model
# ever runs. A rendered TIFF cannot exercise them.
NEEDS_RAW = {
    "film.wb_mode", "film.wb_temperature", "film.wb_tint", "film.raw_profile",
    "film.raw_highlight_recovery", "film.raw_sensor_denoise",
    "film.learned_denoise", "film.learned_denoise_strength",
}
# Controls that legitimately do not reach the film engine.
OUT_OF_BAND = {
    "film.developProfile":
        "applies only when the film profile is switched off",
    "film.workflow_mode":
        "chooses which paper is auto-selected, not how a render is computed",
    "film.rotate":
        "presentation rotation, applied outside the engine",
}


def _grain_variance(image, base):
    """Grain is stochastic, so read its strength as added flat-field spread."""
    unit = image.shape[1] // 10
    flat = image[unit + 2:3 * unit - 2, unit + 2:3 * unit - 2]
    return float((flat @ np.array([0.2126, 0.7152, 0.0722])).var())


def _edge_steepness(image, base):
    """Steepest step across the target's hard block boundary.

    A whole-image edge average is the wrong instrument for a softening
    control: the frame is mostly a smooth ramp, and diffusion *adds* micro
    gradients there, so the average rises while the real edge softens.
    """
    unit = image.shape[1] // 10
    band = image[unit + 4:3 * unit - 4, 4 * unit:9 * unit, 1]
    return float(np.abs(np.diff(band, axis=1)).max())


CLAIMS = {
    "film.exposure_ev": (mean_luma, +1, "exposes the negative further"),
    "film.gamma": (contrast, +1, "develops to a steeper characteristic curve"),
    "film.print_exposure": (mean_luma, -1, "exposes the print, darkening it"),
    "film.grain_amount": (_grain_variance, +1, "coarsens the grain"),
    "film.scan_softness": (_edge_steepness, -1, "softens the scan"),
    "film.scan_sharpness": (edge_energy, +1, "sharpens the scan"),
    # Yellow filtration subtracts blue from the enlarger light, so a negative
    # prints cooler as the shift rises. The sign is the process, not a bug.
    "film.print_y_filter_shift": (warm_balance, -1,
                                  "filters yellow, cooling the print"),
    "film.camera_diffusion_strength": (_edge_steepness, -1,
                                       "diffuses the taking lens"),
    "film.couplers_amount": (chroma_mean, +1, "strengthens the dye couplers"),
}
# Physical stages whose direction is genuinely stock-dependent, or whose
# effect the audit target cannot isolate at this size.
TRACKED_DEFECTS = {
    "film.exposure_ev":
        "Camera exposure compensation barely reaches the picture: a six-stop "
        "sweep from -3 to +3 EV moves mean luminance by about two per cent, "
        "and not monotonically, with auto exposure off and print exposure "
        "fixed. Print exposure, by contrast, works across its whole travel. "
        "The Rust engine and the Python spectral runtime agree, so this is a "
        "property of the model rather than a porting error.",
    "film.glare_amount":
        "Scanner glare never reaches a visible strength. On the negative and "
        "print route where it is supposed to apply, the maximum amount of 3.0 "
        "moves the render by 1.9 eight-bit code values, and the default 1.0 by "
        "0.7. The slider is wired and the response is linear in the amount; it "
        "is simply scaled far below anything a person can see. (Zero effect on "
        "a reversal stock is separate and intended: reversal routes straight "
        "to the scanner and skips print-stage glare.)",
    "film.glare_on":
        "The same scaling as film.glare_amount: switching scanner glare on at "
        "its default amount changes the render by under one code value, so the "
        "switch reads as inert.",
}
UNCLAIMED = {
    "film.development_time": "measured curve family; direction is per stock",
    "film.print_development_time": "measured curve family; direction is per paper",
    "film.print_preflash": "flashes the paper; effect is confined to the toe",
    "film.print_m_filter_shift": "magenta enlarger filtration",
    "film.halation_amount": "spreads light around highlights",
    "film.glare_amount": "scanner veiling flare",
    "film.rotate": "presentation rotation, not a tonal control",
    "film.wb_temperature": "capture white balance, applied before the film model",
    "film.wb_tint": "capture white balance, applied before the film model",
    "film.learned_denoise_strength": "needs the optional AI runtime",
}


def target(width: int = 320, height: int = 224) -> np.ndarray:
    """Grey ramp, colour patches, a flat field, and one hard edge."""
    image = np.zeros((height, width, 3), dtype=np.float64)
    image[:] = np.linspace(0.02, 0.95, width)[None, :, None]
    # Halation and scanner glare spread over a physical distance on the film,
    # so a thumbnail-sized target puts the whole effect inside one pixel and
    # reports a working stage as inert. These blocks are sized for that.
    unit = width // 10
    image[unit:3 * unit, unit:3 * unit] = 0.18
    image[unit:3 * unit, 4 * unit:6 * unit] = 1.0
    image[unit:3 * unit, 6 * unit:9 * unit] = 0.02
    # The fourth patch is a yellow-green with green > red > blue, the only
    # sector the LightTable film tuning acts on. Without it the tuning has
    # nothing in frame to change and reads as inert.
    for index, colour in enumerate(((0.7, 0.1, 0.08), (0.08, 0.6, 0.12),
                                    (0.06, 0.15, 0.8), (0.38, 0.66, 0.09))):
        image[4 * unit:6 * unit, index * 2 * unit:(index + 1) * 2 * unit] = colour
    return np.clip(image, 0, 1)


def _render(root: Path, source: Path, overrides: dict, name: str,
            exclude: str | None = None, engine=None) -> np.ndarray:
    baseline = {key: value for key, value in BASELINE.items() if key != exclude}
    params = {**baseline, **overrides}
    output = root / f"{name}.tif"
    if engine is None:
        return engine_runner.render(source, params, output)
    return engine.render(source, params, output)


def _values(control: Control) -> list:
    if control.key in EXPLICIT_VALUES:
        return list(EXPLICIT_VALUES[control.key])
    if control.kind == "boolean":
        return [False, True]
    if control.kind == "enum":
        return list(control.choices or ())
    return control.sweep(SAMPLES)


def audit(control: Control, root: Path, source: Path, engine=None) -> Finding:
    name = control.key.split(".", 1)[1]
    if control.key in NEEDS_RAW:
        return Finding(control.key, "needs-raw",
                       "resolved by the RAW decoder before the film model; "
                       "covered by tests/test_raw_capture.py")
    if control.key in OUT_OF_BAND:
        return Finding(control.key, "out-of-band", OUT_OF_BAND[control.key])
    companion = COMPANION.get(name, {})
    safe = control.key.replace(".", "-")
    try:
        # The baseline must not supply the control under audit, or its schema
        # default would read as a change against the baseline's override.
        without = _render(root, source, companion, f"{safe}-base",
                          exclude=name, engine=engine)
        values = _values(control)
        renders = [_render(root, source, {**companion, name: value},
                           f"{safe}-{index}", exclude=name, engine=engine)
                   for index, value in enumerate(values)]
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
        return Finding(control.key, "unaudited", str(error))

    if control.default in values:
        at_default = renders[values.index(control.default)]
        leak = float(np.abs(at_default - without).max())
        if leak > 2 / 255:
            return Finding(control.key, "leaky",
                           f"default {control.default} differs from the same "
                           f"render without it by {leak:.4f}")

    spread = max(float(np.abs(render - without).max()) for render in renders)
    if spread <= 2 / 255:
        verdict = "tracked-defect" if control.key in TRACKED_DEFECTS else "inert"
        return Finding(control.key, verdict,
                       TRACKED_DEFECTS.get(
                           control.key, f"no visible change across {values}"),
                       values=values)

    claim = CLAIMS.get(control.key)
    if claim:
        metric, direction, sentence = claim
        readings = [metric(render, without) for render in renders]
        if _monotone(readings, direction, tolerance=0.06):
            return Finding(control.key, "directional", "claim holds across the sweep",
                           sentence, metric.__name__, readings, values)
        verdict = "tracked-defect" if control.key in TRACKED_DEFECTS else "contradicted"
        detail = TRACKED_DEFECTS.get(
            control.key,
            f"{metric.__name__} did not move "
            f"{'up' if direction > 0 else 'down'} across the sweep")
        return Finding(control.key, verdict, detail,
                       sentence, metric.__name__, readings, values)

    readings = [float(np.abs(render - without).mean()) for render in renders]
    return Finding(control.key, "responsive",
                   UNCLAIMED.get(control.key, "changes the render"),
                   metric="divergence", readings=readings, values=values)


def auditable() -> list[Control]:
    skip = {"film.profile_enabled", "film.input_color_space", "film.linear_input",
            "film.film_tuning_version", "film.paper_locked"}
    return [control for control in controls()
            if control.surface == "film" and control.key not in skip]


def run(output: Path | None = None) -> list[Finding]:
    if not engine_runner.available():
        raise RuntimeError(
            f"film audit needs {engine_runner.binary()}; {engine_runner.BUILD_HINT}")
    with tempfile.TemporaryDirectory(prefix="lighttable-film-audit-") as temp:
        root = Path(output) if output else Path(temp)
        root.mkdir(parents=True, exist_ok=True)
        source = root / "target.tif"
        tifffile.imwrite(source, (target() * 65535 + 0.5).astype(np.uint16))
        # One resident process for the whole sweep: respawning the engine per
        # render costs more than every render put together.
        with engine_runner.session() as engine:
            return [audit(control, root, source, engine)
                    for control in auditable()]


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
