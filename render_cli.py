#!/usr/bin/env python
"""Full-resolution export worker: render one image through the pipeline,
then apply the same grade and crop the preview showed.

Usage: render_cli.py <src_tiff> <dst> <job_json_file>
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

import film_pipeline as fp
import grade as grade_mod
import edits as edits_mod
import color_pipeline
import export_workflow
import platform_image

APP = Path(__file__).resolve().parent


def _creation_flags() -> dict:
    """Keep the console-subsystem engine hidden under a console-less parent."""
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


def render_rust(src: str, params: dict) -> np.ndarray:
    """Run the Rust engine for full-resolution export."""
    cp = fp.clean_params(params)
    engine_root = Path(__file__).resolve().parent / "engine"
    binary = engine_root / (
        "spektrafilm-rs.exe" if os.name == "nt" else "spektrafilm-rs"
    )
    if not binary.is_file():
        raise RuntimeError(f"Rust film engine not found at {binary}")
    with tempfile.TemporaryDirectory(prefix="lighttable-export-") as temp:
        temp_path = Path(temp)
        params_path = temp_path / "params.json"
        # The one-shot Rust binary supports RGB16 TIFF. PNG would collapse
        # this fallback to eight bits before the grade and final TIFF encoder.
        output_path = temp_path / "render.tif"
        params_path.write_text(json.dumps(fp.rust_params_json(cp)))
        command = [
            str(binary), "process", src, "-o", str(output_path),
            "--film", cp["stock"], "--data-dir", str(engine_root / "data"),
            "--params", str(params_path),
        ]
        if cp["stock"] in fp.POSITIVE_STOCKS:
            command.append("--scan-film")
        else:
            command += ["--paper", cp["paper"]]
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=1800, **_creation_flags())
        if result.returncode != 0 or not output_path.is_file():
            detail = (result.stderr or result.stdout).strip()[-500:]
            raise RuntimeError(f"Rust film render failed: {detail}")
        return color_pipeline.load_float_rgb(output_path)


def main():
    src, dst, job_file = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(job_file) as f:
        job = json.load(f)
    edits_mod.require_saved_mask_assets(job.get("masks"))
    params = job.get("params", {})
    g = job.get("grade") or {}
    crop = job.get("crop")
    rotate = float(fp.clean_params(params)["rotate"])
    fmt = job.get("format", "jpeg")
    quality = int(job.get("quality", 92))
    long_edge = job.get("longEdge")
    warnings = job.setdefault("warnings", [])
    color_pipeline.required_icc_bytes(job.get("outputSpace", "srgb"))

    t0 = time.time()
    cp = fp.clean_params(params)
    input_space = color_pipeline.normalise_output_space(job.get("inputColorSpace"))
    if input_space != "srgb" and (cp["profile_enabled"] or cp["linear_input"]
                                  or not color_pipeline.wide_develop_edits_supported(job)):
        raise ValueError("Wide-gamut input cannot be used with sRGB color adjustments")
    if (input_space == "srgb"
            and color_pipeline.normalise_output_space(job.get("outputSpace")) != "srgb"
            and color_pipeline.SRGB_LIMITED_EXPORT_WARNING not in warnings):
        warnings.append(color_pipeline.SRGB_LIMITED_EXPORT_WARNING)
    use_rust = (job.get("engine") == "rs"
                or fp.profile_requires_rust(cp["stock"]))
    if cp["profile_enabled"] and use_rust:
        out = render_rust(src, cp)
    else:
        image = fp.load_linear(src, max_width=None)
        out = fp.render_float(image, cp)

    k = (-int(round(rotate / 90))) % 4
    if k:
        out = np.ascontiguousarray(np.rot90(out, k))

    out = edits_mod.apply_base(
        out.astype(np.float32), job.get("optics"), job.get("heals"),
        job.get("lensProfile"))
    if not grade_mod.is_identity(g):
        out = np.clip(grade_mod.apply(out.astype(np.float32), g),
                      0, 1).astype(np.float32)
    out = edits_mod.apply_masks(out, job.get("masks"))

    if crop:
        h, w = out.shape[:2]
        x0 = int(round(crop["x"] * w)); y0 = int(round(crop["y"] * h))
        x1 = min(w, x0 + max(1, int(round(crop["w"] * w))))
        y1 = min(h, y0 + max(1, int(round(crop["h"] * h))))
        out = np.ascontiguousarray(out[y0:y1, x0:x1])

    out = color_pipeline.resize_float(out, long_edge)
    # Kept in step with finish_export() in server.py: watermark after resize,
    # metadata after encode. The two paths must produce the same file.
    out = export_workflow.apply_watermark(out, job.get("watermark"), APP)
    metadata_policy = str(job.get("metadata", "all-except-location"))
    # Catalog exports name the original capture explicitly because ``src``
    # is usually an intermediate render TIFF; external edits pass the capture
    # itself as ``src`` and ask for it with ``copyMetadataFrom``.
    metadata_source = (job.get("metadataSource")
                       or (src if job.get("copyMetadataFrom") else None))
    is_heif = str(fmt).lower() in ("heif", "heic")
    width, height = color_pipeline.save_export_image(
        out, dst, fmt=fmt, quality=quality,
        output_space=str(job.get("outputSpace", "srgb")),
        input_space=input_space,
        bit_depth=int(job.get("bitDepth", 16)),
        metadata_source=metadata_source if is_heif else None,
        metadata_policy=metadata_policy if is_heif else "none",
        metadata_fields=(job.get("metadataFields") or {}) if is_heif else None,
        warnings=warnings)
    if metadata_policy != "none" and not is_heif:
        before = len(warnings)
        succeeded = platform_image.write_metadata(
            dst, metadata_source, metadata_policy,
            job.get("metadataFields") or {}, warnings=warnings)
        if not succeeded and len(warnings) == before:
            warnings.append("Requested metadata could not be saved.")
    print(json.dumps({"ok": True, "path": str(dst), "width": width,
                      "height": height, "seconds": time.time() - t0,
                      "warnings": warnings}))


if __name__ == "__main__":
    main()
