# SPDX-License-Identifier: GPL-3.0-only
"""LittleCMS and shader-reference soft proofing engine."""
from __future__ import annotations

import sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageCms

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
TARGETS = {
    "srgb": (1.0, 0.0, 1.0),
    "display_p3": (1.15, 0.0, 1.0),
    "matte": (0.68, 0.032, 0.90),
    "gloss": (0.86, 0.012, 0.96),
}

INTENTS = {
    "perceptual": ImageCms.Intent.PERCEPTUAL,
    "relative_colorimetric": ImageCms.Intent.RELATIVE_COLORIMETRIC,
    "saturation": ImageCms.Intent.SATURATION,
    "absolute_colorimetric": ImageCms.Intent.ABSOLUTE_COLORIMETRIC,
}


def _srgb_to_linear(value):
    return np.where(value <= 0.04045, value / 12.92,
                    ((value + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(value):
    value = np.clip(value, 0.0, None)
    return np.where(value <= 0.0031308, value * 12.92,
                    1.055 * value ** (1.0 / 2.4) - 0.055)


def apply(image_srgb: np.ndarray, target="srgb", *, simulate_paper=True):
    """Return the proofed RGB image and an out-of-gamut boolean mask."""
    if target not in TARGETS:
        raise ValueError("unknown soft-proof target")
    gamut, black, white = TARGETS[target]
    linear = _srgb_to_linear(np.clip(image_srgb, 0.0, 1.0).astype(np.float32))
    luminance = (linear @ LUMA)[..., None]
    coordinates = luminance + (linear - luminance) / gamut
    warning = np.any((coordinates < 0.0) | (coordinates > 1.0), axis=2)
    proof = luminance + (linear - luminance) * gamut
    if simulate_paper and target in ("matte", "gloss"):
        proof = black + proof * (white - black)
    return (np.clip(_linear_to_srgb(proof), 0.0, 1.0).astype(np.float32),
            warning)


def _get_cms_profile(profile: str | Path | ImageCms.ImageCmsProfile) -> ImageCms.ImageCmsProfile:
    if isinstance(profile, ImageCms.ImageCmsProfile):
        return profile
    p_str = str(profile).strip()
    if p_str.lower() in ("srgb", "srgb-v4.icc"):
        return ImageCms.createProfile("sRGB")
    return ImageCms.getOpenProfile(str(profile))


def apply_icc_proof(
    image: np.ndarray,
    target_profile: str | Path | ImageCms.ImageCmsProfile,
    *,
    input_space: str = "srgb",
    display_space: str = "srgb",
    intent: str = "relative_colorimetric",
    simulate_paper: bool = False,
    simulate_black: bool = True,
    gamut_warning: bool = False,
) -> tuple[np.ndarray, np.ndarray]:
    """Accurate ICC soft-proofing using LittleCMS.

    Converts `image` from `input_space` through `target_profile` (simulating
    the printer, paper, or target display gamut) and back to `display_space`.

    Returns:
        (proofed_image_float32, out_of_gamut_mask)
    """
    in_prof = _get_cms_profile(input_space)
    disp_prof = _get_cms_profile(display_space)
    proof_prof = _get_cms_profile(target_profile)

    intent_code = INTENTS.get(intent, ImageCms.Intent.RELATIVE_COLORIMETRIC)
    proof_intent_code = (ImageCms.Intent.ABSOLUTE_COLORIMETRIC
                         if simulate_paper
                         else intent_code)

    flags = ImageCms.Flags.SOFTPROOFING
    if simulate_black:
        flags |= ImageCms.Flags.BLACKPOINTCOMPENSATION

    # 1. Normal soft proof transform
    proof_transform = ImageCms.buildProofTransform(
        in_prof, disp_prof, proof_prof, "RGB", "RGB",
        renderingIntent=intent_code,
        proofRenderingIntent=proof_intent_code,
        flags=flags,
    )

    # 2. Gamut check transform for out-of-gamut detection
    gamut_transform = ImageCms.buildProofTransform(
        in_prof, disp_prof, proof_prof, "RGB", "RGB",
        renderingIntent=intent_code,
        proofRenderingIntent=proof_intent_code,
        flags=flags | ImageCms.Flags.GAMUTCHECK,
    )

    arr = np.clip(image * 255.0, 0, 255).astype(np.uint8)
    pil_img = Image.fromarray(arr, mode="RGB")

    proofed_pil = ImageCms.applyTransform(pil_img, proof_transform)
    gamut_pil = ImageCms.applyTransform(pil_img, gamut_transform)

    proofed_arr = np.asarray(proofed_pil, dtype=np.float32) / 255.0
    gamut_arr = np.asarray(gamut_pil, dtype=np.float32) / 255.0

    # Pixels where gamut check modified the output differ from standard proof
    warning_mask = np.any(np.abs(gamut_arr - proofed_arr) > (2.0 / 255.0), axis=-1)

    return proofed_arr, warning_mask


def list_system_icc_profiles() -> list[dict]:
    """Discover available ICC printer and display profiles installed on the system."""
    search_dirs: list[Path] = []
    if sys.platform == "darwin":
        search_dirs.extend([
            Path.home() / "Library/ColorSync/Profiles",
            Path("/Library/ColorSync/Profiles"),
            Path("/System/Library/ColorSync/Profiles"),
        ])
    elif sys.platform == "win32":
        search_dirs.append(Path(r"C:\Windows\System32\spool\drivers\color"))
    else:
        search_dirs.extend([
            Path.home() / ".color/icc",
            Path.home() / ".local/share/icc",
            Path("/usr/share/color/icc"),
        ])

    bundled = Path(__file__).resolve().parent / "color-profiles"
    if bundled.is_dir():
        search_dirs.append(bundled)

    results = []
    seen_paths = set()

    for directory in search_dirs:
        if not directory.is_dir():
            continue
        try:
            for pattern in ("**/*.[iI][cC][cC]", "**/*.[iI][cC][mM]"):
                for p in sorted(directory.glob(pattern)):
                    if p in seen_paths or not p.is_file():
                        continue
                    seen_paths.add(p)
                    try:
                        prof = ImageCms.getOpenProfile(str(p))
                        desc = ImageCms.getProfileDescription(prof) or p.stem
                        mfr = ImageCms.getProfileManufacturer(prof) or ""
                        model = ImageCms.getProfileModel(prof) or ""
                        results.append({
                            "path": str(p),
                            "name": desc.strip(),
                            "manufacturer": mfr.strip(),
                            "model": model.strip(),
                            "filename": p.name,
                        })
                    except Exception:
                        pass
        except Exception:
            pass

    return sorted(results, key=lambda x: x["name"].casefold())
