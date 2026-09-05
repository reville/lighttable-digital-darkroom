# LightTable custom profiles

These profiles extend the local `spektrafilm-rs` data set. At startup,
`film_pipeline.py` copies each JSON profile into the untracked engine data
directory without changing the upstream profiles.

## Kodak Professional T-MAX P3200

`kodak_tmax_p3200.json` represents EI 3200 in Kodak T-MAX Developer, small
tank, 12 minutes at 20 C. Its relative spectral response and density curve are
digitized from Kodak technical data F-4001 (March 2018). The density data
follow Kodak's published 12-minute T-MAX Developer characteristic curve.

Kodak reports diffuse RMS granularity 18. Spektrafilm's grain particle area is
a different model with no direct conversion, so LightTable supplies a visibly
strong `1.20 um2` baseline as a labeled calibration choice. That setting is
not represented as a laboratory measurement.

Regenerate the tracked JSON with:

```sh
../.venv/bin/python build_kodak_tmax_p3200.py
```
