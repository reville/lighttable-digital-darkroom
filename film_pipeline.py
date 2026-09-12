# SPDX-License-Identifier: GPL-3.0-only
"""Shared bridge between LightTable UI params and the spektrafilm runtime.

Used by both the preview server (in-process, warm caches) and the
full-resolution export CLI (subprocess per image).
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import numpy as np

import film_tuning

APP = Path(__file__).resolve().parent
RUST_PROFILE_DIR = APP / "engine" / "data" / "profiles"
CUSTOM_PROFILE_DIR = APP / "profiles"
PYTHON_PROFILE_DIR = (
    APP / "vendor" / "spektrafilm" / "src" / "spektrafilm" / "data" / "profiles"
)


def install_custom_profiles() -> None:
    """Make tracked LightTable profiles visible to the untracked Rust runtime."""
    if not CUSTOM_PROFILE_DIR.is_dir() or not RUST_PROFILE_DIR.is_dir():
        return
    for source in CUSTOM_PROFILE_DIR.glob("*.json"):
        destination = RUST_PROFILE_DIR / source.name
        if (not destination.exists()
                or destination.read_bytes() != source.read_bytes()):
            shutil.copyfile(source, destination)


# Local corrections to incomplete upstream metadata. These do not alter the
# measured profile data; they supply intended UI/render pairings and defaults.
PROFILE_OVERRIDES = {
    "kodak_doublex": {
        "targetPrint": "kodak_2302",
        "defaultDevelopmentTime": 6.5,
    },
    "kodak_tmax_p3200": {
        "targetPrint": "kodak_2302",
        "defaultDevelopmentTime": 12.0,
    },
    # The bundled reversal profile scans about 1.5 stops too bright at its
    # nominal zero compensation; this calibrated offset restores highlight
    # separation without altering its measured curve data.
    "kodak_trix": {"defaultExposureEv": -1.5},
}

PREFERRED_FILM_ORDER = [
    "kodak_portra_160", "kodak_portra_400", "kodak_portra_800",
    "kodak_portra_800_push1", "kodak_portra_800_push2",
    "kodak_gold_200", "kodak_ultramax_400", "kodak_ektar_100",
    "kodak_tmax_p3200", "kodak_doublex", "kodak_verita_200d",
    "kodak_vision3_50d", "kodak_vision3_250d", "kodak_vision3_200t",
    "kodak_vision3_500t", "fujifilm_c200", "fujifilm_xtra_400",
    "fujifilm_pro_400h", "fujifilm_velvia_100", "fujifilm_provia_100f",
    "kodak_ektachrome_100", "kodak_kodachrome_64", "kodak_trix",
]
PREFERRED_PAPER_ORDER = [
    "kodak_portra_endura", "kodak_endura_premier", "kodak_supra_endura",
    "kodak_ultra_endura", "kodak_ektacolor_edge",
    "fujifilm_crystal_archive_typeii", "kodak_2383", "kodak_2393",
    "kodak_2302",
]


def _profile_sort_key(profile: dict, preferred: list[str]) -> tuple:
    try:
        return (0, preferred.index(profile["id"]))
    except ValueError:
        return (1, profile["name"].casefold())


def load_profile_catalog() -> list[dict]:
    """Load the engine catalog from profile metadata, not filename guesses."""
    install_custom_profiles()
    python_ids = {p.stem for p in PYTHON_PROFILE_DIR.glob("*.json")}
    profiles = []
    for path in RUST_PROFILE_DIR.glob("*.json"):
        try:
            raw = json.loads(path.read_text())
            info = raw["info"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            continue
        ident = str(info.get("stock") or path.stem)
        times = [float(v) for v in (raw.get("data", {}).get(
            "development_time") or [])]
        profile = {
            "id": ident,
            "name": str(info.get("name") or ident.replace("_", " ").title()),
            "stage": str(info.get("stage") or "filming"),
            "type": str(info.get("type") or "negative"),
            "support": str(info.get("support") or "film"),
            "channelModel": str(info.get("channel_model") or "color"),
            "targetPrint": info.get("target_print"),
            "developmentTimes": times,
            "defaultDevelopmentTime": (times[(len(times) - 1) // 2]
                                       if times else 0.0),
            "rustOnly": ident not in python_ids,
            "custom": path.parent == RUST_PROFILE_DIR
                      and (CUSTOM_PROFILE_DIR / path.name).is_file(),
            "dataHash": hashlib.sha256(path.read_bytes()).hexdigest(),
            "citation": (raw.get("metadata", {}).get("citation")
                         or info.get("citation")),
            "grainBasis": "speed-derived visual baseline",
            "tunings": film_tuning.profile_tunings(ident),
        }
        profile.update(PROFILE_OVERRIDES.get(ident, {}))
        profiles.append(profile)
    films = sorted((p for p in profiles if p["stage"] == "filming"),
                   key=lambda p: _profile_sort_key(p, PREFERRED_FILM_ORDER))
    papers = sorted((p for p in profiles if p["stage"] == "printing"),
                    key=lambda p: _profile_sort_key(p, PREFERRED_PAPER_ORDER))
    return films + papers


PROFILE_CATALOG = load_profile_catalog()
PROFILE_BY_ID = {profile["id"]: profile for profile in PROFILE_CATALOG}
PROFILE_CATALOG_DIGEST = hashlib.sha256(json.dumps(
    [(profile["id"], profile["dataHash"]) for profile in PROFILE_CATALOG],
    separators=(",", ":"), sort_keys=True).encode()).hexdigest()
FILM_PROFILES = [p for p in PROFILE_CATALOG if p["stage"] == "filming"]
PAPER_PROFILES = [p for p in PROFILE_CATALOG if p["stage"] == "printing"]

# Reversal film has no negative-to-paper stage and is scanned directly.
POSITIVE_STOCKS = {
    p["id"] for p in FILM_PROFILES if p["type"] == "positive"
}
NEGATIVES = [p["id"] for p in FILM_PROFILES if p["type"] == "negative"]
POSITIVES = [p["id"] for p in FILM_PROFILES if p["type"] == "positive"]
PAPERS = [p["id"] for p in PAPER_PROFILES]

# Spektrafilm models grain particle area as a physical quantity related to film
# speed (its own control guidance uses roughly 0.1 um2 for ISO 100/200 and
# 0.4 um2 for ISO 400). Profiles do not yet carry a measured granularity
# field, so these are explicit speed-derived baselines rather than claims of
# laboratory measurements for each emulsion. The UI scales these per-stock
# values instead of forcing every stock through one absolute particle size.
STOCK_GRAIN_AREA_UM2 = {
    "kodak_portra_160": 0.10,
    "kodak_portra_400": 0.40,
    "kodak_portra_800": 0.80,
    "kodak_portra_800_push1": 1.20,
    "kodak_portra_800_push2": 1.60,
    "kodak_gold_200": 0.10,
    "kodak_ultramax_400": 0.40,
    "kodak_ektar_100": 0.10,
    # P3200's published RMS granularity is 18. The engine's particle area is
    # a separate model parameter, so 1.20 is a deliberately strong visual
    # baseline rather than a claimed unit conversion from that measurement.
    "kodak_tmax_p3200": 1.20,
    "kodak_doublex": 0.20,
    "kodak_verita_200d": 0.10,
    "kodak_vision3_50d": 0.05,
    "kodak_vision3_250d": 0.20,
    "kodak_vision3_200t": 0.10,
    "kodak_vision3_500t": 0.50,
    "fujifilm_c200": 0.10,
    "fujifilm_xtra_400": 0.40,
    "fujifilm_pro_400h": 0.40,
    "fujifilm_velvia_100": 0.10,
    "fujifilm_provia_100f": 0.10,
    "kodak_ektachrome_100": 0.10,
    "kodak_kodachrome_64": 0.06,
    "kodak_trix": 0.20,
}

# The simulation uses the physical long edge to derive micrometres per pixel.
# Values are representative exposed-image long edges, not marketing names.
FILM_FORMATS_MM = {
    "35mm": 35.0,
    "645": 56.0,
    "6x6": 56.0,
    "6x7": 70.0,
    "4x5": 127.0,
}

# Coherent output-chain starting points. These are modeled recipes, not claims
# that a named commercial lab scanner has been measured. Physical advanced
# controls are applied as offsets/scalars over the selected recipe.
#
# Every recipe scans with black and white point correction on. The engine's
# print stage reproduces the paper as it is, and paper is neither white nor
# black: without the correction a print's brightest pixel reaches only about
# 0.86 in sRGB and its blacks sit near 0.07, which reads as a washed-out
# render rather than as a print. The correction maps the paper's own maximum
# and minimum densities to the scan levels below, which is what a lab scanner
# does when it is calibrated to the paper base. It is off in upstream
# spektrafilm, whose default is to show the physical print.
SCAN_LEVELS = {
    "scan_white_correction": True,
    "scan_black_correction": True,
    "scan_white_level": 0.98,
    "scan_black_level": 0.01,
}
OUTPUT_RECIPES = {
    "clean_scan": {
        **SCAN_LEVELS,
        "name": "Clean scan",
        "description": "Low flare, crisp neutral scan",
        "provenance": "modeled",
        "scanner_lens_blur": 0.0,
        "unsharp_sigma": 0.55,
        "unsharp_amount": 0.55,
        "glare_percent": 0.01,
        "preflash_exposure": 0.0,
    },
    "neutral_print_scan": {
        **SCAN_LEVELS,
        "name": "Neutral print scan",
        "description": "Balanced optical print and neutral scan",
        "provenance": "modeled",
        "scanner_lens_blur": 0.12,
        "unsharp_sigma": 0.7,
        "unsharp_amount": 0.55,
        "glare_percent": 0.03,
        "preflash_exposure": 0.0,
    },
    "soft_optical_print": {
        **SCAN_LEVELS,
        "name": "Soft optical print",
        "description": "Gentler scan MTF with visible print flare",
        "provenance": "modeled",
        "scanner_lens_blur": 0.35,
        "unsharp_sigma": 0.7,
        "unsharp_amount": 0.20,
        "glare_percent": 0.04,
        "preflash_exposure": 0.015,
    },
}

NUMERIC_RANGES = {
    "development_time": (0.0, 60.0),
    "print_development_time": (0.0, 60.0),
    "wb_temperature": (2000.0, 50000.0),
    "wb_tint": (-1.0, 1.0),
    "rotate": (-360.0, 360.0),
    "exposure_ev": (-3.0, 3.0),
    "print_exposure": (0.1, 2.5),
    "gamma": (0.5, 1.5),
    "couplers_amount": (0.0, 2.0),
    "halation_amount": (0.0, 3.0),
    # Keep a generous ceiling so migrated legacy particle-area edits retain
    # their appearance; the UI expands its range to display such values.
    "grain_amount": (0.0, 1000.0),
    "glare_amount": (0.0, 3.0),
    "camera_diffusion_strength": (0.0, 1.0),
    "print_preflash": (0.0, 0.15),
    "print_y_filter_shift": (-20.0, 20.0),
    "print_m_filter_shift": (-20.0, 20.0),
    "scan_softness": (0.0, 1.0),
    "scan_sharpness": (0.0, 2.0),
    "learned_denoise_strength": (0.0, 1.0),
}

RAW_DEVELOP_KEYS = (
    "raw_profile", "raw_highlight_recovery", "raw_sensor_denoise",
    "learned_denoise", "learned_denoise_strength",
    "wb_mode", "wb_temperature", "wb_tint", "developProfile",
    "camera_profile",
)


def camera_profile_name(value) -> str:
    """A bare ``.dcp`` file name, or empty. Never a path."""
    name = " ".join(str(value or "").split()).strip()
    if (not name or len(name) > 200 or "/" in name or "\\" in name
            or name.startswith(".") or not name.lower().endswith(".dcp")):
        return ""
    return name

DEFAULT_PARAMS = {
    # A non-destructive bypass. The selected stock, paper, and physical-stage
    # settings remain stored so the film look can be restored with one click.
    "profile_enabled": True,
    "stock": "kodak_portra_400",
    # LightTable tuned Kodak Portra 400 is the default when film simulation
    # is enabled. Missing variant fields on legacy edits with a saved stock
    # preserve original upstream rendering.
    "film_tuning": "lighttable",
    "film_tuning_version": "1",
    "paper": "kodak_portra_endura",
    "workflow_mode": "authentic",
    "paper_locked": False,
    "output_recipe": "neutral_print_scan",
    # 0 means use the profile's default. B&W development-time families expose
    # their measured choices in the UI and pass a positive minute value here.
    "development_time": 0.0,
    "print_development_time": 0.0,
    # True when the source is already scene-linear (a decoded RAW file), so
    # the sRGB transfer curve must not be decoded a second time.
    "linear_input": False,
    # All film input is normalized to ProPhoto RGB. RAW is linear; processed
    # files retain the ROMM transfer function and are decoded by the engine.
    "input_color_space": "ProPhoto RGB",
    # Capture-stage RAW development. These are resolved by rawpy before the
    # film model or display-referred grade sees the image.
    "raw_profile": "camera",
    "raw_highlight_recovery": "reconstruct",
    "raw_sensor_denoise": "off",
    "learned_denoise": False,
    "learned_denoise_strength": 0.6,
    "wb_mode": "as_shot",
    "wb_temperature": 5500.0,
    "wb_tint": 0.0,
    # The rendering intent used when the film profile is off, applied by
    # color_pipeline.linear_prophoto_to_display_srgb. "standard" adds a fixed
    # base curve to the neutral decode and is the default for new users and
    # imported edits; "linear" is the flat scene-linear encode, for people
    # matching scans. It has no effect on the film render path.
    "developProfile": "standard",
    # A camera profile (.dcp) from the user's own Adobe Camera Raw or
    # Lightroom installation, by file name inside the configured profile
    # folder. Empty means the built-in develop profile. Applied to the
    # Film-off develop only, and read through color_pipeline's resolver so
    # this module never touches the filesystem.
    "camera_profile": "",
    "film_format": "35mm",
    # Presentation-only, in degrees clockwise. Some files (notably these
    # X100VI RAFs) carry Orientation=1 despite portrait content, so the
    # rotation has to be a manual override.
    "rotate": 0.0,
    "exposure_ev": 0.0,
    "print_exposure": 1.0,
    "gamma": 1.0,
    "auto_exposure": True,
    "scan_sharpen": True,
    "couplers_on": True,
    "couplers_amount": 1.0,
    "halation_on": True,
    "halation_amount": 1.0,
    "grain_on": True,
    # Multiplier over STOCK_GRAIN_AREA_UM2. A value of 1.0 is the stock's
    # physical baseline; legacy grain_um2 values are migrated in clean_params.
    "grain_amount": 1.0,
    "glare_on": True,
    "glare_amount": 1.0,
    "camera_diffusion_family": "black_pro_mist",
    "camera_diffusion_strength": 0.0,
    "print_preflash": 0.0,
    "print_y_filter_shift": 0.0,
    "print_m_filter_shift": 0.0,
    "scan_softness": 0.0,
    "scan_sharpness": 1.0,
}


def stock_grain_area_um2(stock: str) -> float:
    """Return the stock's native grain particle-area baseline."""
    return STOCK_GRAIN_AREA_UM2.get(stock, 0.20)


