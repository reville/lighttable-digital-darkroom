"""Frozen renders of complete recipes, as the last line of defence.

Every other gate compares two implementations of the same idea. That cannot
catch a change of mind: if the grade and the shader are edited together, or a
profile's data is replaced, both sides agree and every parity gate stays
green while the picture people already saved quietly changes.

These goldens are the opposite kind of test. They pin what the current code
actually produces for a handful of complete recipes, at a size small enough
to keep in the repository, and fail on changes beyond sparse one-code CPU
rounding (at most 0.01% of channels). That makes an
intended change visible in review as an image diff, and an unintended one
impossible to miss.

Re-blessing is deliberate and audited:

    python tests/processing_goldens.py --bless

which refuses unless RENDER_CACHE_VERSION has been raised in the same tree,
because a changed render that keeps its cache key serves stale pixels from
every warm cache in the field.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

import numpy as np
from PIL import Image
import tifffile

APP = Path(__file__).resolve().parents[1]
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import edits  # noqa: E402
import engine_runner  # noqa: E402
import film_pipeline as fp  # noqa: E402
import grade  # noqa: E402

GOLDENS = Path(__file__).parent / "goldens"
SIZE = (160, 112)


def source_image() -> np.ndarray:
    """A compact but varied target: ramp, patches, flat field, hard edge."""
    width, height = SIZE
    x = np.linspace(0.01, 0.99, width, dtype=np.float64)
    y = np.linspace(0.15, 1.0, height, dtype=np.float64)[:, None]
    image = np.stack([x[None, :] * y, np.sqrt(x)[None, :] * y,
                      (1 - 0.7 * x)[None, :] * y], axis=-1)
    patches = [(0.72, 0.11, 0.07), (0.09, 0.58, 0.13), (0.07, 0.16, 0.79),
               (0.38, 0.66, 0.09), (0.18, 0.18, 0.18), (0.86, 0.84, 0.80)]
    for index, colour in enumerate(patches):
        left = index * width // len(patches)
        image[8:28, left:left + width // len(patches)] = colour
    image[40:60, 20:60] = 1.0
    image[40:60, 60:100] = 0.015
    return np.clip(image, 0, 1)


def _film(params: dict, root: Path, source: Path, name: str, engine=None) -> np.ndarray:
    resolved = fp.clean_params(params)
    # Mirror render_cli's router. With the film profile switched off it does
    # not call the engine at all, and sending the recipe there anyway would
    # pin the wrong path and hide the bypass entirely.
    if not resolved["profile_enabled"]:
        return np.clip(fp.render_float(fp.load_linear(str(source), max_width=None),
                                       resolved), 0, 1)
    output = root / f"{name}.tif"
    if engine is None:
        return engine_runner.render(source, resolved, output)
    return engine.render(source, resolved, output)


def cases() -> list[dict]:
    """One recipe per behaviour worth pinning, not one per control."""
    curve = (np.linspace(0, 1, 256) ** 0.75).tolist()
    chosen = [
        # A stock from each family, so a profile data change is visible.
        {"name": "portra-400-print", "params": {"stock": "kodak_portra_400"}},
        {"name": "vision3-500t-print", "params": {"stock": "kodak_vision3_500t"}},
        {"name": "velvia-100-reversal", "params": {"stock": "fujifilm_velvia_100"}},
        {"name": "doublex-bw-print", "params": {
            "stock": "kodak_doublex", "paper": "kodak_2302",
            "development_time": 6.5, "print_development_time": 5.0}},
        {"name": "portra-400-lighttable-tuned", "params": {
            "stock": "kodak_portra_400", "film_tuning": "lighttable"}},
        # The physical stages, together, at settings a person would use.
        {"name": "physical-stages", "params": {
            "grain_on": True, "grain_amount": 1.5, "halation_on": True,
            "halation_amount": 2.0, "camera_diffusion_strength": 0.5,
            "print_preflash": 0.05, "print_y_filter_shift": 6.0,
            "scan_softness": 0.3, "scan_sharpen": True}},
        # The film bypass, which skips the engine entirely. developProfile
        # only separates from this on a RAW source, so it belongs to the RAW
        # suite rather than to a second golden here.
        {"name": "film-off", "params": {"profile_enabled": False}},
    ]
    graded = [
        {"name": "grade-tone", "grade": {
            "exposure": 0.4, "contrast": 0.25, "highlights": -0.4,
            "shadows": 0.35, "whites": 0.2, "blacks": -0.15}},
        {"name": "grade-colour", "grade": {
            "temp": 0.3, "tint": -0.15, "vibrance": 0.4, "saturation": -0.2,
            "hsl": {"orange": {"h": 0.2, "s": 0.3, "l": 0.1},
                    "blue": {"h": -0.25, "s": -0.3, "l": -0.1}}}},
        {"name": "grade-detail", "grade": {
            "texture": 0.5, "clarity": 0.35, "sharpness": 0.6,
            "sharpenRadius": 1.8, "sharpenDetail": 0.5, "sharpenMasking": 0.3,
            "luminanceNoise": 0.3, "colorNoise": 0.4}},
        {"name": "grade-advanced", "grade": {
            "curveL": curve,
            "pointColor": [{"hue": 25, "range": 45, "hueShift": 12,
                            "saturation": 0.3, "luminance": 0.15}],
            "colorGrading": {"shadows": {"hue": 215, "saturation": 0.35},
                             "highlights": {"hue": 38, "saturation": 0.3},
                             "balance": 0.1, "blending": 0.6}}},
        {"name": "grade-vignette", "grade": {
            "vignette": 0.6, "vignetteSize": 0.35, "vignetteFeather": 0.7,
            "chromaticAberrationRedCyan": 0.3}},
    ]
    edited = [
        {"name": "edit-mask-and-heal",
         "grade": {"exposure": 0.2},
         "masks": [{"type": "radial", "center": [0.35, 0.5], "radiusX": 0.3,
                    "radiusY": 0.2, "angle": 25, "feather": 0.5, "opacity": 0.85,
                    "grade": {"exposure": -0.6, "texture": 0.4, "temp": 0.25}}],
         "heals": [{"mode": "clone", "target": [0.7, 0.35], "source": [0.4, 0.35],
                    "radius": 0.1, "feather": 0.4}]},
        {"name": "edit-optics",
         "optics": {"rotate": 4.0, "scale": 1.1, "distortion": 0.25,
                    "vertical": 0.2, "vignette": -0.3}},
    ]
    recipes = chosen + graded + edited
    # These are frozen recipes, including their film interpretation. The app
    # now defaults to tuned Portra; that must not silently turn the original
    # Portra/grade references into a different recipe during CI.
    for case in recipes:
        case.setdefault("params", {}).setdefault("film_tuning", "original")
    return recipes


def render(case: dict, root: Path, source: Path, engine=None) -> np.ndarray:
    """Full pipeline for one case: film, then base edits, then grade."""
    image = _film(case.get("params", {}), root, source, case["name"], engine)
    optics, heals = case.get("optics"), case.get("heals")
    if optics:
        image = edits.apply_manual_optics(image, edits.clean_optics(optics))
    if heals:
        image = edits.apply_heals(image, heals)
    if case.get("masks"):
        image = edits.apply_masks(image, case["masks"])
    if case.get("grade"):
        image = grade.apply(image, case["grade"])
    return np.clip(image, 0, 1)


def encode(image: np.ndarray) -> bytes:
    return np.round(image * 255).astype(np.uint8).tobytes()


def digest(image: np.ndarray) -> str:
    return hashlib.sha256(encode(image)).hexdigest()


def render_all(root: Path) -> dict[str, np.ndarray]:
    source = root / "source.tif"
    tifffile.imwrite(source, (source_image() * 65535 + 0.5).astype(np.uint16))
    with engine_runner.session() as engine:
        return {case["name"]: render(case, root, source, engine)
                for case in cases()}


def cache_version() -> str:
    for module in ("film_pipeline", "server"):
        text = (APP / f"{module}.py").read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("RENDER_CACHE_VERSION"):
                # The declaration usually carries a trailing comment saying
                # why it was last raised; the version is the value alone.
                return line.split("=", 1)[1].split("#", 1)[0].strip()
    raise AssertionError("RENDER_CACHE_VERSION not found")


def manifest() -> dict:
    path = GOLDENS / "manifest.json"
    return json.loads(path.read_text()) if path.is_file() else {}


def bless() -> int:
    """Rewrite the goldens, refusing to hide a silent cache-key collision."""
    recorded = manifest().get("renderCacheVersion")
    current = cache_version()
    if recorded is not None and recorded == current:
        print("Refusing to bless: RENDER_CACHE_VERSION is unchanged at "
              f"{current}. A changed render that keeps its cache key serves "
              "the old pixels from every warm cache. Raise it first.",
              file=sys.stderr)
        return 1
    GOLDENS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lighttable-goldens-") as temp:
        rendered = render_all(Path(temp))
    digests = {}
    for name, image in sorted(rendered.items()):
        Image.fromarray(np.round(image * 255).astype(np.uint8)).save(
            GOLDENS / f"{name}.png", optimize=True)
        digests[name] = digest(image)
    (GOLDENS / "manifest.json").write_text(json.dumps(
        {"size": list(SIZE), "renderCacheVersion": current,
         "profileCatalogDigest": fp.PROFILE_CATALOG_DIGEST,
         "engine": engine_runner.binary().name,
         "digests": digests}, indent=2) + "\n")
    print(f"Blessed {len(digests)} goldens at cache version {current}.")
    return 0


if __name__ == "__main__":  # pragma: no cover - operator convenience
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bless", action="store_true",
                        help="rewrite the golden images from the current code")
    if parser.parse_args().bless:
        raise SystemExit(bless())
    with tempfile.TemporaryDirectory(prefix="lighttable-goldens-") as temp:
        for name, image in sorted(render_all(Path(temp)).items()):
            print(f"{name:34} {digest(image)[:16]}")
