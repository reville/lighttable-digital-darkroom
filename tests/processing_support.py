# SPDX-License-Identifier: GPL-3.0-only
"""Shared fixtures and fail-closed numerical scoring for processing gates.

References must come from a separate implementation, never captured goldens.
All image tolerances are expressed in 8-bit code values, even for float input.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image


PIXEL_TOLERANCE = {"mean": 0.75, "p95": 2.0, "max": 5.0}


def run_browser(command, *, cwd, timeout):
    """Allow the JS signal handler to close owned browser children on timeout."""
    child = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = child.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        child.terminate()
        try:
            child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)
        raise
    return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)


def target_rgb8(width=128, height=96):
    """Asymmetric ramps, color patches, edges and noise expose common errors."""
    y, x = np.mgrid[:height, :width]
    image = np.stack((x / (width - 1), y / (height - 1),
                      (x + 2 * y) / (width + 2 * height - 3)), axis=-1)
    image[height//3:height//2, :width//4] = (0.8, 0.12, 0.04)
    image[height//2:3*height//4, width//2:3*width//4] = (0.05, 0.25, 0.9)
    image[:height//6, :width//6] = 0
    image[-height//6:, -width//6:] = 1
    noise = np.random.default_rng(8301).normal(0, 0.015, image.shape)
    image[height//2:, :width//3] += noise[height//2:, :width//3]
    return np.round(np.clip(image, 0, 1) * 255).astype(np.uint8)


def grade_cases():
    cases = [{"name": "identity", "grade": {}}]
    for key in ("exposure", "contrast", "highlights", "shadows", "whites",
                "blacks", "temp", "tint", "vibrance", "saturation", "texture",
                "clarity", "dehaze", "vignette", "chromaticAberrationRedCyan",
                "chromaticAberrationBlueYellow"):
        for value in (-0.65, 0.65):
            cases.append({"name": f"{key}-{'negative' if value < 0 else 'positive'}",
                          "grade": {key: value}})
    for key in ("sharpness", "luminanceNoise", "colorNoise"):
        cases.append({"name": key, "grade": {key: 0.7}})
    cases.extend([
        {"name": "vignette-small-soft", "grade": {"vignette": .65,
         "vignetteSize": .2, "vignetteFeather": .8}},
        {"name": "vignette-large-soft", "grade": {"vignette": .65,
         "vignetteSize": .8, "vignetteFeather": 1}},
        {"name": "vignette-hard-bright", "grade": {"vignette": -.65,
         "vignetteSize": .3, "vignetteFeather": 0}},
        {"name": "vignette-shape-disabled", "grade": {
         "vignetteSize": 0, "vignetteFeather": 0}},
        {"name": "sharpen-controls", "grade": {"sharpness": 0.7,
         "sharpenRadius": 2.2, "sharpenDetail": 0.6, "sharpenMasking": 0.4}},
        {"name": "curves", "grade": {"curveL": (np.linspace(0, 1, 256) ** 0.8).tolist(),
         "curveB": (np.linspace(0, 1, 256) ** 1.2).tolist()}},
        {"name": "hsl", "grade": {"hsl": {"red": {"h": 0.2, "s": -0.4, "l": 0.1},
         "blue": {"h": -0.3, "s": 0.2, "l": -0.15}}}},
        {"name": "point-color", "grade": {"pointColor": [{"hue": 20, "range": 50,
         "hueShift": 15, "saturation": -0.2, "luminance": 0.1,
         "uniformHue": 0.3, "uniformSaturation": 0.2, "uniformLuminance": 0.2,
         "refSaturation": 0.6, "refLuminance": 0.5}]}},
        {"name": "color-grading", "grade": {"colorGrading": {
         "shadows": {"hue": 220, "saturation": 0.4},
         "highlights": {"hue": 30, "saturation": 0.3}, "balance": 0.1, "blending": 0.6}}},
        {"name": "combined-order", "grade": {"exposure": 0.4, "contrast": 0.2,
         "highlights": -0.35, "shadows": 0.3, "temp": 0.2, "tint": -0.1,
         "saturation": -0.2, "dehaze": 0.1}},
    ])
    for key in ('curveR', 'curveG'):
        cases.append({'name':key, 'grade':{key:(np.linspace(0, 1, 256) ** 0.75).tolist()}})
    for band in ('orange', 'yellow', 'green', 'aqua', 'purple', 'magenta'):
        cases.append({'name':'hsl-' + band, 'grade':{'hsl':{band:{'h':0.3, 's':-0.4, 'l':0.15}}}})
    for tone in ('midtones', 'global'):
        cases.append({'name':'color-grading-' + tone, 'grade':{'colorGrading':{
            tone:{'hue':180, 'saturation':0.4, 'luminance':0.15}}}})
    return cases


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compare_images(name, reference, actual, outdir, tolerance=None):
    tolerance = dict(PIXEL_TOLERANCE if tolerance is None else tolerance)
    result = {"name": name, "status": "fail", "tolerance": tolerance,
              "units": "8-bit code values", "artifacts": {}}
    reference, actual = np.asarray(reference), np.asarray(actual)
    if reference.shape != actual.shape or reference.ndim != 3 or reference.shape[-1] != 3:
        result["error"] = f"RGB shape mismatch: {reference.shape} versus {actual.shape}"
        return result
    if not reference.size or not np.isfinite(reference).all() or not np.isfinite(actual).all():
        result["error"] = "Empty or non-finite pixel output"
        return result
    error = np.abs(reference.astype(np.float64) - actual.astype(np.float64)) * 255
    metrics = {"mean": float(error.mean()), "p95": float(np.percentile(error, 95)),
               "max": float(error.max())}
    result.update(metrics=metrics, shape=list(actual.shape),
                  status="pass" if all(metrics[k] <= tolerance[k] for k in metrics) else "fail")
    directory = Path(outdir) / name
    directory.mkdir(parents=True, exist_ok=True)
    for key, pixels in (("reference", reference), ("actual", actual),
                        ("difference-8x", error / 255 * 8)):
        path = directory / f"{key}.png"
        Image.fromarray(np.round(np.clip(pixels, 0, 1) * 255).astype(np.uint8)).save(path)
        result["artifacts"][key] = str(path.resolve())
    return result