def profile_requires_rust(stock: str) -> bool:
    return bool(PROFILE_BY_ID.get(stock, {}).get("rustOnly", False))


def compatible_papers(stock: str) -> list[str]:
    """Return print-stage profiles with the film's channel model."""
    film = PROFILE_BY_ID.get(stock, {})
    channel_model = film.get("channelModel", "color")
    return [p["id"] for p in PAPER_PROFILES
            if p["channelModel"] == channel_model]


def default_paper(stock: str) -> str:
    film = PROFILE_BY_ID.get(stock, {})
    compatible = compatible_papers(stock)
    target = film.get("targetPrint")
    if target in compatible:
        return target
    return compatible[0] if compatible else DEFAULT_PARAMS["paper"]


def effective_grain_area_um2(p: dict) -> float:
    """Resolve the stock baseline multiplied by the user's grain amount."""
    p = clean_params(p)
    return round(stock_grain_area_um2(p["stock"]) * p["grain_amount"], 4)


def output_recipe(p: dict) -> dict:
    ident = clean_params(p)["output_recipe"]
    return OUTPUT_RECIPES.get(ident, OUTPUT_RECIPES["neutral_print_scan"])


def rust_cli_environment() -> dict[str, str] | None:
    """Keep Linux's upstream one-shot fallback within its supported CPU path.

    The resident engine carries our portable GPU workgroups. The separately
    pinned upstream CLI still assumes 1024 threads, so it must not retry that
    GPU path when a resident render has fallen back to the one-shot worker.
    Other platforms retain their inherited backend selection.
    """
    if sys.platform.startswith("linux"):
        return dict(os.environ, SPEKTRAFILM_BACKEND="cpu")
    return None


