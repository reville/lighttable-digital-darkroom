# Film profile audit

LightTable reads `stage`, `type`, `channel_model`, `target_print`, and measured
development-time families from the profile JSON. It does not infer profile
roles from filenames or from whether the output medium happens to be paper.
This matters for Kodak 2302, 2383, and 2393, which are print films rather than
paper but still belong in the print-stage selector.

All 23 film profiles below passed the standard-target Rust render smoke in
`tests/stock_smoke.py`. The smoke verifies loading, the intended print/direct-
scan route, non-collapsed tonal range, and neutral RGB output for B&W stocks.

In **Authentic** workflow mode, changing stock also selects its `target_print`
unless Paper Lock is enabled. **Creative** mode preserves an intentional manual
stock/paper combination. Both modes retain explicit physical-stage overrides.
Reversal profiles always route directly to the scanner and ignore print-stage
glare, exposure, preflash, filtration, paper, and paper development.

## Negative stocks

| Film | Default print | Setup review |
|---|---|---|
| Kodak Portra 160 | Kodak Portra Endura | Upstream target; ISO-based 0.10 µm² grain baseline |
| Kodak Portra 400 | Kodak Portra Endura | Upstream target; 0.40 µm² baseline |
| Kodak Portra 800 | Kodak Portra Endura | Upstream target; 0.80 µm² baseline |
| Kodak Portra 800 Push 1 | Kodak Portra Endura | Upstream target; stronger 1.20 µm² baseline |
| Kodak Portra 800 Push 2 | Kodak Portra Endura | Upstream target; stronger 1.60 µm² baseline |
| Kodak Gold 200 | Kodak Portra Endura | Upstream target |
| Kodak Ultramax 400 | Kodak Portra Endura | Upstream target |
| Kodak Ektar 100 | Kodak Portra Endura | Upstream target |
| Kodak Professional T-MAX P3200, EI 3200 | Kodak 2302 | LightTable profile digitized from [Kodak F-4001](https://business.kodakmoments.com/sites/default/files/files/products/F4001.pdf); T-MAX Developer, small tank, 12 min at 20 C; calibrated 1.20 µm² grain baseline |
| Kodak Double-X 5222 | Kodak 2302 | Upstream B&W data; missing print target supplied by LightTable; film development 4, 5, 6.5, 9, or 12 min |
| Kodak Verita 200D | Kodak 2383 | Upstream cinema target |
| Kodak Vision3 50D | Kodak 2383 | Upstream cinema target |
| Kodak Vision3 250D | Kodak 2383 | Upstream cinema target |
| Kodak Vision3 200T | Kodak 2383 | Upstream cinema target; the daylight smoke target is intentionally cooler/darker than daylight stocks |
| Kodak Vision3 500T | Kodak 2383 | Upstream cinema target; the daylight smoke target is intentionally cooler/darker than daylight stocks |
| Fujifilm C200 | Fujifilm Crystal Archive Type II | Upstream target |
| Fujifilm X-Tra 400 | Fujifilm Crystal Archive Type II | Upstream target |
| Fujifilm Pro 400H | Fujifilm Crystal Archive Type II | Upstream target |

## Reversal stocks

These are scanned directly; a print profile is neither selected nor rendered.

| Film | Setup review |
|---|---|
| Fujifilm Velvia 100 | Upstream positive profile, direct scan |
| Fujifilm Provia 100F | Upstream positive profile, direct scan |
| Kodak Ektachrome 100 | Upstream positive profile, direct scan |
| Kodak Kodachrome 64 | Upstream positive profile, direct scan |
| Kodak Tri-X Reversal 7266 | Upstream B&W positive profile, direct scan; LightTable applies a local -1.5 EV smoke-target calibration to restore highlight separation in the current Rust scan path |

## Print-stage profiles

The color choices are Kodak Portra Endura, Endura Premier, Supra Endura,
Ultra Endura, Ektacolor Edge, Vision 2383, Vision Premier 2393, and Fujifilm
Crystal Archive Type II. Kodak 2302 is the B&W print-film choice and exposes
its measured 2, 3.5, 5, 7, and 9 minute development family (5 minutes by
default). LightTable filters the selector by channel model, so a B&W negative
cannot silently render through color paper or vice versa.

Endura Premier, Supra, Ultra, Ektacolor Edge, and Vision Premier 2393 remain
intentional alternate print looks rather than automatic targets. The default
pairings above preserve each film profile's upstream `target_print` metadata.

## Engine boundary

The vendored Python package has no compatible B&W profile schema. T-MAX
P3200, Double-X, Tri-X Reversal, and Kodak 2302 therefore require the Rust
engine. The UI disables Python for those stocks, the server enforces the same
rule, and full-resolution export uses Rust when a B&W profile is selected.

Kodak publishes diffuse RMS granularity 18 for P3200. Spektrafilm instead
models grain with particle area, so LightTable's 1.20 µm² P3200 baseline is an
explicit visual calibration, not a claimed conversion of that measurement.

## Physical scale and modeled output

The selected film format supplies the exposed-image long edge used by the
engine's micrometre-per-pixel calculation: 35 mm, 56 mm for 645/6×6, 70 mm for
6×7, and 127 mm for 4×5. Grain, halation, and diffusion therefore change their
pixel footprint with format and output resolution. The grain slider multiplies
the selected stock's particle-area baseline; it does not replace it. The 1:1
viewer is the intended grain-proofing view.

`Clean scan`, `Neutral print scan`, and `Soft optical print` are coherent,
named starting points for scan MTF, sharpening, print flare, and preflash. They
are deliberately labeled modeled and do not claim to reproduce a Frontier,
Noritsu, drum scanner, or named lab. Likewise, the free Development Contrast
control changes density-curve contrast; actual pushed Portra variants and the
listed B&W development times are the calibrated process choices.

Print glare is applied at `print_render`, after the negative and paper route;
film-render glare remains disabled. The baseline roughness is zero so Python
and Rust are deterministic and directly comparable. The UI's glare amount is
a scalar over the selected output recipe, not a new stochastic texture.

See `calibration/README.md` for the reference-corpus metadata, deterministic
target, and pair-measurement process required to promote a modeled recipe to a
measured endpoint.
