"""Numerical film-processing checks against independent Python and NumPy oracles.

The Python spectral implementation is an independent implementation of the same
model, sharing the measured stock data. Agreement is not validation against a
physical film scan. Stage units are log10 exposure or optical density, never RGB
code values. The CLI is forced to CPU for its diagnostic stage buffers; GPU
resident processing is checked separately at its actual final output.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import numpy as np

APP = Path(__file__).resolve().parents[1]
STAGES = {
    "film_exposure": "SPEKTRAFILM_DUMP_FILM_LOG_RAW",
    "film_development": "SPEKTRAFILM_DUMP_FILM_DENSITY",
    "print_exposure": "SPEKTRAFILM_DUMP_PRINT_LOG_RAW",
    "print_development": "SPEKTRAFILM_DUMP_PRINT_DENSITY",
}
BASE_PARAMS = {
    "stock": "kodak_portra_400", "paper": "kodak_portra_endura",
    "workflow_mode": "creative", "linear_input": True,
    "auto_exposure": False, "grain_on": False, "halation_on": False,
    "glare_on": False, "couplers_on": False,
    "camera_diffusion_strength": 0.0, "scan_softness": 0.0,
    "scan_sharpen": False, "output_recipe": "clean_scan",
}
CASES = {
    "negative_baseline": {},
    "exposure_plus_one": {"exposure_ev": 1.0},
    "density_gamma": {"gamma": 1.2},
    "dye_couplers": {"couplers_on": True},
    "halation": {"halation_on": True, "halation_amount": 2.0},
    "halation_weak": {"stock": "kodak_gold_200", "halation_on": True, "halation_amount": 2.0},
    "halation_cine": {"stock": "kodak_vision3_250d", "halation_on": True, "halation_amount": 2.0},
    "camera_diffusion": {"camera_diffusion_strength": 0.75},
    "print_exposure": {"print_exposure": 1.6},
    "print_preflash": {"print_preflash": 0.08},
    "print_filters": {"print_y_filter_shift": 10.0, "print_m_filter_shift": -8.0},
    "scanner_blur": {"scan_softness": 0.8},
    "scanner_sharpen": {"scan_sharpen": True, "scan_sharpness": 1.5},
    "scanner_glare": {"glare_on": True, "glare_amount": 2.0},
    "reversal": {"stock": "fujifilm_velvia_100"},
}
BW_CASES = {
    "development_short": {"stock": "kodak_doublex", "paper": "kodak_2302", "development_time": 4.0},
    "development_long": {"stock": "kodak_doublex", "paper": "kodak_2302", "development_time": 12.0},
}


def target_linear(width=192, height=128):
    """Quantized ramps, grey steps, color patches, impulses, and edge detail."""
    x = np.linspace(0.003, 0.97, width)
    y = np.linspace(0.1, 1.0, height)[:, None]
    result = np.stack(np.broadcast_arrays(x * y, np.sqrt(x) * y,
                                          (1 - 0.8 * x) * y), axis=-1)
    patches = [(0, 0, 0), (.003, .003, .003), (.018, .018, .018),
               (.18, .18, .18), (.5, .5, .5), (1, 1, 1),
               (.8, .08, .04), (.04, .7, .1), (.05, .1, .85),
               (.75, .45, .27), (.12, .36, .22), (.1, .35, .75)]
    for index, color in enumerate(patches):
        left = index * width // len(patches)
        right = (index + 1) * width // len(patches)
        result[height // 8:height // 3, left:right] = color
    result[height // 2:3 * height // 4, :width // 3] = .004
    result[height // 2 + 2:3 * height // 4 - 2, width // 6] = 1
    result[0, 0] = (1, .9, .7)
    # Both oracles get EXACTLY the same uint16 TIFF input, including black.
    return np.rint(np.clip(result, 0, 1) * 65535).astype(np.uint16) / 65535.0


def difference_metrics(reference, actual):
    """Refuse invalid arrays before aggregating; a NaN may never pass a gate."""
    reference, actual = np.asarray(reference), np.asarray(actual)
    if reference.shape != actual.shape or reference.size == 0:
        raise ValueError(f"shape mismatch or empty arrays: {reference.shape} / {actual.shape}")
    if not np.all(np.isfinite(reference)) or not np.all(np.isfinite(actual)):
        raise ValueError("non-finite reference or actual processing output")
    error = np.abs(reference.astype(np.float64) - actual.astype(np.float64))
    return {"mean": float(error.mean()), "p95": float(np.percentile(error, 95)),
            "max": float(error.max())}


def compare_stage(name, reference, actual, outdir, *, tolerance, units):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    for label, value in (("reference", reference), ("actual", actual)):
        np.save(outdir / f"{label}.npy", value, allow_pickle=False)
    try:
        metrics = difference_metrics(reference, actual)
        passed = all(metrics[key] <= limit for key, limit in tolerance.items())
        error = None
    except ValueError as exc:
        metrics, passed, error = {}, False, str(exc)
    record = {"name": name, "status": "pass" if passed else "fail",
              "metrics": metrics, "tolerance": tolerance, "units": units,
              "artifacts": {"reference": str(outdir / "reference.npy"),
                            "actual": str(outdir / "actual.npy")}}
    if error:
        record["error"] = error
    return record


def numpy_develop(log_exposure, exposure_axis, density_curves, gamma=1.0):
    """Linear interpolation of measured characteristic curves, no engine code."""
    curves = np.asarray(density_curves, dtype=np.float64)
    curves = curves - np.nanmin(curves, axis=0)
    axis = np.asarray(exposure_axis, dtype=np.float64) / gamma
    out = np.empty_like(log_exposure, dtype=np.float64)
    for channel in range(3):
        valid = np.isfinite(curves[:, channel]) & np.isfinite(axis)
        if valid.sum() < 2 or np.any(np.diff(axis[valid]) <= 0):
            raise ValueError("density profile needs at least two ordered finite samples")
        out[..., channel] = np.interp(log_exposure[..., channel], axis[valid],
                                      curves[valid, channel])
    return out


def measured_curves(profile, requested_time=None):
    """Read/resolve the model's measured input, independently of either engine."""
    raw = json.loads(Path(profile).read_text())
    data = raw["data"]
    curves = np.asarray(data["density_curves"], dtype=np.float64)
    if raw["info"].get("channel_model") == "bw":
        times = np.asarray(data.get("development_time") or [0.0])
        index = ((len(times) - 1) // 2 if requested_time is None
                 else int(np.argmin(np.abs(times - requested_time))))
        curves = np.repeat(curves[:, index:index + 1], 3, axis=1)
    return np.asarray(data["log_exposure"], dtype=np.float64), curves


