"""Build the synthetic DNG fixtures the RAW capture suite decodes.

The repository deliberately ships no camera originals: the smallest real RAW
is still megabytes, licensing has to be tracked per file, and the useful part
for a capture-stage test is not the camera, it is the mosaic. These fixtures
are written from first principles as valid uncompressed DNGs, so they are a
few kilobytes each, reproducible from this script, and free of any provenance
question.

They exercise the decoder, not a particular sensor. Format coverage for real
camera files stays with the RAW journey layers in docs/journey-testing.md.

    python tests/make_raw_fixtures.py
"""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import numpy as np
import tifffile

FIXTURES = Path(__file__).parent / "fixtures" / "raw"
WIDTH, HEIGHT = 64, 48
BLACK, WHITE = 1024, 65535
# A plausible, deliberately non-identity camera matrix, so a decode that
# skipped colour conversion would not accidentally look correct.
COLOR_MATRIX = [0.70, -0.20, -0.05, -0.30, 1.20, 0.08, -0.02, 0.15, 0.60]
# An as-shot neutral well away from grey, so white balance has visible work.
AS_SHOT_NEUTRAL = (0.52, 1.0, 0.68)


def _rational(value: float) -> tuple[int, int]:
    fraction = Fraction(value).limit_denominator(10000)
    return fraction.numerator, fraction.denominator


def scene() -> np.ndarray:
    """A scene with a ramp, a saturated block, clipped highlights, and noise."""
    y, x = np.mgrid[0:HEIGHT, 0:WIDTH]
    image = np.stack([x / (WIDTH - 1), y / (HEIGHT - 1),
                      1 - x / (WIDTH - 1)], axis=-1).astype(np.float64)
    image[8:20, 8:24] = (0.85, 0.18, 0.08)
    # Deliberately beyond full scale so highlight recovery has something to
    # reconstruct rather than merely clip.
    image[8:20, 32:48] = 1.4
    # A flat mid-grey field carrying sensor-like noise, for the denoise modes.
    rng = np.random.default_rng(20260910)
    image[28:44, 8:40] = np.clip(0.35 + rng.normal(0, 0.045, (16, 32, 3)), 0, 1)
    return image


def _tags() -> list[tuple]:
    return [
        (50706, "B", 4, bytes([1, 4, 0, 0]), True),        # DNGVersion
        (50707, "B", 4, bytes([1, 1, 0, 0]), True),        # DNGBackwardVersion
        (50708, "s", 0, "LightTable Synthetic Sensor", True),  # UniqueCameraModel
        (50721, "2i", 9, [part for value in COLOR_MATRIX
                          for part in _rational(value)], True),  # ColorMatrix1
        (50728, "2i", 3, [part for value in AS_SHOT_NEUTRAL
                          for part in _rational(value)], True),  # AsShotNeutral
        (50778, "H", 1, 21, True),                         # CalibrationIlluminant1 D65
        (50714, "H", 1, BLACK, True),                      # BlackLevel
        (50717, "I", 1, WHITE, True),                      # WhiteLevel
    ]


def write_bayer(path: Path) -> None:
    """An uncompressed RGGB mosaic: the ordinary camera case."""
    pixels = scene()
    mosaic = np.zeros((HEIGHT, WIDTH), dtype=np.float64)
    mosaic[0::2, 0::2] = pixels[0::2, 0::2, 0]
    mosaic[0::2, 1::2] = pixels[0::2, 1::2, 1]
    mosaic[1::2, 0::2] = pixels[1::2, 0::2, 1]
    mosaic[1::2, 1::2] = pixels[1::2, 1::2, 2]
    encoded = np.clip(mosaic * (WHITE - BLACK) + BLACK, 0, WHITE).astype(np.uint16)
    tifffile.imwrite(
        path, encoded, photometric="cfa", planarconfig="contig",
        compression=None, software="LightTable test fixture",
        extratags=[*_tags(),
                   (33421, "H", 2, (2, 2), True),              # CFARepeatPatternDim
                   (33422, "B", 4, bytes([0, 1, 1, 2]), True)])  # CFAPattern RGGB


def write_linear(path: Path) -> None:
    """A LinearRaw DNG: already demosaiced, so no CFA interpolation runs."""
    encoded = np.clip(scene() * (WHITE - BLACK) + BLACK, 0, WHITE).astype(np.uint16)
    # tifffile has no name for DNG's LinearRaw, so the numeric photometric
    # code 34892 is written directly.
    tifffile.imwrite(
        path, encoded, photometric=34892, planarconfig="contig",
        compression=None, software="LightTable test fixture",
        extratags=_tags())


def build() -> list[Path]:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    written = []
    for name, writer in (("synthetic-bayer-rggb.dng", write_bayer),
                         ("synthetic-linear.dng", write_linear)):
        path = FIXTURES / name
        writer(path)
        written.append(path)
    (FIXTURES / "README.md").write_text(
        "# Synthetic RAW fixtures\n\n"
        "Written by `tests/make_raw_fixtures.py`, not captured by a camera.\n"
        "They are valid uncompressed DNGs carrying a known colour matrix, a\n"
        "known as-shot neutral, clipped highlights and a noisy flat field, so\n"
        "the capture stage can be checked without shipping a camera original\n"
        "or tracking its licence. Real-format coverage lives in the RAW\n"
        "journey layers described in docs/journey-testing.md.\n\n"
        "Rebuild with:\n\n```sh\npython tests/make_raw_fixtures.py\n```\n")
    return written


if __name__ == "__main__":  # pragma: no cover - operator convenience
    for path in build():
        print(f"{path.relative_to(Path(__file__).parents[1])}  {path.stat().st_size} bytes")
