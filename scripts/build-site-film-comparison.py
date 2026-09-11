#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build the website's reproducible RAW/XMP/film comparison.

The default run uses a tracked CC0 RAW. The XMP result is intentionally
described as a Lightroom-compatible preset rendered by LightTable, never as
an Adobe Lightroom render. Pass ``--adobe-reference`` with a real Lightroom
JPEG/TIFF export to substitute an Adobe-rendered middle column.

Run from the app directory and point at a checkout of the canonical website:

    .venv/bin/python scripts/build-site-film-comparison.py \
        --output /path/to/lighttable-site/assets/film-comparison
    .venv/bin/python scripts/build-site-film-comparison.py --check \
        --output /path/to/lighttable-site/assets/film-comparison
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image, ImageOps


APP = Path(__file__).resolve().parents[1]
DEFAULT_RAW = APP / "demo-assets/cc0-raw/files/06-nikon-z6-still-life-toys.NEF"
DEFAULT_XMP = APP / "scripts/site-film-comparison/lighttable-natural.xmp"
_SITE_ROOT = os.environ.get("LIGHTTABLE_SITE_ROOT")
DEFAULT_OUTPUT = (
    Path(_SITE_ROOT).expanduser() / "assets/film-comparison"
    if _SITE_ROOT else None
)
MANIFEST_NAME = "manifest.json"
WEB_LONG_EDGE = 1800
CROP_SIZE = 900

DEFAULT_SOURCE_METADATA = {
    "description": "Toy still life — Nikon Z6 RAW",
    "sourceId": "raw.pixls.us 3587",
    "sourceUrl": (
        "https://raw.pixls.us/getfile.php/3587/nice/"
        "Nikon%20-%20Z%206%20-%2012bit%2012bit%20compressed%20%283%3A2%29.NEF"
    ),
    "license": "Creative Commons Zero 1.0 — Public Domain",
    "licenseUrl": "https://creativecommons.org/publicdomain/zero/1.0/",
}