def rust_params_json(p: dict) -> dict:
    """Same parameters, shaped for the spektrafilm-rs `--params` file.

    The Rust port mirrors the Python schema field for field, so this is a
    re-nesting rather than a translation.
    """
    p = clean_params(p)
    recipe = output_recipe(p)
    positive = p["stock"] in POSITIVE_STOCKS
    film_render = {
        "density_curve_gamma": p["gamma"],
        "grain": {"active": p["grain_on"],
                  # spektrafilm-rs keeps the upstream model's historical
                  # `agx_` field name.  Sending `particle_area_um2` is
                  # silently ignored by the Rust parameter loader.
                  "agx_particle_area_um2": effective_grain_area_um2(p)},
        "halation": {"active": p["halation_on"],
                     "halation_amount": p["halation_amount"]},
        "dir_couplers": {"active": p["couplers_on"],
                         "amount": p["couplers_amount"]},
        "glare": {"active": False},
    }
    if p["development_time"] > 0:
        film_render["development_time"] = p["development_time"]
    result = {
        "camera": {
            "exposure_compensation_ev": p["exposure_ev"],
            "auto_exposure": p["auto_exposure"],
            "film_format_mm": FILM_FORMATS_MM[p["film_format"]],
            "diffusion_filter": {
                "active": p["camera_diffusion_strength"] > 0,
                "filter_family": p["camera_diffusion_family"],
                "strength": p["camera_diffusion_strength"],
            },
        },
        "enlarger": {
            "print_exposure": p["print_exposure"],
            "preflash_exposure": (recipe["preflash_exposure"] +
                                  p["print_preflash"]),
            "y_filter_shift": p["print_y_filter_shift"],
            "m_filter_shift": p["print_m_filter_shift"],
        },
        "scanner": {
            "lens_blur": recipe["scanner_lens_blur"] + p["scan_softness"],
            "white_correction": recipe["scan_white_correction"],
            "black_correction": recipe["scan_black_correction"],
            "white_level": recipe["scan_white_level"],
            "black_level": recipe["scan_black_level"],
            "unsharp_mask": [
                recipe["unsharp_sigma"],
                (recipe["unsharp_amount"] * p["scan_sharpness"]
                 if p["scan_sharpen"] else 0.0),
            ],
        },
        "film_render": film_render,
        "print_render": {
            "glare": {
                "active": p["glare_on"] and not positive,
                "percent": recipe["glare_percent"] * p["glare_amount"],
                # A uniform LightTable baseline is repeatable in both engines;
                # users control its amount rather than a new random texture.
                "roughness": 0.0,
            },
        },
        "io": {
            "input_color_space": p["input_color_space"],
            "input_cctf_decoding": (not p["linear_input"]
                                    and film_tuning.specification(p) is None),
            "output_color_space": "sRGB",
            "output_cctf_encoding": True,
            "scan_film": positive,
        },
    }
    if p["print_development_time"] > 0:
        result["print_render"]["development_time"] = p["print_development_time"]
    return result