def grain_variance(density, density_max, density_min, particle_area, pixel_size,
                   uniformity, blur):
    """Poisson thinning variance, including the discrete Gaussian blur's L2 norm.

    If N~Poisson(n/s), X|N~Binomial(N,p), then X~Poisson(n*p/s).
    Grain density is X*(Dmax/n)*s. This derives its variance without calling
    either engine's RNG or grain code. Independent sublayer averaging cancels
    the corresponding particle-count division.
    """
    total_density = np.asarray(density) + np.asarray(density_min)
    maximum = np.asarray(density_max) + np.asarray(density_min)
    probability = np.clip(total_density / maximum, 1e-6, 1 - 1e-6)
    saturation = 1 - probability * np.asarray(uniformity) * (1 - 1e-6)
    particles = pixel_size ** 2 / np.asarray(particle_area)
    variance = probability * maximum ** 2 * saturation / particles
    if blur > .4:
        coordinates = np.arange(-int(3 * blur + .5), int(3 * blur + .5) + 1)
        kernel = np.exp(-.5 * (coordinates / blur) ** 2)
        kernel /= kernel.sum()
        variance *= np.sum(kernel ** 2) ** 2
    return variance


def grain_checks(output_dir, binary, gpu=False):
    """Grain uses moments and exact within-engine repeatability, not seed parity.

    Discrete Poisson/binomial samplers can consume different random streams
    after even a 1e-7 input perturbation. GPU also uses another sampler. A
    per-pixel cross-engine threshold would therefore test the seed stream,
    while these checks test the modeled mean, variance, and stable previews.
    """
    from PIL import Image
    import tifffile
    fp, digest_params, _ = _runtime()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = np.empty((128, 192, 3), dtype=np.uint16)
    for index, value in enumerate((.03, .18, .5)):
        target[:, index * 64:(index + 1) * 64] = round(value * 65535)
    source = output_dir / "flat-fields.tif"
    tifffile.imwrite(source, target, photometric="rgb")
    params = fp.clean_params(BASE_PARAMS | {"grain_on": True, "grain_amount": 20.0})
    digested = digest_params(fp.build_params(params))
    grain = digested.film_render.grain
    axis, curves = measured_curves(APP / "engine/data/profiles/kodak_portra_400.json")
    maxima = np.nanmax(curves - np.nanmin(curves, axis=0), axis=0)
    records = []
    # CPU diagnostic density is used for an absolute analytical variance gate.
    actual = cli_stages(source, params, output_dir / "cpu", binary=binary)
    repeat = cli_stages(source, params, output_dir / "cpu-repeat", binary=binary)
    for stage in ("film_development", "scan"):
        records.append(compare_stage(f"grain/{stage}/repeatability", actual[stage], repeat[stage],
                                     output_dir / f"repeat-{stage}",
                                     tolerance={"mean": 0, "p95": 0, "max": 0}, units="exact float values"))
    no_grain = numpy_develop(actual["film_exposure"], axis, curves)
    variance = grain_variance(no_grain, maxima, grain.density_min,
                              grain.particle_area_um2 * np.asarray(grain.particle_scale),
                              float(np.float32(35000 / 192)), grain.uniformity, grain.blur)
    unblurred_variance = grain_variance(no_grain, maxima, grain.density_min,
                                        grain.particle_area_um2 * np.asarray(grain.particle_scale),
                                        float(np.float32(35000 / 192)), grain.uniformity, 0)
    for field in range(3):
        # Exclude blur support near the flat-field boundary; these are uniform
        # fields, not a global image histogram that can hide missing grain.
        area = (slice(8, -8), slice(field * 64 + 8, (field + 1) * 64 - 8))
        residual = actual["film_development"][area] - no_grain[area]
        wanted = variance[area].mean(axis=(0, 1))
        measured = residual.var(axis=(0, 1))
        ratio = measured / wanted
        samples = residual.shape[0] * residual.shape[1]
        # Mean uncertainty uses preblur variance; blurring correlates pixels.
        mean_z = np.abs(residual.mean(axis=(0, 1))) / np.sqrt(unblurred_variance[area].mean(axis=(0, 1)) / samples)
        valid = bool(np.all(np.isfinite(ratio)) and np.all((ratio >= .7) & (ratio <= 1.3))
                     and np.all(mean_z <= 6))
        records.append({"name": f"grain/flat_field_{field}/analytical_moments",
                        "status": "pass" if valid else "fail", "units": "optical density",
                        "metrics": {"variance_ratio_rgb": ratio.tolist(), "mean_error_sigma_rgb": mean_z.tolist()},
                        "expected_variance_rgb": wanted.tolist(), "actual_variance_rgb": measured.tolist(),
                        "tolerance": {"variance_ratio": [.7, 1.3], "mean_error_sigma": 6},
                        "sample_count_per_channel": samples})
    Image.fromarray(np.rint(np.clip(actual["scan"], 0, 1) * 255).astype(np.uint8)).save(output_dir / "grain-preview.png")
    np.save(output_dir / "density-with-grain.npy", actual["film_development"], allow_pickle=False)
    np.save(output_dir / "density-no-grain-reference.npy", no_grain, allow_pickle=False)
    if gpu:
        gpu_actual = cli_stages(source, params, output_dir / "gpu", backend="wgpu", binary=binary)
        gpu_repeat = cli_stages(source, params, output_dir / "gpu-repeat", backend="wgpu", binary=binary)
        records.append(compare_stage("grain/gpu_scan/repeatability", gpu_actual["scan"], gpu_repeat["scan"],
                                     output_dir / "gpu-repeatability", tolerance={"mean": 0, "p95": 0, "max": 0}, units="exact float values"))
        # For GPU, where density taps are unavailable, compare flat-field scan
        # noise variance and mean to the verified CPU implementation.
        for field in range(3):
            area = (slice(8, -8), slice(field * 64 + 8, (field + 1) * 64 - 8))
            cpu, gpu_pixels = actual["scan"][area], gpu_actual["scan"][area]
            ratio = gpu_pixels.var(axis=(0, 1)) / cpu.var(axis=(0, 1))
            bias = np.abs(gpu_pixels.mean(axis=(0, 1)) - cpu.mean(axis=(0, 1))) * 255
            passed = bool(np.isfinite(ratio).all() and np.all((ratio >= .6) & (ratio <= 1.4)) and np.all(bias <= 1))
            records.append({"name": f"grain/gpu_flat_field_{field}/cpu_moments", "status": "pass" if passed else "fail",
                            "metrics": {"variance_ratio_rgb": ratio.tolist(), "mean_bias_255_rgb": bias.tolist()},
                            "tolerance": {"variance_ratio": [.6, 1.4], "mean_bias_255": 1}})
    return records


