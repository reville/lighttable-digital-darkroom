"""Fit screenshot geometry with the captured app's own optics solver."""
from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess


SOLVE = """
import json, sys
sys.path.insert(0, sys.argv[1])
import geometry_auto
request = json.load(sys.stdin)
optics = request['optics']
aspect = request['aspect']
# The implementation behind solve_scale also reports an impossible fit.
required, fits = geometry_auto._scale_and_fit(optics, aspect)
if not fits:
    raise ValueError('Screenshot geometry cannot fit within the supported zoom range')
scale = max(optics.get('scale', 1.0), required)
print(json.dumps(scale))
"""


def fit_state(state: dict, shot: dict, python: Path, app_root: Path) -> dict:
    """Return a fitted copy before saving; never send pipeline flags to the app."""
    if not shot.get("fitGeometry"):
        return state
    width, height = state.get("width"), state.get("height")
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
           for value in (width, height)):
        raise ValueError("Screenshot geometry requires the photo's oriented dimensions")
    optics = dict(state.get("optics") or {})
    result = subprocess.run(
        [str(python), "-B", "-c", SOLVE, str(app_root.resolve())],
        input=json.dumps({"optics": optics, "aspect": width / height}),
        capture_output=True, text=True, check=True, timeout=60,
    )
    scale = json.loads(result.stdout)
    if not isinstance(scale, (int, float)) or not math.isfinite(scale) or not 1 <= scale <= 1.6:
        raise ValueError("Screenshot geometry solver returned an invalid scale")
    return {**state, "optics": {**optics, "scale": scale}}
