"""Real export files, checked against analytical and separate-code references.

The no-film CLI checks use only NumPy equations for sRGB, exposure and geometry.
Resident postprocessing is isolated by comparing Rust grade/masks with Python
grade/edits on an ungraded float film render. That baseline is NOT a film oracle;
the film stage gate owns independent validation of the film model itself.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import numpy as np
from PIL import Image, ImageCms
import tifffile

from processing_support import compare_images, file_digest, grade_cases, target_rgb8

APP = Path(__file__).resolve().parents[1]
FLOAT_TOLERANCE = {"mean": 0.05, "p95": 0.1, "max": 0.5}
RGB16_TOLERANCE = {"mean": 0.0021, "p95": 0.0021, "max": 0.0041}


def srgb_encode(linear):
    linear = np.clip(np.asarray(linear, dtype=np.float64), 0, 1)
    return np.where(linear <= 0.0031308, linear * 12.92,
                    1.055 * np.power(linear, 1 / 2.4) - 0.055)


def srgb_decode(encoded):
    encoded = np.clip(np.asarray(encoded, dtype=np.float64), 0, 1)
    return np.where(encoded <= 0.04045, encoded / 12.92,
                    np.power((encoded + 0.055) / 1.055, 2.4))


def analytical_exposure(encoded, stops):
    return srgb_encode(srgb_decode(encoded) * 2 ** stops)


def reference_resize(image, long_edge):
    """Separable, antialiased Lanczos-3; no application/Pillow resize calls."""
    image = np.asarray(image, dtype=np.float64)
    height, width = image.shape[:2]
    if not long_edge or max(height, width) <= long_edge:
        return image.copy()
    scale = long_edge / max(height, width)
    output_width, output_height = max(1, round(width * scale)), max(1, round(height * scale))

    def weights(source, destination):
        ratio = source / destination
        kernel_scale = max(1, ratio)
        centers = (np.arange(destination) + 0.5) * ratio
        distance = ((np.arange(source)[None, :] + 0.5) - centers[:, None]) / kernel_scale
        kernel = np.sinc(distance) * np.sinc(distance / 3)
        kernel[np.abs(distance) >= 3] = 0
        return kernel / kernel.sum(axis=1, keepdims=True)

    horizontal = np.einsum("hsc,ws->hwc", image, weights(width, output_width))
    result = np.einsum("shc,ys->yhc", horizontal, weights(height, output_height))
    return np.clip(result, 0, 1)


def reference_geometry(image, *, rotate=0, crop=None, long_edge=None):
    result = np.rot90(image, (-round(rotate / 90)) % 4)
    if crop:
        height, width = result.shape[:2]
        x, y = round(crop["x"] * width), round(crop["y"] * height)
        result = result[y:min(height, y + max(1, round(crop["h"] * height))),
                        x:min(width, x + max(1, round(crop["w"] * width)))]
    return reference_resize(result, long_edge)


def _check(name, condition, **details):
    return {"name": name, "status": "pass" if condition else "fail", **details}


def _subprocess_environment():
    # The CLI runs in a fresh interpreter. CI prepares the pinned runtime in
    # vendor rather than installing it, so the parent's sys.path is insufficient.
    pythonpath = os.pathsep.join(filter(None, [
        str(APP), str(APP / "vendor" / "spektrafilm" / "src"),
        os.environ.get("PYTHONPATH"),
    ]))
    return dict(os.environ, SPEKTRAFILM_BACKEND="cpu", NUMBA_NUM_THREADS="2",
                RAYON_NUM_THREADS="2", PYTHONHASHSEED="0", PYTHONPATH=pythonpath)


def _read_export(path):
    if path.suffix == ".tif":
        with tifffile.TiffFile(path) as document:
            pixels = document.asarray()
            tag = document.pages[0].tags.get(34675)
            profile = bytes(tag.value) if tag else b""
    else:
        with Image.open(path) as document:
            pixels = np.asarray(document.convert("RGB"))
            profile = document.info.get("icc_profile", b"")
    floating = pixels.astype(np.float64)
    if np.issubdtype(pixels.dtype, np.integer):
        floating /= np.iinfo(pixels.dtype).max
    return pixels, floating, profile


def _profile_record(name, profile):
    try:
        parsed = ImageCms.ImageCmsProfile(BytesIO(profile))
        description = ImageCms.getProfileDescription(parsed).strip()
        valid = (profile[36:40] == b"acsp" and profile[16:20] == b"RGB "
                 and "srgb" in description.lower())
    except (OSError, ValueError) as error:
        description, valid = str(error), False
    return _check(name, valid, description=description, bytes=len(profile),
                  sha256=hashlib.sha256(profile).hexdigest())


def run_cli(output_dir: Path) -> list[dict]:
    """Exercise render_cli.py as an actual worker, including its final encoder."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # More than 256 input codes per channel catches hidden 8-bit intermediates.
    pixels16 = np.round(np.linspace(0, 1, 96 * 128 * 3).reshape(96, 128, 3)
                        * 65535).astype(np.uint16)
    pixels16[24:48, 0:32] = (53001, 9123, 2701)
    source = output_dir / "rgb16-source.tif"
    tifffile.imwrite(source, pixels16, photometric="rgb", metadata=None)
    source_float = pixels16.astype(np.float64) / 65535
    crop = {"x": 0.135, "y": 0.217, "w": 0.617, "h": 0.571}
    cases = [
        {"name": "identity-rgb16"},
        {"name": "linear-srgb-transfer", "linear": True},
        {"name": "exposure-positive", "exposure": 0.7},
        {"name": "exposure-negative", "exposure": -1.25},
        {"name": "rotate-crop", "rotate": 90, "crop": crop},
        {"name": "exposure-rotate-crop-resize", "exposure": 0.65,
         "rotate": 270, "crop": crop, "long_edge": 47},
        {"name": "png-encoding", "format": "png", "exposure": -0.35},
        {"name": "tiff-eight-bit", "bit_depth": 8},
    ]
    records = []
    for case in cases:
        name = "cli-" + case["name"]
        destination = output_dir / (name + (".png" if case.get("format") == "png" else ".tif"))
        job = {"params": {"profile_enabled": False,
                          "linear_input": case.get("linear", False),
                          "rotate": case.get("rotate", 0)},
               "grade": {"exposure": case.get("exposure", 0)},
               "crop": case.get("crop"), "longEdge": case.get("long_edge"),
               "format": case.get("format", "tif"),
               "bitDepth": case.get("bit_depth", 16), "outputSpace": "srgb",
               "metadata": "none", "watermark": {"enabled": False}}
        job_path = output_dir / (name + ".json")
        job_path.write_text(json.dumps(job, indent=2))
        expected = srgb_encode(source_float) if case.get("linear") else source_float
        if case.get("exposure"):
            expected = analytical_exposure(expected, case["exposure"])
        expected = reference_geometry(expected, rotate=case.get("rotate", 0),
                                      crop=case.get("crop"), long_edge=case.get("long_edge"))
        command = [sys.executable, str(APP / "render_cli.py"), str(source),
                   str(destination), str(job_path)]
        try:
            result = subprocess.run(command, cwd=APP, env=_subprocess_environment(),
                                    capture_output=True, text=True, timeout=120, check=True)
            response = json.loads(result.stdout.splitlines()[-1])
            raw, actual, profile = _read_export(destination)
            # Rounding to 8-bit codes permits at most half a code per sample;
            # this is a quantization bound, independent of fixture statistics.
            tolerance = ({"mean": 0.501, "p95": 0.501, "max": 0.501}
                         if raw.dtype == np.uint8 else RGB16_TOLERANCE)
            record = compare_images(name, expected, actual, output_dir, tolerance)
            record.update(reference="independent NumPy sRGB/exposure/Lanczos equations",
                          command=command, output=str(destination.resolve()),
                          output_sha256=file_digest(destination), dtype=str(raw.dtype))
            records.append(record)
            expected_dtype = np.uint8 if (case.get("format") == "png" or case.get("bit_depth") == 8) else np.uint16
            records.append(_check(name + "-format", raw.dtype == expected_dtype
                                  and response.get("ok") is True
                                  and (response.get("height"), response.get("width")) == actual.shape[:2],
                                  dtype=str(raw.dtype), shape=list(raw.shape)))
            records.append(_profile_record(name + "-icc", profile))
            if case["name"] == "identity-rgb16":
                records.append(_check(name + "-precision", np.array_equal(raw, pixels16)
                                      and np.unique(raw[..., 0]).size > 256,
                                      unique_red_codes=int(np.unique(raw[..., 0]).size),
                                      reference="exact original RGB16 codes"))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            detail = getattr(error, "stderr", "") or str(error)
            records.append(_check(name, False, error=str(detail)[-3000:], command=command))
    return records