def rust_tuning_request(p: dict, image: np.ndarray | None = None,
                        anchor: float | None = None) -> dict:
    """LightTable worker extension, deliberately outside upstream params.

    ``image`` or ``anchor`` supplies the expansion anchor for a processed
    source; the application passes the anchor it measured once per photo so
    preview and export agree. Without either the anchor is middle grey.
    """
    spec = film_tuning.specification(clean_params(p), image)
    if spec is not None and anchor is not None and spec["display_expansion"] > 0:
        spec = dict(spec, display_expansion_anchor=float(anchor))
    return {"input_tuning": spec} if spec is not None else {}


@contextmanager
def prepared_input_file(source: str | Path, p: dict, anchor: float | None = None):
    """Apply the same tuning for the separately pinned one-shot Rust CLI."""
    cleaned = clean_params(p)
    if film_tuning.specification(cleaned) is None:
        yield Path(source)
        return
    import tifffile
    with tempfile.TemporaryDirectory(prefix="lighttable-film-input-") as directory:
        path = Path(directory) / "linear-prophoto.tif"
        pixels = load_linear(str(source))
        spec = film_tuning.specification(cleaned, pixels)
        if anchor is not None and spec["display_expansion"] > 0:
            spec = dict(spec, display_expansion_anchor=float(anchor))
        tuned = film_tuning.prepare_input(pixels, spec)
        if spec.get("display_expansion", 0.0) > 0:
            # Expanded highlights are scene-linear values above 1.0. A 16-bit
            # file would clip them back to display white, so hand the CLI the
            # float pixels the resident engine sees.
            tifffile.imwrite(path, np.ascontiguousarray(tuned, dtype=np.float32),
                             photometric="rgb")
        else:
            tifffile.imwrite(path, (np.clip(tuned, 0, 1) * 65535 + 0.5).astype(np.uint16),
                             photometric="rgb")
        yield path


