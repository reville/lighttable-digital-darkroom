# LightTable calibration workflow

LightTable separates **profile evidence** from **creative preference**. A
manufacturer curve, a measured scan, and a speed-derived visual grain prior
are not interchangeable evidence.

## LightTable tuned interpretations

The stock menu offers **LightTable tuned** Portra 160, 400, and 800 above the
**Spektrafilm original** stocks. The original rendering remains the default
for new photos and older edits. A photo or native preset stores the base stock,
variant, and tuning version; upstream profile data is kept separate from the
LightTable adjustment.

Version 1 selectively moves source yellow-greens toward green before spectral
reconstruction. It preserves luminance in linear ProPhoto and leaves source
colors outside that hue sector unchanged, including true yellows, skin tones,
neutrals, blues, and cyans. Selecting colors from the source avoids treating
all output yellows alike after the film model has shifted them. The adjustment
is fixed per stock; there are no extra tuning sliders. Paper choices and output
recipes remain available and still affect the result.

This is a visual interpretation informed by reference photographs and numerical
color checks, **not calibration to measured film scans**. Published photographs
support judging whether a stock can retain varied greens, but different scenes,
lighting, processing, scanning, and editing prevent a numerical fit. References
reviewed for this work include [Portra 160 by Bastian Kratzke](https://phillipreeve.net/blog/analogue-adventures-part-10-kodak-portra-160/),
[Portra 400 by Lily Heaton](https://www.lilywanderlust.com/blog/2025/1/rolleiflex-challenge-japanese-garden),
and [Portra 800 by Aukje](https://www.35mmc.com/28/08/2019/kodak-portra-800-my-latest-crush-in-film-photography-by-aukje/).
Those negative scans also differ from a simulated film-to-paper-to-scan output.

Evaluate tuning across foliage, autumn yellows, skin, neutrals, saturated
objects, and several exposures, keeping separate photographs for review after
choosing the adjustment. Check hue continuity, luminance, gamut boundaries,
and Python/Rust agreement. Numerical comparisons to a digital original are
guardrails, not evidence of real-film accuracy: making every color match the
digital source would remove intentional stock differences. A measured film
match requires the paired references below with a defined scan or print target.

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
