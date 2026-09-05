# LightTable calibration workflow

LightTable separates **profile evidence** from **creative preference**. A
manufacturer curve, a measured scan, and a speed-derived visual grain prior
are not interchangeable evidence.

## Capture a paired reference

1. Photograph the same stable scene on digital RAW and film from the same
   position. Include a colour target, neutral ramp, skin, foliage, saturated
   objects, fine detail, and hard bright/dark edges.
2. Record illuminant, meter reading, bracket offset, lens/filter, film format,
   stock batch, processing lab/chemistry/time/temperature, scanner, scanner
   illuminant, and every scanner software setting.
3. Preserve a neutral high-bit-depth scan before creative lab correction. A
   separate optical print or preferred lab scan can be a perceptual target,
   but label it as such.
4. Add the pair to a corpus manifest based on `corpus.example.json`. Never
   overwrite the source scan with an aligned or graded derivative.

## Evaluate one pair

Align and crop the images first, then run:

```sh
MPLCONFIGDIR=/tmp/lighttable-mpl .venv/bin/python \
  calibration/measure_pair.py candidate.tif reference.tif --output report.json
```

The report includes RGB error, luminance error, and CIEDE2000 distribution.
Inputs default to encoded sRGB. For a wide-gamut file, declare its encoding
with `--candidate-space display_p3` or `--reference-space prophoto`; the tool
converts both into sRGB before perceptual comparison. These switches are
explicit because a missing or incorrect embedded scan profile is itself a
calibration error and should not be guessed away.
Use edge-spread/MTF, grain power spectra or the manufacturer's own granularity
metric, and halo edge profiles as separate spatial evaluations; do not infer
them from colour error.

The in-app **Match** tool is for a physically interpretable starting point. It
adjusts capture exposure and RAW white balance, or print filtration for an
already-rendered source. It intentionally does not manufacture a hidden HSL
or LUT fit.

## Release gate

Run `tests/engine_parity.py` after renderer or parameter-schema work. It checks
the deterministic colour/tone path against fixed Python/Rust tolerances.
Spatial effects need their own scale-aware comparisons at matching film format
and output enlargement.
