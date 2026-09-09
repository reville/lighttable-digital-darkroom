# On-demand browser UI audit

Run this occasionally when requested, before a release, or while investigating
visual bugs. **It is deliberately absent from commit hooks, normal test discovery,
CI triggers, build scripts, and `mnb`.** No baseline is approved automatically.
This is a headless Chromium review aid; the application's real HTML/CSS, server,
UI command executor, and WebGL presentation run against disposable photo copies.
It never opens the native app or uses your personal catalog.

## Run

Complete the source dependency setup in [CONTRIBUTING.md](../../CONTRIBUTING.md),
including `engine/data` and the Python runtime. Node and Playwright/Chromium are
also required. Existing benchmark discovery is reused. To install locally:

```sh
npm install --no-save --package-lock=false playwright
npx playwright install chromium
export LIGHTTABLE_PLAYWRIGHT_MODULE="$PWD/node_modules/playwright/index.mjs"
```

Alternatively, discovery uses an existing cached Playwright installation and
macOS headless Chromium. `LIGHTTABLE_BROWSER_EXECUTABLE` can select an exact
Chromium executable. On other platforms Playwright's installed default is used.
Chromium may need permission to launch outside an agent's filesystem sandbox;
headless launch still does not take desktop focus.

From the app checkout:

```sh
# About 80 states: English, Arabic, German, Russian at 1280 × 720.
.venv/bin/python scripts/ui-audit/run.py invariants

# Capture candidate baselines, retaining both original and masked screenshots.
.venv/bin/python scripts/ui-audit/run.py snapshot --locales en ar --timeout 600

# Reproducible, bounded random walks; one walk per locale/viewport.
.venv/bin/python scripts/ui-audit/run.py explore --seed 1234 --steps 80 --timeout 600

# List the full matrix without starting a server or browser.
.venv/bin/python scripts/ui-audit/run.py --profile full --list

# Full matrix is intentionally expensive; shard it by locale/state/viewport.
.venv/bin/python scripts/ui-audit/run.py invariants --profile full --locales ar --timeout 1800

# Small, targeted run.
.venv/bin/python scripts/ui-audit/run.py invariants --locales de ru \
  --viewports 1280x720 1440x900 --states pane-mask-fit settings no-results

# Replay a saved first violation or reduced sequence in fresh isolated data.
.venv/bin/python scripts/ui-audit/run.py --replay /absolute/path/to/case-minimal.json
```

Exit codes: **0** means the selected automatic checks completed without hits,
**1** means review is required (including unapproved/mismatched snapshots), and
**2** means execution failed or coverage is incomplete. Neither 0 nor 1 certifies
visual quality. A missing dependency, exception, browser launch failure, timeout,
or failed state assertion cannot produce a passing run. Use an unused `--output`
directory to name a run; existing outputs are never overwritten.

The overall execution cap is `--timeout` (default 600 seconds, maximum four
hours), plus bounded process teardown and evidence analysis. Exploration also
caps `--steps`, `--minimize-attempts` (default 12), and `--max-frames` (default
600 per walk). All spawned browser/server process groups are stopped on exit,
including timeout and interruption. Keep separate worktrees for separate hunts.

## State matrix

`--profile full` takes the cross product of every shipped locale in
`web/locales/manifest.json` (currently 20), three viewports (1280×720, 1440×900,
1728×1117), and the following states. Arabic uses the application's actual RTL
language initialization, not CSS direction injected by the harness.

| State group | Coverage |
|---|---|
| Panes | Every `section.panel-pane` in the application source: edit, mask, heal, film, crop, presets, history, info |
| Pane zooms | Fit, actual/100%, two steps in, one step back out |
| Views | Grid, detail, before/after compare, selected-photo survey, secondary loupe popup, Help, Settings |
| Stress | No search results, long multilingual filename plus 24 hierarchical keywords |
| Separate libraries | Empty library after setup, actual first-run setup, missing disposable originals after indexing |

Quick mode uses every state with Fit pane zoom, four locales, and the short
viewport. Full mode currently contains 2,640 cases. The generated `--list` is
authoritative if panes or locales change. The matrix is a useful sample, not all
possible combinations of app features, nested sections, themes, fonts or OSes.
The fixture photographs are the repository's attributed CC0 JPEGs, reduced to
1200 pixels for speed. RAW processing belongs to the processing/native suites.
Film is off for deterministic, fast layout checks; the Film pane still renders.

## Probes and exploration

