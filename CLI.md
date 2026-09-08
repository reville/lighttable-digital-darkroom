# LightTable command line

`lighttable` is a standard-library client for the local app server. Its
stdout is data, progress is written to stderr, and all agent-originated
state changes are strict, attributed, visible in the window, and undoable.

## Start

```sh
scripts/install-cli.sh
lighttable status --json
lighttable --profile review photos list --limit 5 --jsonl
```

A running window is discovered from its protected instance file. Use
`--port` or `--catalog` if several instances exist. Supplying the
`--profile review` option may start an isolated headless server for that command.

## Common recipes

```sh
lighttable photos list --where status=pending --jsonl
lighttable photos list --where 'rating>=4' --where focal-length=50 --where from=2026-01-01 --where to=2026-12-31 --jsonl
lighttable rate 5 @current
lighttable flag reject --where 'rating>=1' --where status=pending
lighttable edit set @selection --grade exposure=0.25 --curve 'L=0,0;0.5,0.4;1,1' --label 'Lift exposure'
lighttable render @current -o /tmp/after.png --width 1400
lighttable analyze @current --region 0.2,0.1,0.4,0.3 --json
lighttable export run @selection --destination ~/Pictures/Exports
lighttable ui command nextPhoto
```

## Export delivery

`--destination-mode fixed` writes into one destination folder (the default).
`original-folder-relative` treats `--destination` as a subfolder beside each
original. `preserve-source-hierarchy` retains folders beneath stable source
namespaces, so two sources named Photos cannot collide. Single and batch
exports follow the same rules; `/api/export/preview` returns sample paths.

`--preserve-capture-time` sets file times from EXIF capture time. An embedded
timezone offset wins. Without one, the default preserves export time and
reports a warning; `--capture-time-policy local` explicitly uses this
computer's timezone at the capture date, including daylight saving time.
`--metadata` selects all, all-except-location, copyright, or none;
`--no-sidecar` omits the delivery recipe sidecar.

Use `--no-wait` to get a job ID, then `lighttable jobs cancel ID` to stop it.
Cancellation stops queueing and waits for active work to clean up. Completed
outputs survive. The job becomes terminal only after cleanup; its result
records completed, skipped, cancelledCount and per-file warnings.

## Photo availability and lens profiles

Catalog photos expose `availability`: local or cloud-only. Cloud-only means
macOS reports a dataless placeholder; download it in Finder, then rescan.
Content hashing, metadata extraction and rendering refuse placeholders.

`/api/lens-profile?name=...` returns found, profile, reason and candidates.
Set `optics.profileOverride` to null for automatic selection or an object
with cameraMaker, cameraModel, lensMaker and lensModel from a candidate.
The choice is validated against the camera and recorded focal length.
Ambiguous automatic matches and unavailable overrides stay uncorrected.

## Route coverage