def _runtime():
    """Resolve this checkout's vendored oracle before any editable install."""
    source = APP / "vendor" / "spektrafilm" / "src"
    if not (source / "spektrafilm").is_dir():
        raise RuntimeError("missing vendor/spektrafilm/src Python reference runtime")
    sys.path.insert(0, str(source))
    sys.path.insert(0, str(APP))
    import film_pipeline as fp
    from spektrafilm.runtime.params_builder import digest_params
    from spektrafilm.runtime.pipeline import SimulationPipeline
    return fp, digest_params, SimulationPipeline


def python_stages(target, params):
    fp, digest_params, SimulationPipeline = _runtime()
    pipeline = SimulationPipeline(digest_params(fp.build_params(params)))
    outputs = {}
    value = pipeline.process(target, collect="log_e_film")
    outputs["film_exposure"] = np.asarray(value).copy()
    value = pipeline.process(value, inject="log_e_film", collect="cmy_film")
    outputs["film_development"] = np.asarray(value).copy()
    if not pipeline.io.scan_film:
        value = pipeline.process(value, inject="cmy_film", collect="log_e_print")
        outputs["print_exposure"] = np.asarray(value).copy()
        value = pipeline.process(value, inject="log_e_print", collect="cmy_print")
        outputs["print_development"] = np.asarray(value).copy()
        inject = "cmy_print"
    else:
        inject = "cmy_film"
    # This is also the clipping performed by LightTable's Python bridge,
    # film_pipeline.render_float, before display/export of scanner output.
    outputs["scan"] = np.clip(pipeline.process(value, inject=inject), 0, 1).copy()
    return outputs, pipeline


