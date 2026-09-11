# SPDX-License-Identifier: GPL-3.0-only
"""Regression recipes for ordered edits, with flat-field and real-photo controls."""
import base64
import io
from pathlib import Path

import numpy as np
from PIL import Image

from processing_support import target_rgb8


def full_mask(values):
    return {"type": "subject", "bitmap": {"width": 1, "height": 1,
            "data": base64.b64encode(b"\xff").decode()}, "opacity": 1, "grade": values}


def edit_sources():
    width, height = 192, 128
    x, y = np.meshgrid(np.linspace(0, 1, width), np.linspace(0, 1, height))
    gradient = np.stack([.1 + .75*x, .25 + .3*y, .65 - .45*x], axis=-1)
    gradient[((x - .35)**2 + ((y - .5)*height/width)**2) < .024**2] = .04
    with Image.open(Path(__file__).parent / "fixtures/photos/still-life.jpg") as image:
        photo = np.asarray(image.convert("RGB").resize((width, height)))
    return {"flat": np.full((height, width, 3), 64, np.uint8),
            "target": target_rgb8(width, height),
            "gradient": np.rint(gradient * 255).astype(np.uint8), "photo": photo}


def edit_cases():
    spot = {"target": [.35, .5], "source": [.7, .5], "radius": .14, "feather": .4}
    cases = [
        {"name": "flat-local-texture", "fixture": "flat", "masks": [full_mask({"texture": 1})]},
        {"name": "flat-exposure-texture", "fixture": "flat", "grade": {"exposure": 1},
         "masks": [full_mask({"texture": 1})]},
        {"name": "flat-exposure-clarity", "fixture": "flat", "grade": {"exposure": 1},
         "masks": [full_mask({"clarity": 1})]},
        {"name": "prior-mask-texture", "fixture": "flat",
         "masks": [full_mask({"exposure": 1}), full_mask({"texture": 1})]},
        {"name": "photo-mask-detail", "fixture": "photo", "grade": {"exposure": .7},
         "masks": [full_mask({"texture": .7, "clarity": .4})]},
        {"name": "photo-partial-mask-detail", "fixture": "photo", "grade": {"exposure": .7},
         "masks": [{"type": "radial", "center": [.4, .5], "radius": .3,
                    "feather": .3, "grade": {"texture": .7, "clarity": .4}}]},
        {"name": "clone-then-sharpen", "fixture": "gradient", "grade": {"sharpness": 1},
         "heals": [dict(spot, mode="clone")]},
        {"name": "sequential-clones", "fixture": "gradient", "heals": [dict(spot, mode="clone"),
         dict(spot, mode="clone", target=[.15, .5], source=[.35, .5], radius=.1)]},
    ]
    brush = {"buildUp": True, "size": .35, "feather": .6, "flow": .2,
             "density": .5, "points": [[.2, .5], [.8, .5]]}
    for count in (1, 2, 5):
        cases.append({"name": f"brush-flow-{count}", "fixture": "photo", "masks": [
            {"type": "brush", "strokes": [brush] * count, "grade": {"exposure": .7}}]})
    edge = np.zeros((128, 192), dtype=np.uint8)
    edge[:, :96] = 255
    encoded = io.BytesIO()
    Image.fromarray(edge).save(encoded, format='PNG')
    bitmap = {"width": 192, "height": 128, "encoding": "png",
              "data": base64.b64encode(encoded.getvalue()).decode()}
    cases.append({"name": "brush-saved-auto-mask", "fixture": "photo", "masks": [
        {"type": "brush", "strokes": [dict(brush, edgeMask=bitmap)] * 3,
         "grade": {"exposure": .7}}]})
    cases.extend([
        {"name": "ellipse-local-tones", "fixture": "photo", "masks": [
            {"type": "radial", "center": [.4, .5], "radiusX": .4, "radiusY": .17,
             "angle": 35, "feather": .6, "grade": {"whites": -.4, "blacks": .25}}]},
        {"name": "ellipse-local-curve", "fixture": "photo", "masks": [
            {"type": "radial", "center": [.5, .5], "radiusX": .45, "radiusY": .2,
             "angle": -20, "feather": .5,
             "grade": {"curveL": (np.linspace(0, 1, 256) ** .75).tolist()}}]},
    ])
    for mode in ("clone", "heal", "remove"):
        cases.append({"name": "photo-" + mode, "fixture": "photo", "heals": [dict(spot, mode=mode)]})
    for key, value in (("rotate", 7), ("distortion", .3), ("vertical", .3),
                       ("horizontal", -.3), ("scale", 1.2), ("flipHorizontal", True)):
        cases.append({"name": "optics-" + key, "fixture": "target", "optics": {key: value}})
    cases.append({"name": "rotation-sharpen", "fixture": "target",
                  "optics": {"rotate": 7}, "grade": {"sharpness": .6}})
    return cases