def clean_params(p: dict) -> dict:
    """Fill defaults and drop unknown keys so cache keys stay stable."""
    source = dict(p or {})
    # Before stock-relative grain, persisted edits stored an absolute particle
    # area. Convert it to a multiplier at the edit's current stock, preserving
    # its existing appearance while making subsequent stock changes physical.
    if "grain_amount" not in source and "grain_um2" in source:
        stock = str(source.get("stock", DEFAULT_PARAMS["stock"]))
        try:
            source["grain_amount"] = (
                float(source["grain_um2"]) / stock_grain_area_um2(stock)
            )
        except (TypeError, ValueError):
            source["grain_amount"] = DEFAULT_PARAMS["grain_amount"]
    out = {}
    for k, v in DEFAULT_PARAMS.items():
        raw = source.get(k, v)
        if isinstance(v, bool):
            out[k] = bool(raw)
        elif isinstance(v, float):
            try:
                number = float(raw)
                out[k] = round(number, 4) if math.isfinite(number) else v
            except (TypeError, ValueError):
                out[k] = v
        else:
            out[k] = str(raw)
    for key, (minimum, maximum) in NUMERIC_RANGES.items():
        out[key] = round(max(minimum, min(maximum, out[key])), 4)
    out["camera_profile"] = camera_profile_name(out["camera_profile"])
    if out["stock"] not in {p["id"] for p in FILM_PROFILES}:
        out["stock"] = DEFAULT_PARAMS["stock"]
    if "film_tuning" not in source and "stock" in source:
        if source.get("stock") != "kodak_portra_400" or source.get("profile_enabled") is True:
            out["film_tuning"] = "original"
            out["film_tuning_version"] = "1"
    elif (out["film_tuning"] != "lighttable"
            or out["film_tuning_version"] != film_tuning.VERSION
            or out["stock"] not in film_tuning.TUNINGS):
        out["film_tuning"] = "original"
        out["film_tuning_version"] = film_tuning.VERSION
    if out["workflow_mode"] not in ("authentic", "creative"):
        out["workflow_mode"] = "authentic"
    valid_papers = compatible_papers(out["stock"])
    if (out["workflow_mode"] == "authentic" and not out["paper_locked"]
            and out["stock"] not in POSITIVE_STOCKS):
        out["paper"] = default_paper(out["stock"])
    elif out["paper"] not in valid_papers:
        out["paper"] = default_paper(out["stock"])
    if out["film_format"] not in FILM_FORMATS_MM:
        out["film_format"] = "35mm"
    if out["output_recipe"] not in OUTPUT_RECIPES:
        out["output_recipe"] = "neutral_print_scan"
    if out["wb_mode"] not in ("as_shot", "auto", "daylight", "cloudy", "shade", "tungsten", "fluorescent", "flash", "custom"):
        out["wb_mode"] = "as_shot"
    if out["raw_profile"] not in ("camera", "detail", "smooth"):
        out["raw_profile"] = "camera"
    if out["raw_highlight_recovery"] not in ("off", "blend", "reconstruct"):
        out["raw_highlight_recovery"] = "reconstruct"
    if out["raw_sensor_denoise"] not in ("off", "light", "full"):
        out["raw_sensor_denoise"] = "off"
    if out["developProfile"] not in ("linear", "standard", "soft"):
        out["developProfile"] = "standard"
    if out["input_color_space"] != "ProPhoto RGB":
        out["input_color_space"] = "ProPhoto RGB"
    if out["camera_diffusion_family"] not in (
            "black_pro_mist", "pro_mist", "glimmerglass", "cinebloom"):
        out["camera_diffusion_family"] = "black_pro_mist"
    return out