FILM_BASE = {
    "profile_enabled": True,
    "stock": "kodak_gold_200",
    "paper": "kodak_portra_endura",
    "workflow_mode": "authentic",
    "linear_input": True,
    "input_color_space": "ProPhoto RGB",
    "film_format": "35mm",
    "auto_exposure": True,
    "grain_on": True,
    "grain_amount": 1.0,
    "halation_on": True,
    "halation_amount": 1.0,
    "couplers_on": True,
    "couplers_amount": 1.0,
    "glare_on": True,
    "glare_amount": 1.0,
    "scan_sharpen": True,
    "scan_sharpness": 1.0,
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def relative_name(path: Path) -> str:
    try:
        return path.resolve().relative_to(APP.resolve()).as_posix()
    except ValueError:
        return path.name


def git_revision() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=APP, capture_output=True, text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def image_float(path: Path) -> np.ndarray:
    image = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    return np.asarray(image, dtype=np.float32) / 255.0


def fit_reference(reference: np.ndarray, target_shape: tuple[int, int]) -> np.ndarray:
    target_h, target_w = target_shape
    source_h, source_w = reference.shape[:2]
    source_ratio = source_w / source_h
    target_ratio = target_w / target_h
    if abs(source_ratio - target_ratio) > 0.01:
        raise ValueError(
            "Adobe reference aspect ratio does not match the decoded RAW; "
            "export it without crop or geometry changes"
        )
    image = Image.fromarray((np.clip(reference, 0, 1) * 255 + 0.5).astype(np.uint8))
    image = image.resize((target_w, target_h), Image.Resampling.LANCZOS)
    return np.asarray(image, dtype=np.float32) / 255.0


def parse_crop(value: str | None, width: int, height: int) -> dict[str, int]:
    size = min(CROP_SIZE, width, height)
    if value:
        pieces = [int(piece.strip()) for piece in value.split(",")]
        if len(pieces) != 3:
            raise ValueError("--crop must be x,y,size in decoded-image pixels")
        x, y, size = pieces
    else:
        # The default CC0 still life: detailed midtones and a smooth neutral
        # patch share this area, making both grain and edge behavior visible.
        x = int(round(width * 0.52))
        y = int(round(height * 0.38))
    size = max(64, min(size, width, height))
    x = max(0, min(x, width - size))
    y = max(0, min(y, height - size))
    return {"x": x, "y": y, "width": size, "height": size}


def crop_image(image: np.ndarray, crop: dict[str, int]) -> np.ndarray:
    x, y = crop["x"], crop["y"]
    return np.ascontiguousarray(
        image[y:y + crop["height"], x:x + crop["width"]]
    )


def save_web(image: np.ndarray, destination: Path, *, long_edge: int | None) -> None:
    import color_pipeline

    output = image
    if long_edge:
        output = color_pipeline.resize_float(output, long_edge)
    color_pipeline.save_export_image(
        output, destination, fmt="jpeg", quality=94, output_space="srgb"
    )


def output_record(path: Path, role: str) -> dict:
    with Image.open(path) as image:
        width, height = image.size
    return {
        "file": path.name,
        "role": role,
        "width": width,
        "height": height,
        "sha256": sha256(path),
    }


def build(args: argparse.Namespace) -> dict:
    sys.path.insert(0, str(APP))
    import color_pipeline
    import film_pipeline
    import grade
    import preset_io
    from render_cli import render_rust

    raw_path = args.raw.resolve()
    xmp_path = args.xmp.resolve()
    output_dir = args.output.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not raw_path.is_file():
        raise FileNotFoundError(raw_path)
    if not xmp_path.is_file():
        raise FileNotFoundError(xmp_path)

    raw_params = film_pipeline.clean_params({
        "wb_mode": "as_shot",
        "raw_profile": "camera",
        "raw_highlight_recovery": "reconstruct",
        "raw_sensor_denoise": "off",
        "developProfile": "standard",
    })
    linear = color_pipeline.decode_raw(raw_path, raw_params)
    neutral = color_pipeline.linear_prophoto_to_display_srgb(linear, raw_params)
    height, width = neutral.shape[:2]
    crop = parse_crop(args.crop, width, height)

    preset = preset_io.import_lightroom(xmp_path.read_text(), xmp_path.name)
    adobe_reference = None
    if args.adobe_reference:
        adobe_reference = args.adobe_reference.resolve()
        xmp_result = fit_reference(image_float(adobe_reference), (height, width))
        xmp_kind = "adobe-lightroom-export"
        xmp_label = "Adobe Lightroom export"
    else:
        xmp_result = np.clip(grade.apply(neutral, preset["grade"]), 0, 1)
        xmp_kind = "lighttable-xmp-conversion"
        xmp_label = "Imported XMP · Film off"

    film_params = film_pipeline.clean_params({
        **FILM_BASE,
        "output_recipe": "neutral_print_scan",
    })
    clean_scan_params = film_pipeline.clean_params({
        **FILM_BASE,
        "output_recipe": "clean_scan",
    })
    optical_print_params = film_pipeline.clean_params({
        **FILM_BASE,
        "output_recipe": "soft_optical_print",
    })

    with tempfile.TemporaryDirectory(prefix="lighttable-site-comparison-") as temp:
        linear_tiff = Path(temp) / "decoded-linear-prophoto.tif"
        tifffile.imwrite(linear_tiff, linear, photometric="rgb", metadata=None)
        film = render_rust(str(linear_tiff), film_params)
        clean_scan = render_rust(str(linear_tiff), clean_scan_params)
        optical_print = render_rust(str(linear_tiff), optical_print_params)

    images = {
        "neutral-frame.jpg": (neutral, WEB_LONG_EDGE, "Neutral RAW — Film off"),
        "imported-xmp-frame.jpg": (xmp_result, WEB_LONG_EDGE, xmp_label),
        "lighttable-film-frame.jpg": (
            film, WEB_LONG_EDGE, "Kodak Gold 200 — neutral print scan"
        ),
        "neutral-crop.jpg": (
            crop_image(neutral, crop), None, "Neutral RAW — 100% crop"
        ),
        "imported-xmp-crop.jpg": (
            crop_image(xmp_result, crop), None, f"{xmp_label} — 100% crop"
        ),
        "lighttable-film-crop.jpg": (
            crop_image(film, crop), None,
            "Kodak Gold 200 — 100% physical-grain crop",
        ),
        "clean-scan-frame.jpg": (
            clean_scan, WEB_LONG_EDGE, "Modeled clean scan"
        ),
        "soft-optical-print-frame.jpg": (
            optical_print, WEB_LONG_EDGE, "Modeled soft optical print and scan"
        ),
    }
    records = []
    for filename, (image, long_edge, role) in images.items():
        destination = output_dir / filename
        save_web(image, destination, long_edge=long_edge)
        records.append(output_record(destination, role))

    source_metadata = dict(DEFAULT_SOURCE_METADATA)
    if raw_path != DEFAULT_RAW.resolve():
        source_metadata = {
            "description": args.source_description or raw_path.stem,
            "sourceId": args.source_id or "user-supplied",
            "sourceUrl": args.source_url,
            "license": args.license or "user-supplied; verify before publishing",
            "licenseUrl": args.license_url,
        }

    manifest = {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "lighttableRevision": git_revision(),
        "claimBoundary": (
            "The imported-XMP files are rendered by LightTable unless "
            "xmpResult.kind is adobe-lightroom-export. Different editors have "
            "different render engines, so converted settings are approximate."
        ),
        "source": {
            "file": relative_name(raw_path),
            "sha256": sha256(raw_path),
            "decodedWidth": width,
            "decodedHeight": height,
            **source_metadata,
        },
        "xmpResult": {
            "kind": xmp_kind,
            "label": xmp_label,
            "presetFile": relative_name(xmp_path),
            "presetSha256": sha256(xmp_path),
            "mappedControls": preset["conversion"]["mapped"],
            "ignoredOperations": preset["conversion"]["ignored"],
            "notes": preset["conversion"]["notes"],
            "adobeReference": (
                {
                    "file": relative_name(adobe_reference),
                    "sha256": sha256(adobe_reference),
                } if adobe_reference else None
            ),
        },
        "filmResult": {
            "renderer": "spektrafilm-rs through LightTable",
            "profileCatalogDigest": film_pipeline.PROFILE_CATALOG_DIGEST,
            "comparisonParams": film_params,
            "cleanScanParams": clean_scan_params,
            "softOpticalPrintParams": optical_print_params,
            "effectiveGrainAreaUm2": film_pipeline.effective_grain_area_um2(
                film_params
            ),
        },
        "crop": {
            **crop,
            "scale": "one output pixel per decoded RAW pixel",
        },
        "outputs": records,
    }
    manifest_path = output_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"Built {len(records)} comparison images in {output_dir}")
    print(f"Crop: {crop['x']},{crop['y']},{crop['width']} ({width}x{height} source)")
    return manifest


