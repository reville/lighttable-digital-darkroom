# Recorded journey review

On-demand native visual review, starting with browse, zoom and edit. Run this
when requested, rather than on every commit. This is a review aid, not an
exhaustive test or an automated certification of visual quality.

The runner copies the three CC0 photo fixtures into a disposable folder and
isolates preferences, catalog, caches, presets and instance registration. It
launches an explicit developer bundle from this checkout. It never launches
or changes the installed personal app. ScreenCaptureKit records only the
unique main window owned by the new process, at a requested 60 fps. Recording
must deliver a frame before the journey starts. Actual timestamps, frame counts
and dropped frames are retained; requested frame rate is not proof of cadence.

## Run

Build a developer bundle with a distinct identifier:

```sh
LIGHTTABLE_BUNDLE_IDENTIFIER=org.lighttable.recorded-review bash build-app.sh
.venv/bin/python scripts/visual-review/run.py --preflight
.venv/bin/python scripts/visual-review/run.py --allow-foreground
```

The native launch activates its window. Only pass `--allow-foreground` after
Nicholas has authorized a foreground testing window, or on a dedicated test
machine. Screen Recording permission must already be enabled. Preflight does
not launch the app or open a permission prompt. Runs have a hard time limit,
stop their own app/server/recorder, preserve partial evidence on failure, and
never automatically approve a baseline.

Outputs are in `output/visual-reviews/<timestamp>/`: the original movie,
source/bundle/fixture hashes, app and server logs, recorded steps, measured
capture timing, extracted evidence, journey clips, and `index.html`.

Use `--previous /absolute/path/to/run` to place a prior recording beside the new
one. Comparability requires the same fixture hashes, journey script, viewport,
and capture settings; app changes are expected and recorded. A prior run is
only a comparison, never an implicitly approved baseline.

## Review protocol

1. Check capture coverage and dropped frames first. Missing or truncated video
   makes visual verification NOT DONE even if functional assertions passed.
2. Inspect the browse, zoom and edit clips, including the exploratory section.
   Examine transition frames at native resolution. A contact sheet is an index,
   not sufficient evidence that every frame is good.
3. Inspect flagged transient changes around their timestamps. A flash candidate
   can be an intended edit, navigation, or recorder artifact. Reproduce it before
   calling it a product defect. Unflagged video is not a clean visual bill of health.
4. Record findings in `review.json` with classification `confirmed-bug`,
   `suspected-bug`, `usability-observation`, or `capture-limitation`, plus journey,
   timestamp, evidence filename, reproduction steps, impact, and confidence.
   Set `reviewStatus` to `reviewed` only after actual evidence inspection.
5. Generate the report again with `run.py --report /absolute/path/to/run`.

The script uses the app's UI command executor and DOM slider/pointer events.
It does not prove OS input routing, menu interaction, or every feature. Presets,
RAW decoding, crop, masks, window resizing, export, and restart recovery are
explicit future coverage. The initial explore section interleaves navigation
and zoom; add bounded variations after reviewing the core journeys.

## Reference videos

Use `references.json` in a run folder to attach externally obtained references:

```json
[{"title":"Reference editor zoom workflow", "url":"https://example.org/video",
  "journey":"zoom", "startSeconds":12, "endSeconds":28,
  "version":"Version shown in source", "purpose":"workflow-only",
  "notes":"Tutorial recording; hardware and playback speed unknown"}]
```

Choose the editor and edition requested by the user. Record source URL,
app version/date, exact timestamps, and whether footage is edited. Compare
feedback, discoverability, step count and continuity. Do not infer comparative
latency or rendering quality from unmatched tutorial recordings. Matched
performance comparisons require the same photos, machine, settings, dimensions
and recording method. Missing reference footage is reported as NOT DONE.