def cli_stages(source, params, outdir, *, backend="cpu", binary=None):
    """Run the product's resident JSON CLI; backend fallback cannot pass."""
    fp, _, _ = _runtime()
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    binary = Path(binary or APP / "build/processing-cargo/debug/lighttable-engine").resolve()
    data = APP / "engine" / "data"
    if not binary.is_file() or not data.is_dir():
        raise RuntimeError("requires current-source lighttable-engine build and engine/data; run setup/build first")
    output = outdir / "output.tif"
    # Reusing a report directory must never let yesterday's successful TIFF
    # or stage dump stand in for a missing result from today's engine.
    for artifact in [output, *(outdir / f"{stage}.f64" for stage in STAGES)]:
        artifact.unlink(missing_ok=True)
    request = {"id": 1, "input": str(source), "output": str(output),
               "bit_depth": 32, "data_dir": str(data), "film": params["stock"],
               "paper": params["paper"], "scan_film": params["stock"] in fp.POSITIVE_STOCKS,
               "params": fp.rust_params_json(params)}
    (outdir / "request.json").write_text(json.dumps(request, indent=2) + "\n")
    env = dict(os.environ, SPEKTRAFILM_BACKEND=backend, RAYON_NUM_THREADS="2")
    # Parent process diagnostic variables must not leak into unrelated runs.
    for name in STAGES.values():
        env.pop(name, None)
    if backend == "cpu":
        for stage, name in STAGES.items():
            env[name] = str(outdir / f"{stage}.f64")
    result = subprocess.run([str(binary)], input=json.dumps(request) + "\n",
                            capture_output=True, text=True, env=env, timeout=180)
    logs = result.stdout + "\n" + result.stderr
    (outdir / "cli.log").write_text(logs)
    if result.returncode:
        raise RuntimeError(f"CLI exited {result.returncode}: {logs[-1500:]}")
    responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    if len(responses) != 1 or responses[0].get("id") != 1 or not responses[0].get("ok"):
        raise RuntimeError(f"invalid/failed resident engine response: {responses}")
    reported_backend = responses[0].get("backend", "")
    if backend == "wgpu" and "wgpu" not in reported_backend.lower():
        raise RuntimeError(f"required wgpu unavailable: engine reported {reported_backend!r}")
    if backend == "cpu" and "cpu" not in reported_backend.lower():
        raise RuntimeError(f"required CPU backend: engine reported {reported_backend!r}")
    import tifffile
    shape = tifffile.imread(source).shape
    stages = {}
    if backend == "cpu":
        stages.update({stage: outdir / f"{stage}.f64" for stage in STAGES
                       if not stage.startswith("print_") or params["stock"] not in fp.POSITIVE_STOCKS})
    if not output.is_file():
        raise RuntimeError(f"missing required final scan TIFF: {output}")
    arrays = {"scan": tifffile.imread(output)}
    if arrays["scan"].shape != shape or arrays["scan"].dtype != np.float32:
        raise RuntimeError("expected full-size RGB float32 TIFF from resident engine")
    for stage, path in stages.items():
        if not path.is_file():
            raise RuntimeError(f"missing required {stage} diagnostic buffer: {path}")
        raw = np.fromfile(path, dtype="<f8")
        if raw.size != int(np.prod(shape)):
            raise RuntimeError(f"{stage}: expected {shape} f64 buffer, got {raw.size} elements")
        arrays[stage] = raw.reshape(shape)
    return arrays