def check(args: argparse.Namespace) -> None:
    manifest_path = args.output.resolve() / MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"comparison manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text())
    expected_source = args.raw.resolve()
    expected_xmp = args.xmp.resolve()
    errors = []
    if manifest.get("source", {}).get("sha256") != sha256(expected_source):
        errors.append("source RAW hash differs from manifest")
    if manifest.get("xmpResult", {}).get("presetSha256") != sha256(expected_xmp):
        errors.append("XMP preset hash differs from manifest")
    if manifest.get("xmpResult", {}).get("kind") not in {
        "lighttable-xmp-conversion", "adobe-lightroom-export"
    }:
        errors.append("XMP result has an unknown provenance kind")
    for record in manifest.get("outputs", []):
        path = args.output.resolve() / record["file"]
        if not path.is_file():
            errors.append(f"missing output: {record['file']}")
            continue
        if sha256(path) != record.get("sha256"):
            errors.append(f"hash mismatch: {record['file']}")
        with Image.open(path) as image:
            if list(image.size) != [record.get("width"), record.get("height")]:
                errors.append(f"dimension mismatch: {record['file']}")
    if len(manifest.get("outputs", [])) != 8:
        errors.append("manifest must contain exactly eight comparison images")
    if errors:
        raise SystemExit("Comparison check failed:\n- " + "\n- ".join(errors))
    print(f"OK: verified {len(manifest['outputs'])} images and their provenance")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    result.add_argument("--xmp", type=Path, default=DEFAULT_XMP)
    result.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        required=DEFAULT_OUTPUT is None,
        help=(
            "canonical lighttable-site assets/film-comparison directory; "
            "defaults from LIGHTTABLE_SITE_ROOT"
        ),
    )
    result.add_argument("--adobe-reference", type=Path)
    result.add_argument("--crop", help="x,y,size in decoded-image pixels")
    result.add_argument("--source-description")
    result.add_argument("--source-id")
    result.add_argument("--source-url")
    result.add_argument("--license")
    result.add_argument("--license-url")
    result.add_argument("--check", action="store_true")
    return result


if __name__ == "__main__":
    arguments = parser().parse_args()
    check(arguments) if arguments.check else build(arguments)