@contextmanager
def resident(binary, log_path):
    """One bounded resident worker; stdout reader avoids platform select limits."""
    with open(log_path, "w") as log:
        process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=log, text=True,
                                   bufsize=1, env=_subprocess_environment(), cwd=APP)
        lines = queue.Queue()
        def read_lines():
            for line in process.stdout:
                lines.put(line)
            lines.put(None)
        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start()
        request_id = 0
        def request(payload):
            nonlocal request_id
            request_id += 1
            process.stdin.write(json.dumps(dict(payload, id=request_id)) + "\n")
            process.stdin.flush()
            try:
                line = lines.get(timeout=120)
            except queue.Empty as error:
                raise TimeoutError("resident render exceeded 120 seconds") from error
            if not line:
                raise RuntimeError(f"resident exited; see {log_path}")
            response = json.loads(line)
            if response.get("id") != request_id or not response.get("ok"):
                raise RuntimeError(f"resident render failed: {response}")
            return response
        try:
            yield request
        finally:
            if process.stdin:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            reader.join(timeout=2)
            if process.stdout:
                process.stdout.close()


def mask_cases():
    bitmap = np.arange(12 * 16, dtype=np.uint8).reshape(12, 16)
    return [
        {"name": "linear-mask", "masks": [{"type": "linear", "start": [.12, .3],
         "end": [.81, .7], "opacity": .7, "grade": {"exposure": .65}}]},
        {"name": "radial-mask", "masks": [{"type": "radial", "center": [.37, .62],
         "radius": .32, "feather": .6, "grade": {"saturation": -.6, "temp": .2}}]},
        {"name": "mask-components-ranges", "masks": [{"opacity": .8,
         "lumaLow": .12, "lumaHigh": .83, "colorHue": 35, "colorRange": 50,
         "colorAmount": .5, "grade": {"exposure": -.4, "contrast": .2},
         "components": [{"type": "linear", "start": [.1, .1], "end": [.8, .9]},
                        {"type": "radial", "center": [.4, .6], "radius": .3,
                         "feather": .5, "combine": "subtract"}]}]},
        {"name": "bitmap-mask", "masks": [{"type": "subject", "opacity": .65,
         "bitmap": {"width": 16, "height": 12,
                    "data": base64.b64encode(bitmap.tobytes()).decode()},
         "grade": {"exposure": .5}}]},
        {"name": "global-before-local", "grade": {"contrast": .5, "exposure": .3},
         "masks": [{"type": "linear", "start": [.1, .4], "end": [.9, .6],
                    "invert": True, "grade": {"exposure": -.8, "saturation": -.4}}]},
    ]


