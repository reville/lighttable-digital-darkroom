# SPDX-License-Identifier: GPL-3.0-only
"""Analytic brush coverage, mirrored in web/mask-raster.js.

Each stroke contributes once at each pixel, independent of pointer sampling.
Separate strokes build coverage up to their Density ceiling. Legacy strokes
retain their original rasterization in edits.py.
"""
import numpy as np


def stroke_coverage(stroke, height, width):
    coverage = np.zeros((height, width), dtype=np.float32)
    radius = max(0.5, stroke["size"] * min(width, height) / 2)
    feather = stroke["feather"]
    points = [(x * (width - 1), y * (height - 1)) for x, y in stroke["points"]]
    for a, b in zip(points, points[1:] or points):
        x0 = max(0, int(np.floor(min(a[0], b[0]) - radius - 1)))
        x1 = min(width, int(np.ceil(max(a[0], b[0]) + radius + 1)))
        y0 = max(0, int(np.floor(min(a[1], b[1]) - radius - 1)))
        y1 = min(height, int(np.ceil(max(a[1], b[1]) + radius + 1)))
        yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        dx, dy = b[0] - a[0], b[1] - a[1]
        denominator = dx * dx + dy * dy
        t = np.clip(((xx - a[0]) * dx + (yy - a[1]) * dy) /
                    max(denominator, 1e-12), 0, 1)
        distance = np.hypot(xx - a[0] - t * dx, yy - a[1] - t * dy) / radius
        if feather <= 0:
            alpha = (distance <= 1).astype(np.float32)
        else:
            fade = np.clip((distance - (1 - feather)) / feather, 0, 1)
            alpha = 1 - fade * fade * (3 - 2 * fade)
        region = coverage[y0:y1, x0:x1]
        np.maximum(region, alpha, out=region)
    return coverage


def accumulate(combined, coverage, stroke):
    ceiling = stroke.get("density", 1.0)
    return np.maximum(combined, np.minimum(ceiling,
                      combined + coverage * stroke["flow"]))