| Command family | API routes |
| --- | --- |
| `status / doctor / open / serve / instances / stop` | `/api/health` |
| `photos list / show / path / thumb / orig` | `/api/images`, `/api/catalog/query`, `/api/state`, `/api/resolve`, `/api/thumb`, `/api/orig`, `/api/exif` |
| `rate / flag / label / edit` | `/api/state`, `/api/state/bulk` |
| `render / analyze / compare` | `/api/render`, `/api/render/file`, `/api/analyze`, `/api/render/compare`, `/api/neutral` |
| `sources / folders` | `/api/catalog/sources`, `/api/catalog/scan`, `/api/catalog/folders`, `/api/folders` |
| `collections / stacks / virtual-copy` | `/api/catalog/collections`, `/api/library` |
| `keywords` | `/api/catalog/keywords`, `/api/state` |
| `metadata / api post` | `/api/metadata`, `/api/metadata/bulk`, `/api/metadata/capture-time`, `/api/exif` |
| `history` | `/api/history`, `/api/history/state`, `/api/history/clear` |
| `versions` | `/api/state` |
| `film / schema` | `/api/options` |
| `raw-default` | `/api/raw-default` |
| `presets` | `/api/presets`, `/api/presets/import`, `/api/presets/export`, `/api/presets/community`, `/api/presets/community/recipe`, `/api/presets/community/install`, `/api/presets/submission` |
| `export` | `/api/export`, `/api/export/status`, `/api/export-recipes` |
| `jobs` | `/api/jobs`, `/api/jobs/<id>`, `/api/jobs/<id>/cancel` |
| `import` | `/api/import/catalog`, `/api/import/report`, `/api/import/status`, `/api/import/sidecars`, `/api/sidecars/write`, `/api/sidecars/status` |
| `ingest` | `/api/ingest`, `/api/ingest/scan`, `/api/ingest/status`, `/api/ingest/sources`, `/api/ingest/cancel` |
| `watch` | `/api/watch`, `/api/watch/status` |
| `merge` | `/api/merge`, `/api/merge/status` |
| `denoise / enhance` | `/api/denoise`, `/api/denoise/status`, `/api/denoise/cancel`, `/api/enhance`, `/api/enhance/capabilities` |
| `external-edit` | `/api/edit-external`, `/api/edit-external/status` |
| `files` | `/api/photos/move`, `/api/photos/rename`, `/api/photos/trash`, `/api/photos/reveal`, `/api/catalog/duplicates` |
| `catalog` | `/api/storage`, `/api/catalog`, `/api/catalog/backup`, `/api/catalog/folders`, `/api/catalog/scan` |
| `doctor / recovery` | `/api/recovery`, `/api/recovery/log` |
| `cache / masks` | `/api/cache/status`, `/api/cache/purge`, `/api/cache/pregenerate`, `/api/cache/pregenerate/status`, `/api/cache/pregenerate/cancel`, `/api/batch/semantic-masks`, `/api/batch/semantic-masks/status`, `/api/batch/semantic-masks/cancel`, `/api/batch/semantic-masks/undo`, `/api/mask/semantic` |
| `ai-index` | `/api/ai-index`, `/api/ai-index/status`, `/api/ai-index/results` |
| `soft-proof` | `/api/soft-proof`, `/api/soft-proof/profiles` |
| `prefs` | `/api/prefs` |
| `match-exposure` | `/api/match-exposure` |
| `lens / geometry` | `/api/lens-profile`, `/api/geometry/auto` |
| `ui` | `/api/ui/state`, `/api/ui/command`, `/api/ui/result`, `/api/events` |
| `route` | `/api/*` |

Internal browser/native routes are declared rather than hidden:

- `/api/desktop-theme` — read-only Linux desktop palette for interface chrome.
- `/api/export/preview` — read-only export dialog delivery example.
- `/api/thumb/rendered` — edit-aware browser thumbnail replacement.
- `/api/render/native` — native surface transport.
- `/api/render/png` — lossless corrected preview transport.
- `/api/render/helper` — browser helper generated from a native surface.
- `/api/render/image` — cached base-render bytes.
- `/api/edit/image` — cached base-edit bytes.
- `/api/refine` — progressive browser preview plumbing.
- `/api/perf/export-one` — benchmark-only export path.
- `/api/video` — range streaming used by the window.
- `/api/calibration/target.png` — calibration UI asset.

## MCP tools

- `lighttable_status` — Inspect the running app.
- `lighttable_photos_list` — List photos matching catalog query fields.
- `lighttable_photo_show` — Read one edit record.
- `lighttable_state_update` — Strictly update one photo and record its origin.
- `lighttable_render` — Render an edited photo.
- `lighttable_analyze` — Measure a rendered photo.
- `lighttable_compare` — Render a visual comparison.
- `lighttable_ui_command` — Drive the visible window.
- `lighttable_api_request` — Call any manifest-covered local API route.

Run `lighttable mcp` as a stdio MCP server. It supports `initialize`,
`tools/list`, `tools/call`, `resources/list`, and `resources/read`.
Resources expose this schema, the agent guide, and current window state.

## Safety

Mutating requests require the token held in the discovered instance file.
The CLI never prints it. Cross-origin requests and incorrect Host headers
are rejected. Destructive generic route calls require `--yes`; photo trash
remains a recoverable native-host operation.