def ensure_resident(output_dir: Path):
    """Build this checkout, with an isolated Cargo target and a provenance record."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = APP / "build" / "processing-cargo"
    command = ["cargo", "build", "--manifest-path", str(APP / "rust-engine" / "Cargo.toml"), "--locked"]
    build = subprocess.run(command, cwd=APP, env=dict(os.environ, CARGO_TARGET_DIR=str(target)),
                           capture_output=True, text=True, timeout=900)
    (output_dir / "build.log").write_text(build.stdout + build.stderr)
    if build.returncode:
        return None, _check("resident-source-build", False, error=build.stderr[-3000:], command=command)
    binary = target / "debug" / ("lighttable-engine.exe" if os.name == "nt" else "lighttable-engine")
    return binary, _check("resident-source-build", binary.is_file(), command=command,
                          binary=str(binary), sha256=file_digest(binary))


def run_resident(output_dir: Path) -> list[dict]:
    import edits
    import film_pipeline as film
    import grade

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    binary, build_record = ensure_resident(output_dir)
    records = [build_record]
    if binary is None:
        return records
    source = output_dir / "resident-source.tif"
    tifffile.imwrite(source, target_rgb8().astype(np.uint16) * 257, photometric="rgb", metadata=None)
    params = film.clean_params({"input_color_space": "sRGB", "linear_input": False,
                                "workflow_mode": "creative", "auto_exposure": False,
                                "grain_on": False, "halation_on": False, "glare_on": False,
                                "couplers_on": False, "scan_sharpen": False,
                                "output_recipe": "clean_scan"})
    base = {"input": str(source), "data_dir": str(APP / "engine" / "data"),
            "film": params["stock"], "paper": params["paper"],
            "params": film.rust_params_json(params), "bit_depth": 32}
    with resident(binary, output_dir / "resident.log") as render:
        baseline_path = output_dir / "film-baseline-float.tif"
        response = render(dict(base, output=str(baseline_path)))
        baseline = tifffile.imread(baseline_path)
        records.append(_check("resident-float-intermediate", baseline.dtype == np.float32
                              and np.isfinite(baseline).all()
                              and np.unique(baseline[..., 0]).size > 256
                              and "cpu" in response["backend"].lower(),
                              dtype=str(baseline.dtype), backend=response["backend"],
                              unique_red_codes=int(np.unique(baseline[..., 0]).size)))
        for case in grade_cases() + mask_cases():
            name = "resident-" + case["name"]
            path = output_dir / (name + ".tif")
            request = dict(base, output=str(path), grade=grade.clean(case.get("grade", {})))
            if case.get("masks"):
                request["masks"] = edits.clean_masks(case["masks"])
            expected = grade.apply(baseline.copy(), case.get("grade", {}))
            expected = edits.apply_masks(expected, case.get("masks"))
            try:
                response = render(request)
                actual = tifffile.imread(path)
                record = compare_images(name, expected, actual, output_dir, FLOAT_TOLERANCE)
                record.update(reference="Python grade.py/edits.py on identical ungraded float film output",
                              backend=response["backend"], output=str(path), recipe=case)
                records.append(record)
            except (OSError, ValueError, RuntimeError, TimeoutError) as error:
                records.append(_check(name, False, error=str(error)))
        geometry = {"rotate": 90, "crop": {"x": .13, "y": .17, "w": .63, "h": .59},
                    "long_edge": 43}
        path = output_dir / "resident-grade-rotate-crop-resize-rgb16.tif"
        response = render(dict(base, output=str(path), bit_depth=16, rotate_quarters_ccw=3,
                               grade={"exposure": .65}, crop=geometry["crop"], long_edge=43))
        # Rotation is before grading. Exposure is pointwise, so this oracle
        # also verifies grade-before-resize (these operations do not commute).
        expected = reference_geometry(analytical_exposure(baseline, .65), **geometry)
        actual_raw = tifffile.imread(path)
        actual = actual_raw.astype(np.float64) / 65535
        records.append(compare_images("resident-grade-rotate-crop-resize-rgb16", expected,
                                      actual, output_dir, FLOAT_TOLERANCE))
        records.append(_check("resident-final-rgb16", actual_raw.dtype == np.uint16,
                              dtype=str(actual_raw.dtype), shape=list(actual_raw.shape)))
    return records


def run(output_dir: Path) -> dict:
    started = time.monotonic()
    output_dir = Path(output_dir).resolve()
    records = run_cli(output_dir / "cli")
    try:
        records.extend(run_resident(output_dir / "resident"))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, TimeoutError) as error:
        records.append(_check("resident-export-execution", False, error=str(error)))
    return {"records": records, "seconds": time.monotonic() - started,
            "scope": "Analytical CLI delivery plus independent resident grade/local-mask and geometry checks",
            "limitations": ["The resident ungraded film image is a postprocessing input, not a film truth reference.",
                            "JPEG/HEIF compression, RAW demosaicing and physical monitor calibration are outside this gate."]}