def build_params(p: dict):
    global spektrafilm
    import spektrafilm

    p = clean_params(p)
    if profile_requires_rust(p["stock"]):
        raise ValueError(f"{p['stock']} is supported by the Rust engine only")
    params = spektrafilm.init_params(
        film_profile=p["stock"], print_profile=p["paper"])

    params.io.input_color_space = p["input_color_space"]
    params.io.input_cctf_decoding = (not p["linear_input"]
                                   and film_tuning.specification(p) is None)
    params.io.output_color_space = "sRGB"
    params.io.output_cctf_encoding = True
    params.settings.use_enlarger_lut = True
    params.settings.use_scanner_lut = True

    recipe = output_recipe(p)
    params.camera.film_format_mm = FILM_FORMATS_MM[p["film_format"]]
    params.camera.auto_exposure = p["auto_exposure"]
    params.camera.exposure_compensation_ev = p["exposure_ev"]
    params.enlarger.print_exposure = p["print_exposure"]
    params.enlarger.preflash_exposure = (recipe["preflash_exposure"] +
                                         p["print_preflash"])
    params.enlarger.y_filter_shift = p["print_y_filter_shift"]
    params.enlarger.m_filter_shift = p["print_m_filter_shift"]
    params.camera.diffusion_filter.active = p["camera_diffusion_strength"] > 0
    params.camera.diffusion_filter.filter_family = p["camera_diffusion_family"]
    params.camera.diffusion_filter.strength = p["camera_diffusion_strength"]
    params.scanner.lens_blur = recipe["scanner_lens_blur"] + p["scan_softness"]
    params.scanner.white_correction = recipe["scan_white_correction"]
    params.scanner.black_correction = recipe["scan_black_correction"]
    params.scanner.white_level = recipe["scan_white_level"]
    params.scanner.black_level = recipe["scan_black_level"]
    params.scanner.unsharp_mask = (
        recipe["unsharp_sigma"],
        recipe["unsharp_amount"] * p["scan_sharpness"]
        if p["scan_sharpen"] else 0.0,
    )
    # This is a creative density-curve contrast scale, not a complete
    # chemical push/pull model. Calibrated pushed stocks remain profiles.
    params.film_render.density_curve_gamma = p["gamma"]

    fr = params.film_render
    fr.dir_couplers.active = p["couplers_on"]
    fr.dir_couplers.amount = p["couplers_amount"]
    fr.halation.active = p["halation_on"]
    fr.halation.halation_amount = p["halation_amount"]
    fr.grain.active = p["grain_on"]
    fr.grain.particle_area_um2 = effective_grain_area_um2(p)
    fr.glare.active = False
    params.print_render.glare.active = (
        p["glare_on"] and p["stock"] not in POSITIVE_STOCKS)
    params.print_render.glare.percent = (
        recipe["glare_percent"] * p["glare_amount"])
    params.print_render.glare.roughness = 0.0

    if p["stock"] in POSITIVE_STOCKS:
        params.io.scan_film = True

    return params


