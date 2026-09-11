# SPDX-License-Identifier: GPL-3.0-only
"""Deterministic colour, tone, and edge target for LightTable calibration."""
from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageDraw


def target_png(width: int = 1600, height: int = 1000) -> bytes:
    """Create a deterministic target with patches, ramps, and bright edges."""
    width = max(800, min(3200, int(width)))
    height = max(500, min(2000, int(height)))
    image = Image.new("RGB", (width, height), (116, 116, 116))
    draw = ImageDraw.Draw(image)
    margin = round(width * 0.04)
    top = round(height * 0.06)
    patch_colors = [
        (115, 82, 68), (194, 150, 130), (98, 122, 157), (87, 108, 67),
        (133, 128, 177), (103, 189, 170), (214, 126, 44), (80, 91, 166),
        (193, 90, 99), (94, 60, 108), (157, 188, 64), (224, 163, 46),
        (56, 61, 150), (70, 148, 73), (175, 54, 60), (231, 199, 31),
        (187, 86, 149), (8, 133, 161), (243, 243, 242), (200, 200, 200),
        (160, 160, 160), (122, 122, 121), (85, 85, 85), (52, 52, 52),
    ]
    gap = max(4, round(width * 0.005))
    patch_width = (width - margin * 2 - gap * 5) // 6
    patch_height = round(height * 0.105)
    for index, color in enumerate(patch_colors):
        row, column = divmod(index, 6)
        x0 = margin + column * (patch_width + gap)
        y0 = top + row * (patch_height + gap)
        draw.rectangle((x0, y0, x0 + patch_width, y0 + patch_height), fill=color)

    ramp_top = top + 4 * (patch_height + gap) + round(height * 0.04)
    ramp_height = round(height * 0.11)
    ramp = np.linspace(0, 255, width - margin * 2, dtype=np.uint8)
    ramp_rgb = np.repeat(ramp[None, :, None], ramp_height, axis=0)
    ramp_rgb = np.repeat(ramp_rgb, 3, axis=2)
    image.paste(Image.fromarray(ramp_rgb, "RGB"), (margin, ramp_top))

    edge_top = ramp_top + ramp_height + round(height * 0.05)
    edge_bottom = height - top
    third = (width - margin * 2) // 3
    draw.rectangle((margin, edge_top, margin + third, edge_bottom), fill=(2, 2, 2))
    draw.rectangle((margin + third, edge_top, margin + 2 * third, edge_bottom),
                   fill=(252, 252, 252))
    for x in range(margin + 2 * third, width - margin, max(2, width // 200)):
        value = 255 if (x // max(2, width // 200)) % 2 else 0
        draw.line((x, edge_top, x, edge_bottom), fill=(value, value, value), width=1)

    buffer = io.BytesIO()
    image.save(buffer, "PNG", icc_profile=None)
    return buffer.getvalue()
