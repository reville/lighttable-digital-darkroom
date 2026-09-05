#!/usr/bin/env python3
"""Evaluate the bundled denoiser on synthetic and real RAW crops.

The report separates measured scores from the required human contact-sheet
review. It intentionally refuses to run without the real helper and model.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from skimage.metrics import peak_signal_noise_ratio, structural_similarity

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import color_pipeline  # noqa: E402
import enhance_workflow  # noqa: E402
import media_formats  # noqa: E402


NOISE_LEVELS = (
    ("low", 180.0, 0.004),
    ("middle", 80.0, 0.009),
    ("high", 35.0, 0.016),
)


def add_poisson_gaussian(clean: np.ndarray, photons: float,
                         sigma: float, seed: int) -> np.ndarray:
    """Deterministic signal-dependent shot noise plus read noise."""
    rng = np.random.default_rng(seed)
    source = np.clip(np.asarray(clean, dtype=np.float32), 0.0, 1.0)
    shot = rng.poisson(source * photons).astype(np.float32) / photons
    read = rng.normal(0.0, sigma, source.shape).astype(np.float32)
    return np.clip(shot + read, 0.0, 1.0).astype(np.float32)


def image_metrics(candidate: np.ndarray, reference: np.ndarray) -> dict:
    candidate = np.clip(candidate, 0.0, 1.0).astype(np.float32)
    reference = np.clip(reference, 0.0, 1.0).astype(np.float32)
    return {
        "psnrDb": round(float(peak_signal_noise_ratio(
            reference, candidate, data_range=1.0)), 4),
        "ssim": round(float(structural_similarity(
            reference, candidate, channel_axis=2, data_range=1.0)), 6),
    }


def _lab(linear_prophoto: np.ndarray) -> np.ndarray:
    """Convert linear ProPhoto samples to CIE Lab D50 without a display curve."""
    matrix = np.asarray([
        [0.7976749, 0.1351917, 0.0313534],
        [0.2880402, 0.7118741, 0.0000857],
        [0.0000000, 0.0000000, 0.8252100],
    ], dtype=np.float64)
    white = np.asarray([0.96422, 1.0, 0.82521], dtype=np.float64)
    xyz = np.asarray(linear_prophoto, dtype=np.float64) @ matrix.T
    value = xyz / white
    delta = 6 / 29
    f = np.where(value > delta ** 3, np.cbrt(value),
                 value / (3 * delta ** 2) + 4 / 29)
    return np.stack((116 * f[..., 1] - 16,
                     500 * (f[..., 0] - f[..., 1]),
                     200 * (f[..., 1] - f[..., 2])), axis=-1)


def flat_patch_delta_e76(candidate: np.ndarray, reference: np.ndarray,
                         *, divisions: tuple[int, int] = (4, 6)) -> float:
    """Mean patch-colour drift over the flattest half of a regular grid."""
    height, width = reference.shape[:2]
    patches = []
    for row in range(divisions[0]):
        for column in range(divisions[1]):
            y0, y1 = row * height // divisions[0], (row + 1) * height // divisions[0]
            x0, x1 = column * width // divisions[1], (column + 1) * width // divisions[1]
            ref = reference[y0:y1, x0:x1]
            out = candidate[y0:y1, x0:x1]
            patches.append((float(np.mean(np.var(ref, axis=(0, 1)))),
                            np.mean(ref, axis=(0, 1)),
                            np.mean(out, axis=(0, 1))))
    # Near-black or clipped patches turn symmetric synthetic noise into a
    # one-sided colour bias. They measure clipping, not denoiser hue drift,
    # so the colour gate uses only patches with headroom in every channel.
    eligible = [item for item in patches
                if np.all(item[1] > 0.03) and np.all(item[1] < 0.97)]
    pool = eligible or patches
    pool.sort(key=lambda item: item[0])
    chosen = pool[:max(1, min(len(pool), len(patches) // 2))]
    ref_lab = _lab(np.asarray([item[1] for item in chosen]))
    out_lab = _lab(np.asarray([item[2] for item in chosen]))
    return float(np.mean(np.linalg.norm(out_lab - ref_lab, axis=1)))


def _model(image: np.ndarray, encoding: str, strength: float = 1.0) -> np.ndarray:
    if encoding == "gamma":
        return enhance_workflow.denoise_linear_prophoto(
            image, strength=strength)
    result = enhance_workflow.run_model(image, "denoise", strength=strength)
    return enhance_workflow.preserve_low_frequency_color(image, result)


def _decode(path: Path, max_edge: int) -> np.ndarray:
    # FBDD full is the repository's existing sensor-stage baseline. Synthetic
    # noise is added after it, then learned denoise is measured against the
    # untouched FBDD decode.
    raw = color_pipeline.decode_raw(path, {
        "raw_sensor_denoise": "full", "learned_denoise": False,
        "developProfile": "linear",
    }, max_width=max_edge, apply_learned_denoise=False)
    return color_pipeline.resize_float_width(
        color_pipeline.as_float_rgb(raw), max_edge)


def _iso(path: Path) -> int:
    try:
        import exiv2
        image = exiv2.ImageFactory.open(str(path)); image.readMetadata()
        data = image.exifData()
        item = data.findKey(exiv2.ExifKey("Exif.Photo.ISOSpeedRatings"))
        return int(float(item.toString())) if item != data.end() else 0
    except Exception:
        return 0


def _raw_files(root: Path) -> list[Path]:
    return sorted(path for path in root.iterdir()
                  if path.is_file()
                  and path.suffix.lower() in media_formats.RAW_EXTENSIONS)


def _contact_sheet(rows: list[tuple[str, list[np.ndarray]]],
                   destination: Path) -> None:
    if not rows:
        return
    cell = 360
    labels = ("Original", "Strength 0.3", "Strength 0.6", "Strength 1.0")
    sheet = Image.new("RGB", (cell * 4, (cell + 42) * len(rows)), "#181818")
    draw = ImageDraw.Draw(sheet)
    for row, (name, images) in enumerate(rows):
        top = row * (cell + 42)
        for column, (label, value) in enumerate(zip(labels, images)):
            display = color_pipeline.linear_prophoto_to_display_srgb(
                value, develop_profile="linear")
            height, width = display.shape[:2]
            side = min(height, width, 768)
            y0, x0 = (height - side) // 2, (width - side) // 2
            crop = display[y0:y0 + side, x0:x0 + side]
            tile = Image.fromarray((crop * 255 + 0.5).astype(np.uint8))
            tile = tile.resize((cell, cell), Image.Resampling.LANCZOS)
            sheet.paste(tile, (column * cell, top))
            draw.text((column * cell + 8, top + cell + 7), label, fill="white")
        draw.text((8, top + cell + 24), name, fill="#aaaaaa")
    destination.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(destination, quality=92)


def evaluate(raw_root: Path, output: Path, *, synthetic_limit: int = 3,
             real_limit: int = 3, max_edge: int = 1600) -> dict:
    capability = enhance_workflow.capabilities()
    if not capability["modes"].get("denoise"):
        raise RuntimeError(capability["reason"] or "denoise is unavailable")
    files = _raw_files(raw_root)
    if not files:
        raise ValueError(f"no RAW files found in {raw_root}")
    output.mkdir(parents=True, exist_ok=True)
    measurements = []
    first_seconds = warm_seconds = None
    for file_index, path in enumerate(files[:max(1, synthetic_limit)]):
        clean = _decode(path, max_edge)
        megapixels = clean.shape[0] * clean.shape[1] / 1_000_000
        for level_index, (level, photons, sigma) in enumerate(NOISE_LEVELS):
            noisy = add_poisson_gaussian(
                clean, photons, sigma, seed=file_index * 100 + level_index)
            baseline = image_metrics(noisy, clean)
            for encoding in ("linear", "gamma"):
                started = time.perf_counter()
                denoised = _model(noisy, encoding)
                elapsed = time.perf_counter() - started
                if first_seconds is None:
                    first_seconds = elapsed / megapixels
                else:
                    warm_seconds = elapsed / megapixels if warm_seconds is None \
                        else min(warm_seconds, elapsed / megapixels)
                metrics = image_metrics(denoised, clean)
                measurements.append({
                    "file": path.name, "level": level, "encoding": encoding,
                    "sensorBaseline": "FBDD full before synthetic noise",
                    "baseline": baseline, "denoised": metrics,
                    "gainDb": round(metrics["psnrDb"] - baseline["psnrDb"], 4),
                    "meanFlatPatchDeltaE76": round(
                        flat_patch_delta_e76(denoised, clean), 4),
                    "secondsPerMegapixel": round(elapsed / megapixels, 4),
                })

    ranked = sorted(files, key=lambda path: (_iso(path), path.name), reverse=True)
    contact_rows = []
    chosen_encoding = max(
        ("linear", "gamma"),
        key=lambda encoding: np.mean([row["denoised"]["psnrDb"]
            for row in measurements if row["level"] == "middle"
            and row["encoding"] == encoding]))
    for path in ranked[:max(1, real_limit)]:
        original = _decode(path, max_edge)
        contact_rows.append((f"{path.name} · ISO {_iso(path) or 'unknown'}", [
            original, *[_model(original, chosen_encoding, strength)
                        for strength in (0.3, 0.6, 1.0)],
        ]))
    contact = output / "contact-sheet.jpg"
    _contact_sheet(contact_rows, contact)

    middle = [row for row in measurements
              if row["level"] == "middle"
              and row["encoding"] == chosen_encoding]
    gain = float(np.mean([row["gainDb"] for row in middle]))
    delta_e = float(np.mean([row["meanFlatPatchDeltaE76"] for row in middle]))
    report = {
        "schema": 1, "model": capability["models"]["denoise"],
        "encodingChoice": chosen_encoding, "measurements": measurements,
        "timing": {"firstSecondsPerMegapixel": round(first_seconds or 0, 4),
                   "warmSecondsPerMegapixel": round(warm_seconds or 0, 4)},
        "contactSheet": str(contact),
        "shipGate": {
            "middleLevelGainDb": round(gain, 4),
            "meanFlatPatchDeltaE76": round(delta_e, 4),
            "measuredPass": gain >= 1.5 and delta_e < 1.0,
            "visualReviewRequired": True,
        },
    }
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, default=APP / "raw-test")
    parser.add_argument("--output", type=Path,
                        default=APP / "bench/results/denoise")
    parser.add_argument("--synthetic-limit", type=int, default=3)
    parser.add_argument("--real-limit", type=int, default=3)
    parser.add_argument("--max-edge", type=int, default=1600)
    args = parser.parse_args()
    report = evaluate(args.raw_root, args.output,
                      synthetic_limit=max(1, args.synthetic_limit),
                      real_limit=max(1, args.real_limit),
                      max_edge=max(512, args.max_edge))
    print(json.dumps(report["shipGate"], indent=2))


if __name__ == "__main__":
    main()