def load_linear(tif_path: str, max_width: int | None = None) -> np.ndarray:
    """Read a 16-bit TIFF into float32 [0,1] RGB, optionally downsized."""
    import tifffile
    import color_pipeline

    raw = tifffile.imread(tif_path)
    if raw.ndim == 3 and raw.shape[2] == 4:
        raw = raw[:, :, :3]
    if np.issubdtype(raw.dtype, np.integer):
        scale = float(np.iinfo(raw.dtype).max)
        img = raw.astype(np.float32) / scale
    else:
        img = raw.astype(np.float32)
    return color_pipeline.resize_float_width(img, max_width)


def render_float(image: np.ndarray, p: dict) -> np.ndarray:
    """Run the full pipeline and retain display-referred float precision."""
    import spektrafilm

    p = clean_params(p)
    if not p["profile_enabled"]:
        # Non-RAW inputs are already display-referred. RAW decodes are linear,
        # so provide a reasonable neutral display conversion for direct users
        # of this bridge; the app preview/export use the camera's full neutral
        # conversion for exact source-view parity.
        out = np.clip(image, 0.0, 1.0)
        if p["linear_input"]:
            out = np.where(out <= 0.0031308,
                           out * 12.92,
                           1.055 * np.power(out, 1.0 / 2.4) - 0.055)
        return np.clip(out, 0.0, 1.0).astype(np.float32)
    spec = film_tuning.specification(p, image)
    if spec is not None:
        image = film_tuning.prepare_input(image, spec)
    out = spektrafilm.simulate(image, build_params(p))
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def render(image: np.ndarray, p: dict) -> np.ndarray:
    """Compatibility wrapper used by the 8-bit display preview."""
    return (render_float(image, p) * 255.0 + 0.5).astype(np.uint8)