The DOM oracles flag horizontal overflow, clipped text, overlapping controls,
zero-size controls, positive tab order, focusable controls inside aria-hidden
regions, `[hidden]` elements that still paint, language direction, and Fit bounds.
Contrast checks apply WCAG text floors to solid opaque colors only. They skip
unknown compositing, gradients, photos and disabled controls. These are heuristic
candidates, not a complete accessibility or keyboard-navigation audit.
Intentional ellipsis, scroll containers, closed details, nested controls and
occluded overlays are handled conservatively. Custom controls and intentional
low-emphasis text can still produce candidates. Never suppress a finding without
inspecting its original screenshot and documenting why.

Explore uses a seeded command vocabulary (panes, navigation, zoom, compare,
library/filmstrip toggles), actual browser pointer drags, and short command bursts.
Use `--rules fit-geometry hidden-paints` to focus a hunt; omitted rules are
explicitly recorded in the configuration. It probes after each settled action; CDP screencast frames capture intervening
transitions. Initial candidates are retained separately. It stops at the first
*new* signature, saves its screenshot and full sequence before reduction, then
tries deleting actions with a fresh browser and reset photo state each time.
`oneMinimal: true` means every remaining single-action deletion was tried; bounded
partial reductions remain explicitly partial. An initial-state problem may reduce
to zero actions. The browser state reset does not assert process/restart isolation
for arbitrary commands outside this deliberately limited vocabulary.

The native review's A-B-A detector runs on the captured frames. Real frame
timestamps and original JPEG frames are retained. This sampling is not guaranteed
60 fps, captures may reach their frame cap, and uncaptured glitches cannot be
excluded. A-B-A changes can also be intentional navigation, edits or recording
artifacts. Inspect the surrounding frames before reproducing and classifying.

## Evidence and review

Each `output/ui-audits/<timestamp>/` contains:

- `README.md`, `result.json`, and the native review-compatible `review.json` ledger.
- Original app screenshots, masked comparison PNGs, and baseline difference PNGs.
- Source revision/dirty status, source and fixture hashes, browser version,
  machine, viewport, locale, seed and run bounds in provenance/config files.
- Isolated runtime data and logs; exploration sequences, reduced repros, actual
  screencast frames/timestamps, and `frame-analysis.json`.

Keep `reviewStatus: "not-reviewed"` until a person or independent reviewer has
opened the evidence. Add reviewed filenames to `inspectedEvidence`. Each finding
uses the existing native ledger fields: `classification` (`confirmed-bug`,
`suspected-bug`, `usability-observation`, `capture-limitation`), `journey`,
`timestamp`, `evidence`, `reproduction`, `impact`, and `confidence`. Point `journey`
at the case ID. Candidate details and selectors remain in `result.json`.

A confirmed finding should become a focused regression in the normal suite once
its cause is understood; do not promote a heuristic probe hit into a permanent
failure. Assign hunting/fixing and verification to independent contexts when
requested. The author of a fix must not approve its visual baseline.

## Baselines

Baseline approval is a separate, explicit action by Nicholas or an authorized
independent reviewer. The snapshot run must be complete, its ledger marked
`reviewed`, and **every original and masked PNG** listed in `inspectedEvidence`.
Unresolved confirmed/suspected bugs block approval. Never mark a ledger reviewed
just to get this command to succeed.

```sh
.venv/bin/python scripts/ui-audit/run.py \
  --approve-from /absolute/path/to/reviewed-snapshot-run \
  --baseline /absolute/path/to/new-baseline-directory --reviewer 'Reviewer name'

.venv/bin/python scripts/ui-audit/run.py snapshot \
  --baseline /absolute/path/to/approved-baseline-directory
```

Approval records the reviewer, hashes and comparison environment; it refuses to
overwrite a baseline. Comparisons require matching browser/platform, fixture
hashes, device scale and mask policy. App source changes are expected and recorded.
Missing, incompatible or corrupt baselines require review. Pixel comparison uses
a 16/255 per-channel threshold and a 0.1% changed-pixel allowance; these tolerate
small rasterization differences and do not replace inspection. Photo pixels are
masked while their boundaries and surrounding UI remain visible. Original
screenshots always retain the actual photographs.

## Harness self-checks

These are also manual and are not added to the normal test gate:

```sh
.venv/bin/python scripts/ui-audit/test_runner.py
LIGHTTABLE_PLAYWRIGHT_MODULE="$PWD/node_modules/playwright/index.mjs" \
  node scripts/ui-audit/test_probes.mjs
```

Self-checks use small synthetic images/DOM to verify the oracles, matrix, baseline
approval guard, and existing transient detector. They are never app visual proof.

## Native coverage

Use [recorded native review](../visual-review/README.md) in an explicitly authorized
foreground testing window for Metal, native window chrome/insets, live resize,
real trackpad gestures, Reduce Motion and multiple displays. This suite provides
no native proof and never launches or installs a personal build.