def run(output_dir, *, cases=None, gpu=False, binary=None):
    """Run required real engines and return JSON-safe evidence and check records."""
    from PIL import Image
    import tifffile
    fp, _, _ = _runtime()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    records = []
    if binary is None:
        from processing_export import ensure_resident
        binary, build_record = ensure_resident(output_dir / "build")
        records.append(build_record)
        if binary is None:
            return {"name": "film_reference", "status": "fail", "records": records}
    elif not Path(binary).is_file():
        raise RuntimeError(f"explicit engine binary does not exist: {binary}")
    target = target_linear()
    source = output_dir / "target.tif"
    tifffile.imwrite(source, np.rint(target * 65535).astype(np.uint16), photometric="rgb")
    Image.fromarray(np.rint(target * 255).astype(np.uint8)).save(output_dir / "target.png")
    actuals = {}
    started = time.monotonic()
    for case in cases or CASES:
        if case not in CASES:
            raise ValueError(f"unknown reference case: {case}")
        case_dir = output_dir / case
        params = fp.clean_params(BASE_PARAMS | CASES[case])
        try:
            reference, pipeline = python_stages(target, params)
            actual = cli_stages(source, params, case_dir / "cpu", binary=binary)
        except Exception as error:
            records.append({"name": f"{case}/required_engine_execution", "status": "fail",
                            "error": f"{type(error).__name__}: {error}"})
            continue
        actuals[case] = actual
        for stage in actual:
            rgb = stage == "scan"
            # Stage thresholds guard f32 rounding and LUT interpolation without
            # allowing display quantization to hide exposure or density errors.
            if rgb:
                from processing_support import compare_images
                records.append(compare_images(f"{case}/{stage}/python_vs_cpu", reference[stage],
                                               actual[stage], output_dir,
                                               {"mean": 1, "p95": 2, "max": 8}))
            else:
                records.append(compare_stage(f"{case}/{stage}/python_vs_cpu", reference[stage],
                                             actual[stage], case_dir / stage,
                                             tolerance={"mean": 0.001, "p95": 0.003, "max": 0.01},
                                             units="log10 exposure" if stage.endswith("exposure")
                                             else "optical density"))
        if not params["grain_on"] and not params["couplers_on"]:
            axis, curves = measured_curves(APP / "engine/data/profiles" / (params["stock"] + ".json"))
            expected_density = numpy_develop(actual["film_exposure"], axis, curves, params["gamma"])
            records.append(compare_stage(f"{case}/film_development/numpy_curve", expected_density,
                                         actual["film_development"], case_dir / "numpy_development",
                                         tolerance={"mean": 1e-5, "p95": 3e-5, "max": 1e-4},
                                         units="optical density"))
        if gpu and not params["grain_on"]:
            try:
                actual_gpu = cli_stages(source, params, case_dir / "gpu", backend="wgpu", binary=binary)
                comparison = compare_images(f"{case}/scan/cpu_vs_gpu", actual["scan"], actual_gpu["scan"],
                                            output_dir, {"mean": 1, "p95": 2, "max": 8})
                if params["camera_diffusion_strength"] > 0:
                    comparison["execution"] = "GPU backend with exact CPU diffusion convolution; full-frame rendering"
                records.append(comparison)
            except Exception as error:
                records.append({"name": f"{case}/required_gpu_execution", "status": "fail", "error": str(error)})
    if "negative_baseline" in actuals and "exposure_plus_one" in actuals:
        baseline = actuals["negative_baseline"]["film_exposure"]
        expected = np.log10(np.maximum(10 ** baseline - 1e-10, 0) * 2 + 1e-10)
        records.append(compare_stage("exposure_one_stop/numpy_photometric_law", expected,
                                     actuals["exposure_plus_one"]["film_exposure"],
                                     output_dir / "exposure_law",
                                     tolerance={"mean": 1e-5, "p95": 3e-5, "max": 1e-4},
                                     units="log10 exposure"))
    if cases is None:
        for case, changes in BW_CASES.items():
            params = fp.clean_params(BASE_PARAMS | changes)
            try:
                actual = cli_stages(source, params, output_dir / case / "cpu", binary=binary)
                axis, curves = measured_curves(APP / "engine/data/profiles" / (params["stock"] + ".json"),
                                               params["development_time"])
                expected = numpy_develop(actual["film_exposure"], axis, curves, params["gamma"])
                records.append(compare_stage(f"{case}/film_development/numpy_measured_time_curve", expected,
                                             actual["film_development"], output_dir / case / "numpy_development",
                                             tolerance={"mean": 1e-5, "p95": 3e-5, "max": 1e-4}, units="optical density"))
            except Exception as error:
                records.append({"name": f"{case}/required_engine_execution", "status": "fail", "error": str(error)})
        try:
            records.extend(grain_checks(output_dir / "grain", binary, gpu=gpu))
        except Exception as error:
            records.append({"name": "grain/required_engine_execution", "status": "fail", "error": str(error)})
    # A parity pass is not meaningful if the tested control did nothing. These
    # checks require a measurable effect at the stage controlled by each knob.
    affected_stages = {
        "exposure_plus_one": "film_exposure", "density_gamma": "film_development",
        "dye_couplers": "film_development", "halation": "film_exposure",
        "camera_diffusion": "film_exposure", "print_exposure": "print_development",
        "print_preflash": "print_development", "print_filters": "print_development",
        "scanner_blur": "scan", "scanner_sharpen": "scan", "scanner_glare": "scan",
    }
    if "negative_baseline" in actuals:
        for case, stage in affected_stages.items():
            if case in actuals:
                change = difference_metrics(actuals["negative_baseline"][stage], actuals[case][stage])
                records.append({"name": f"{case}/{stage}/effect_is_observable",
                                "status": "pass" if change["max"] > 1e-6 else "fail",
                                "metrics": change, "minimum_max_change": 1e-6})
    binary_path = Path(binary or APP / "build/processing-cargo/debug/lighttable-engine").resolve()
    reference_root = APP / "vendor/spektrafilm/src/spektrafilm"
    reference_digest = hashlib.sha256()
    reference_files = sorted(reference_root.rglob("*.py"))
    for path in reference_files:
        reference_digest.update(path.relative_to(reference_root).as_posix().encode() + b"\0")
        reference_digest.update(path.read_bytes())
    return {"name": "film_reference", "status": "pass" if all(r["status"] == "pass" for r in records) else "fail",
            "records": records, "seconds": time.monotonic() - started,
            "binary": str(binary_path), "binary_sha256": hashlib.sha256(binary_path.read_bytes()).hexdigest(),
            "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "reference_source": {"path": str(reference_root.resolve()), "files": len(reference_files),
                                 "sha256": reference_digest.hexdigest()},
            "profiles": {name: hashlib.sha256((APP / "engine/data/profiles" / (name + ".json")).read_bytes()).hexdigest()
                         for name in sorted({fp.clean_params(BASE_PARAMS | changes)[key]
                                             for changes in list(CASES.values()) + list(BW_CASES.values())
                                             for key in ("stock", "paper")})},
            "reference": "independent Python spectral model and NumPy characteristic-curve/EV equations",
            "limitations": ["Shared measured profiles are inputs, not independently remeasured film ground truth.",
                            "CPU diagnostic buffers expose film exposure/development and print exposure/development; final scan is a float TIFF from the product export path.",
                            "GPU intermediate stage buffers are not exposed by the resident path.",
                            "Grain is checked against analytical Poisson-binomial moments and exact within-engine repeatability; cross-engine random sample identity is not required.",
                            "Active optical diffusion uses an exact CPU convolution with other stages on the selected backend; this can increase preview latency and disables viewport acceleration for the effect.",
                            "B&W development-time families are checked against their measured characteristic curves, not the Python bridge, which does not route that control."]}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--case", action="append", choices=CASES)
    parser.add_argument("--gpu", action="store_true")
    args = parser.parse_args()
    result = run(args.output_dir, cases=args.case, gpu=args.gpu)
    (args.output_dir / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "pass" else 1)
