# CLI and agent control milestone

Status: **built**, 2026-09-04. Companion to
[WORKFLOW-ROADMAP.md](WORKFLOW-ROADMAP.md) (catalog, migration, culling: built),
[DEVELOP-ROADMAP.md](DEVELOP-ROADMAP.md) (develop-tool parity: built), and
[GAPS-ROADMAP.md](GAPS-ROADMAP.md) (remaining field gaps: built).

Implementation landed as a standard-library HTTP client and MCP surface,
strict and structured server requests, protected per-instance discovery,
uniform jobs, rendered programmatic inspection, and a live window bridge with
origin-labelled toast and Undo. The optional native window-capture endpoint in
3.4 and catalog-free `render --file` remain deferred, as scoped below.

This milestone makes everything LightTable can do reachable from a shell and
from an AI coding agent, without taking the app away from the person sitting in
front of it. It has three layers, built in that order:

1. **An API a program can rely on.** The HTTP server already does the work;
   it needs to be findable, to say no to bad input instead of quietly fixing
   it, to tell the open window when something changed behind its back, and to
   report long jobs in one shape.
2. **A `lighttable` command.** A thin client over that API, with a JSON mode,
   selectors that name sets of photos, a dry run for anything destructive,
   and a way to look at the result of an edit without a browser.
3. **Surfaces for agents.** One manifest that produces the argument parser,
   a JSON Schema, a Model Context Protocol server, and the generated docs; an
   `AGENTS.md` and a Claude Code skill that teach the vocabulary; and a bridge
   that lets an agent see and drive the live window while the user watches and
   can undo.

Line numbers were read on 2026-09-03 against `server.py` at 4,896 lines and
`web/app.js` at 7,651. Treat them as approximate and search by function name.

## What users get

- **Batch work without a mouse.** Rate, flag, label, keyword, apply a look,
  and export a whole shoot from a script, a Shortcuts action, a folder
  watcher, or a cron job. Every recipe is a JSON file that can be diffed and
  reused.
- **An assistant that works inside the app.** An agent can cull a burst,
  keyword from the local AI index, match a reference scan, or build a preset,
  while the window shows each change as it lands, names who made it in the
  History panel, and offers Undo.
- **A safe place to experiment.** The isolated review profile the
  benchmark and the review server already use becomes a named profile, so an
  agent can test against the demo assets without touching the real catalog.
- **Answers instead of screenshots.** `render`, `analyze`, and `compare`
  give numbers and files for what a photo looks like before and after an
  edit, so a script or an agent can verify its own work.

## Starting point (pre-build)

The inventory that shaped the plan before this milestone landed; nothing here
is a criticism of how the app was built for one window.

- `server.py` is a `ThreadingHTTPServer` on `127.0.0.1` with 79 `/api/*`
  routes dispatched by two `if`/`elif` chains (`Handler.do_GET` near line
  3485, `do_POST` near 3724). Requests and responses are JSON. Every failure
  is a 500 with `{"error": text}` except a handful of 400s and a 503; there is
  no route table, no schema, no request log (`log_message` is silenced), and
  no `Host` or `Origin` check. `_body()` parses JSON whatever the
  `Content-Type` says.
- Every stored object has a cleaner that is the de facto schema:
  `fp.clean_params`, `grade.clean`, `clean_crop`, `edits.clean_masks`,
  `edits.clean_heals`, `edits.clean_optics`, `clean_keywords`,
  `clean_versions`, `clean_preset`, `export_workflow.clean_recipe`,
  `ingest_workflow.clean_plan_request`, `watch_workflow.clean_watch`. They
  coerce silently: an unknown stock becomes the default, an unknown mask type
  becomes `radial`, an out-of-range value is clamped. That is right for a
  slider and wrong for a program, which needs to be told.
- A photo's name is `sourceId:relpath` (`catalog.qualified_name`), with a
  `::lighttable-copy::ident` suffix for virtual copies. Its edit record is
  `status`, `rating`, `label`, `params` (film), `grade`, `crop`, `masks`,
  `heals`, `optics`, `keywords`, `versions`, `provenance`
  (`catalog_entry_for`). `Catalog.query` already filters and sorts in SQL by
  scope, status, minimum rating, label, kind, camera, lens, keyword, date
  range, and full-text query.
- The window's own state lives only in the browser: current photo, selection,
  view mode, pane, zoom, compare, filter and sort. The native host reaches it
  through one string vocabulary, `performNativeMenuCommand` in `web/app.js`
  (about sixty verbs plus the `flag:`, `rating:`, `label:`, `view:`, and
  `pane:` prefixes), and `web/midi.js` moves sliders by dispatching `input`
  and `change` events on the real controls, so undo and history behave as if
  the user had dragged them. Both are the seams the UI bridge will reuse.
- The window learns about changes only by polling the job it started
  (export, import, ingest, merge, denoise, external edit, watch, every two
  seconds). An edit made by another process does not appear until the photo
  is reopened.
- Long jobs are ten module-level dicts (`EXPORT`, `MERGE`, `INGEST`,
  `IMPORT_JOB`, denoise, enhance, external edit, semantic-mask batch, cache
  pregenerate, AI index) each with its own `/status` route and shape. Only
  one export runs at a time.
- The host prefers port 8321 and falls back to a random port
  (`choosePort` in `app/main.swift`). Nothing writes the chosen port down, so
  a script cannot find a running app.
- Command-line precedents: `render_cli.py` (one full-resolution export from
  a file), `bench/benchmark.py` (spawns `server.py` with an isolated
  `LIGHTTABLE_*` environment and waits on `/api/images`; the model for
  auto-starting a headless server), `scripts/camera-list.py`,
  `calibration/measure_pair.py`, `run.sh`, and the review profile in
  `.claude/launch.json`.
- `preset_io._curve_lut` already turns curve points into the 256-entry table
  the shader and exporter share, so a command line can set a tone curve
  without reimplementing the browser's spline.

## Scope

| # | Gap | Where it lands | Days |
| --- | --- | --- | --- |
| 1 | A script cannot find or start the server | Phase 0.1 | 1 |
| 2 | Every error is a 500; bad input is silently coerced | Phase 0.2 | 2 |
| 3 | Nothing tells the window that something changed | Phase 0.3 | 2 |
| 4 | Ten job systems, ten status shapes | Phase 0.4 | 1 |
| 5 | Any local process or web page can mutate the catalog | Phase 0.6 | 1 |
| 6 | No command line | Phase 1 | 7 |
| 7 | A program cannot see what an edit did | Phase 0.8, 1.7 | 2 |
| 8 | No schema, manifest, or docs an agent can load | Phase 2.1–2.2 | 3 |
| 9 | No tool interface for agents | Phase 2.3 | 2.5 |
| 10 | The live window cannot be observed or driven except by the host menu | Phase 3 | 3 |
| + | Tests, packaging, documentation, performance proof | Phase 4 | 4 |

About 28 working days. Phase 0 and Phase 1 alone (14 days) already deliver a
complete, safe command line; Phases 2 and 3 are what turn it into something an
agent can use well.

## Ordering and tracks

Phase 0 comes first and is entirely server work; nothing in it changes what
the window does. Phase 1 depends on 0.1, 0.2, and 0.4. Phase 2 depends on
Phase 1 because the manifest is what the command line is built from. Phase 3
depends only on 0.3 and can run beside Phase 2. Phase 4 runs throughout; the
contract test in 4.1 should exist by the end of Phase 1 so new routes cannot
drift out of the command line.

Two tracks can proceed in parallel after Phase 0: **client** (Phases 1, 2) and
**window** (Phase 3). They meet in the manifest, which lists UI verbs too.

## Rules that apply to every item

1. **The server is the only writer of the catalog.** The command never opens
   `library.sqlite3` itself. The server holds the WAL connections, the render
   and decode caches, the state-file mirror, the watch ledger, and the
   resident Rust engine; a second writer would race all of them. When no
   server is running, the command starts one headless and talks to it.
2. **One vocabulary.** A flag is named after the state key it sets, a
   route field is named after the flag, and the schema names both. No
   translation layer, no friendly synonyms; `--grade exposure=0.3` writes
   `grade.exposure`.
3. **Strict in, honest out.** Requests from the command line and from agents
   validate strictly: a field that would be dropped or clamped is an error
   with the field path and the accepted range. The window keeps the lenient
   default so an old preset still loads.
4. **Everything the command does is visible in the window.** A state change
   records a History step with its origin (`cli`, `agent:<name>`, `watch`),
   the open window refreshes within a second, and Undo reverts it.
5. **Destructive means a plan first.** Trash, move, rename, overwrite on
   export, remove source, clear history, delete the AI index: `--dry-run`
   prints exactly what would happen, and a non-interactive call needs
   `--yes`. Trash still goes through the platform so it stays recoverable;
   the server never unlinks a photograph (`trash_photos`).
6. **Standard out is data, standard error is progress.** `--json` on every
   command, `--jsonl` for lists, stable exit codes (0 ok, 1 error, 2 usage,
   3 no server, 4 validation, 5 job failed, 6 confirmation required).
7. **Nothing new in the render loop.** The command renders with its own
   `client` id and `priority: "background"`, so it never supersedes or
   delays the window's interactive generations
   (`LATEST_GENERATION`, `render_preview`).
8. **Standard library only** in the command and the MCP server. They must run
   on the bundled Python runtime and start in under 100 ms, which rules out
   importing `server.py`, numpy, or PIL on the client path. Image math stays
   on the server.
9. **Third-party product names stay out of code and commits**, as recorded
   in the napkin. Preset and catalog conversion docs are the exception.
10. **Every route is registered in one table** with its schema and its
    mutating/destructive flags. A contract test fails when a route in
    `server.py` is neither mapped by the command nor listed as internal with
    a reason.

## Phase 0 — An API a program can rely on (8 days)

### 0.1 Find the server

- `GET /api/health` returns version, source revision, catalog path, primary
  folder, port, pid, uptime, `rust` availability, engine warm state, model
  availability (`enhance_workflow.capabilities()`), and whether a window is
  connected (from 3.1). It must answer before the catalog scan finishes,
  which `main()` already guarantees for the port.
- On bind, `main()` writes an **instance file**
  `~/Library/Application Support/LightTable/instances/<port>.json`
  (`%LOCALAPPDATA%\LightTable\instances` on Windows) containing the same
  fields plus the per-instance token from 0.6, and removes it at exit
  (`atexit`, beside `RUST_ENGINE.close`). `LIGHTTABLE_INSTANCE_DIR` overrides
  the directory so an isolated profile has its own registry. A reader treats
  a file whose pid is dead as stale and deletes it.
- `LIGHTTABLE_HEADLESS=1` is informational: the server reports it in health
  so a command can tell a user's window from a server it started itself.

### 0.2 Say no properly

- Map exceptions in the two `except Exception` clauses: `KeyError` and
  `ValueError` → 400, `FileNotFoundError` → 404, "already running" → 409,
  "not ready" → 503, everything else stays 500. Bodies become
  `{"error": text, "code": "bad-request" | "not-found" | "busy" | ...,
  "field": "grade.exposure"?}`. The window only reads `.error`, so it is
  unaffected.
- **Strict validation** on request: header `X-LightTable-Strict: 1` (the
  command sends it always; the window never does). A new `validation.py`
  wraps each cleaner: run it, diff input against output, and report
  `rejected` (keys dropped), `clamped` (values changed), and `unknown`
  (keys the cleaner does not know) with paths. In strict mode any entry
  is a 400; in lenient mode the same report rides along as `warnings` so a
  script that chose leniency can still see it. Anchors: `/api/state`,
  `/api/state/bulk`, `/api/presets`, `/api/export`, `/api/export-recipes`,
  `/api/watch`, `/api/ingest`, `/api/catalog/collections` (smart rules),
  `/api/metadata`.
- The mask cleaner's `radial` fallback for an unknown type and the film
  cleaner's default-stock fallback are the two places the differ must
  catch first; add tests for both.
- Optional request log: `LIGHTTABLE_REQUEST_LOG=path` appends one JSON line
  per request (method, path, status, ms, client, origin). Off by default;
  the benchmark stays quiet.

### 0.3 Tell the window

- `GET /api/events` is a server-sent-events stream. Events:
  `state` (`names`, `fields`, `origin`, `client`), `library` (`reason`:
  scan, source, collection, import, ingest, watch), `job` (the 0.4 record),
  `ui.command` and `ui.state` (Phase 3), and `resync` when a subscriber
  fell behind. A new `events.py` holds a bounded queue per subscriber (drop
  oldest, then send `resync`) and caps subscribers at eight; each SSE
  connection occupies one handler thread, which is fine at that count.
- Emit from the write paths: `/api/state`, `/api/state/bulk`,
  `/api/metadata`, `/api/metadata/bulk`, `/api/history`,
  `catalog_sources_action`, `catalog_collections_action`, `update_library`,
  `ScanService` completion, `WatchService._handle`, catalog import, ingest,
  presets, export recipes, and every job transition.
- `web/events.js` subscribes once per page. On `state` for the current
  photo from a `client` that is not this page, it refetches `/api/state`
  and re-renders; on `library` it calls `reloadLibrary()`; on `job` it
  updates the matching progress UI. The existing pollers stay as the
  fallback when `EventSource` is unavailable, then retire one at a time
  once each event path is proven.

### 0.4 One job record

- `jobs.py` defines the record: `id`, `kind`, `state`
  (`queued`/`running`/`done`/`failed`/`cancelled`), `progress`, `total`,
  `started`, `finished`, `errors`, `log` (bounded), `result`. Each existing
  job system registers itself and updates the record where it updates its
  dict today, so `EXPORT`, `MERGE`, and the others keep working unchanged.
- `GET /api/jobs`, `GET /api/jobs/<id>`, `POST /api/jobs/<id>/cancel`
  (delegating to the existing cancel routes where one exists, otherwise
  409 "not cancellable"). The `/status` routes stay for the window.
- Export gains a job id in its `start_export` reply and keeps its
  one-at-a-time rule; a second request returns 409 with the running job id
  instead of a 200 that says `error`.

### 0.5 Attribution

- `/api/state` and `/api/state/bulk` accept `origin` and `label`. When
  `origin` is anything other than the window's own, the server records
  `cat.add_history(image_id, label, state, origin=origin)` itself, so a
  command-line edit appears in the History panel exactly like a slider move
  and Undo reverts it. The window keeps recording its own steps.

### 0.6 Local hardening

The server accepts any POST from any local process, and because `_body()`
ignores `Content-Type` and no `Origin` is checked, a hostile page in the
user's browser can also reach `127.0.0.1:8321` with a cross-site request
that needs no preflight. Widening the automation surface is the moment to
close this.

- Reject requests whose `Host` is not the bound address and port.
- Reject state-changing requests that carry an `Origin` header other than the
  server's own origin. The window is same-origin and the command sends none,
  so neither notices.
- Issue a per-instance token at start, place it in the instance file, set it
  as a `SameSite=Strict` cookie when `/` is served, and require it (cookie or
  `X-LightTable-Token`) on every mutating route. The command reads it from
  the instance file, so there is no user-visible setup. This is Decision 5.

### 0.7 Lookups that were buried

- `GET /api/options` returns stocks, papers, profile catalog, output recipes,
  `DEFAULT_PARAMS`, `grade.DEFAULTS`, curve and HSL keys, `ADVANCED_KEYS`,
  label and status values, mask kinds, media formats, provenance, and rust
  availability: the same objects `do_GET` assembles for `/api/images`
  (near line 3510) without the image page. `lighttable film stocks` and
  `lighttable schema` read this.
- `GET /api/resolve?path=/abs/file.RAF` maps a filesystem path to its
  qualified name and image id by longest matching source path, so a command
  can accept paths the shell completes.

### 0.8 Eyes for a program

- `POST /api/render/file` `{name, w, format: "png"|"jpeg", state?, before?}`
  renders through `render_preview` and `apply_preview_edits` with the stored
  state or a supplied one and returns the encoded image. `before` returns
  the source preview the Compare view uses.
- `POST /api/analyze` `{name, w?, state?, reference?, regions?}` returns
  mean and percentile RGB, a 64-bin luminance histogram, clipped shadow and
  highlight fractions, and, with `reference`, the same mean RGB, luminance,
  pixel error, and approximate ΔE76 the Reference Match panel reports
  (`match-exposure`, `calibration/measure_pair.py`). Optional `regions` are
  normalised rectangles so an agent can ask about a face or a sky.
- `POST /api/render/compare` composes before/after or two photos side by
  side or as a wipe at a given split, returning one PNG. Composition stays on
  the server so the command needs no image library.

## Phase 1 — The `lighttable` command (7 days)

### 1.1 Layout

- `app/lighttable_cli/` with `__main__.py`, `client.py` (HTTP, retries,
  strict header, token), `instances.py` (discovery, auto-start, lifetime),
  `selectors.py`, `output.py` (tables, JSON, JSONL, progress), `manifest.py`
  (Phase 2.1; from day one every command is declared there), and
  `commands/` split by domain.
- Launcher `app/lighttable` (exec the venv or bundled Python with
  `-m lighttable_cli`), `scripts/install-cli.sh` to symlink it into
  `~/.local/bin` or `/usr/local/bin`. The bundle carries
  `Contents/MacOS/lighttable-cli`; the Windows package carries `lighttable.cmd`.
- Startup budget 100 ms cold, measured in the tests.

### 1.2 Finding or starting a server

Resolution order: `--url`, `LIGHTTABLE_URL`, the instance registry
(one live instance: use it; several: `--catalog` or `--port` picks; none:
auto-start). Auto-start runs `server.py` headless the way
`bench/benchmark.py` does, with `LIGHTTABLE_PARENT_PID` set so the server
exits with the command unless `--daemon` was asked, in which case
`lighttable stop` ends it. `--profile review` selects the isolated
demo-assets profile from `.claude/launch.json` as a named profile;
`--profile default` is the user's real catalog and is never chosen
implicitly by an auto-start unless the user's window is already running it.
The command refuses to start a second server against a catalog file that a
live instance already holds.

### 1.3 Addressing photos

- A **photo reference** is a qualified name, `#<imageId>`, or a filesystem
  path (resolved by `/api/resolve`). `@current` and `@selection` resolve
  through the UI bridge (Phase 3) when a window is connected.
- `--where` takes `key=value` pairs that map one-to-one onto
  `Catalog.query`'s spec: `status`, `rating>=`, `label`, `kind`, `camera`,
  `lens`, `keyword`, `from`, `to`, `q` (full text), `source`, `folder`,
  `collection`, with `--sort field[:desc]`, `--limit`, `--offset`. No new
  query language is invented; a filter the catalog cannot run is a usage
  error.
- `--names-from file` and standard input (one name per line) so one
  command's `--jsonl` output feeds the next.

### 1.4 Output and exit codes

Human tables on a terminal, `--json` for one object, `--jsonl` for lists,
`--quiet` for nothing but exit status, progress and warnings on standard
error. Exit codes as in rule 6. Field names in JSON are exactly the API's.

### 1.5 The command tree

Every route is accounted for below; the contract test in 4.1 enforces it.

| Command | Routes | Notes |
| --- | --- | --- |
| `serve`, `status`, `instances`, `stop`, `open`, `doctor` | `/api/health`, instance files | `doctor` checks engine, models, helper binaries, catalog integrity, stale instances, port conflicts |
| `sources list\|add\|remove\|rename\|favorite\|rescan` | `/api/catalog/sources`, `/api/catalog/scan`, `/api/catalog/folders` | `remove` retires, never deletes edits (napkin) |
| `folders create\|rename` | `/api/folders` | subfolders inside a source; photos move with `files move` |
| `photos list\|show\|find\|path\|thumb\|orig` | `/api/catalog/query`, `/api/state`, `/api/resolve`, `/api/thumb`, `/api/orig`, `/api/exif` | `show` prints the full edit record; `orig -o` writes the source preview |
| `rate`, `flag pick\|reject\|clear`, `label` | `/api/state`, `/api/state/bulk` | accept refs or `--where`; `--advance` is a UI verb, not here |
| `keywords list\|add\|remove\|set\|rename` | `/api/state`, `/api/catalog/keywords` | hierarchical `Parent > Child` paths |
| `metadata get\|set\|bulk` | `/api/metadata`, `/api/metadata/bulk`, `/api/exif` | IPTC fields and GPS |
| `edit get\|set\|patch\|reset\|copy\|paste` | `/api/state` | see 1.6 |
| `edit mask list\|add\|remove\|set`, `edit heal …`, `edit optics …`, `edit lens show`, `edit level\|upright` | `/api/state`, `/api/mask/semantic`, `/api/lens-profile`, `/api/geometry/auto` | semantic kinds need the Vision helper; without it the command reports unavailable, never a silent `radial` |
| `film stocks\|papers\|profiles\|recipes\|defaults` | `/api/options` | what the film stage accepts |
| `raw-default get\|save\|delete` | `/api/raw-default` | per-camera RAW development defaults |
| `presets list\|show\|apply\|save\|delete\|import\|export` | `/api/presets`, `/api/presets/import`, `/api/presets/export` | import reads `.ltpreset`, `.xmp`, `.lrtemplate`, `.costyle` and prints the mapped/skipped report |
| `versions list\|save\|restore\|delete` | `/api/state` | named checkpoints |
| `history list\|show\|restore\|clear` | `/api/history`, `/api/history/state`, `/api/history/clear` | `restore` writes the step's state through `/api/state` with origin |
| `render`, `analyze`, `compare` | `/api/render/file`, `/api/analyze`, `/api/render/compare`, `/api/render`, `/api/render/image`, `/api/neutral` | see 1.7 |
| `export run\|status\|recipes list\|save\|delete` | `/api/export`, `/api/export/status`, `/api/export-recipes`, `/api/jobs` | `run --which approved\|rated\|all`, refs, or `--where`; `--recipe` or inline fields; `--wait` default |
| `collections list\|create\|create-smart\|add\|set\|delete` | `/api/catalog/collections`, `/api/library` | smart rules are the `Catalog.query` filter dict |
| `stacks create\|toggle\|unstack`, `virtual-copy create\|delete` | `/api/library` | |
| `import catalog inspect\|run`, `import sidecars`, `sidecars write` | `/api/import/catalog`, `/api/import/status`, `/api/import/sidecars`, `/api/sidecars/write` | `--root-map old=new`, `--options` as in the dialog; read-only copy of the source catalog |
| `ingest sources\|scan\|plan\|run\|cancel\|status` | `/api/ingest/*` | `plan` prints the verified-copy plan before `run` |
| `watch list\|add\|remove\|status` | `/api/watch`, `/api/watch/status` | |
| `merge hdr\|panorama\|focus` | `/api/merge`, `/api/merge/status` | 2–9, 2–20, 2–60 originals |
| `denoise run\|cancel\|status`, `enhance capabilities\|run` | `/api/denoise*`, `/api/enhance*` | refuses when the model is absent (napkin rule) |
| `external-edit start\|status` | `/api/edit-external`, `/api/edit-external/status` | |
| `files move\|rename\|trash\|reveal\|duplicates` | `/api/photos/move`, `/api/photos/rename`, `/api/photos/trash`, `/api/photos/reveal`, `/api/catalog/duplicates` | destructive: plan, `--yes`; trash is relayed to the host when a window is connected, otherwise the command uses the platform's own trash and reports unavailable where none exists |
| `catalog backup\|stats\|folders\|scan-status` | `/api/catalog/backup`, `/api/catalog`, `/api/catalog/folders`, `/api/catalog/scan` | |
| `cache pregenerate\|cancel\|status`, `masks batch\|cancel\|status` | `/api/cache/pregenerate*`, `/api/batch/semantic-masks*` | |
| `ai-index status\|enable\|disable\|rebuild\|clear\|results` | `/api/ai-index*` | |
| `soft-proof profiles\|render` | `/api/soft-proof/profiles`, `/api/soft-proof` | |
| `prefs get\|set` | `/api/prefs` | typed keys from the schema; unknown keys rejected in strict mode |
| `match-exposure` | `/api/match-exposure` | physical-controls-only match, as the panel |
| `jobs list\|show\|wait\|cancel` | `/api/jobs*` | |
| `ui …` | `/api/ui/*`, `/api/events` | Phase 3 |
| `schema`, `completion`, `mcp` | `/api/options`, manifest | Phase 2 |

Internal, listed with reasons rather than mapped: `/api/render/native` and
`/api/render/helper` (Metal surface transport), `/api/edit/image` and
`/api/refine` (browser progressive-preview plumbing that `render` drives
through `/api/render/file`), `/api/perf/export-one` (benchmark),
`/api/video` (range streaming; `photos path` gives the file),
`/api/calibration/target.png` (mapped later as `calibration target` if the
calibration workflow grows a command).

### 1.6 The edit grammar

```
lighttable edit set PHOTO... [--where ...] \
  --grade exposure=0.3 contrast=-0.1 \
  --film stock=kodak_portra_160 grain_amount=0.8 profile_enabled=true \
  --crop 0.1,0.1,0.8,0.8 --rotate 90 \
  --curve L=0,0;0.25,0.2;1,1 --hsl red.sat=-0.2 \
  --patch changes.json | --replace state.json \
  --from OTHER --include film,grade,crop,masks,heals,optics \
  --preset "Name" [--layer] \
  --reset film|grade|crop|masks|heals|optics|all \
  --label "Warm up the shadows" --origin agent:claude
```

`set` merges; `--replace` writes a whole record; `--patch` applies a JSON
merge patch. Curve points go through `preset_io._curve_lut` on the server so
the shader, the exporter, and the command share one table. `--from` copies
the chosen groups from another photo, which is copy/paste settings. `--preset`
applies a style or layers a tool preset (`--layer`), matching the panel.
Every write goes through `/api/state` with strict validation and an origin.

### 1.7 Looking at the result

```
lighttable render PHOTO -o after.png --width 1400 [--before] [--state s.json]
lighttable analyze PHOTO [--reference OTHER] [--region 0.2,0.1,0.4,0.3] --json
lighttable compare PHOTO --before -o pair.png [--wipe 0.5]
lighttable compare PHOTO --against OTHER -o pair.png
lighttable parity PHOTO
```

`parity` renders the preview and runs the export path at the same width,
reporting the maximum channel difference, which is the check
`tests/test_export_parity.py` performs and the one an agent editing
`grade.py` or `web/gl.js` needs to run.

### 1.8 Long jobs

Export, merge, import, ingest, denoise, enhance, and external edit wait by
default, drawing progress on standard error from `/api/jobs/<id>` (or the
event stream when available), with `--timeout`. `--no-wait` prints the job
id; `jobs wait ID` resumes waiting. A failed job exits 5 and prints the
job's `errors`.

## Phase 2 — Surfaces for agents (5.5 days)

### 2.1 One manifest

`lighttable_cli/manifest.py` declares every command: name, summary, one-line
"when to use", arguments (type, enum, default, required, repeatable),
route(s), `mutating`, `destructive`, `needs_window`, and examples. The
argument parser is built from it, `lighttable schema` emits it as JSON Schema
(draft 2020-12) alongside the object schemas, `lighttable mcp` publishes it
as tools, and `scripts/gen-cli-docs.py` writes `CLI.md` from it. A test fails
when the generated `CLI.md` is stale, the way the camera list is checked.

### 2.2 Object schemas

A pure `schema.py` (importable without numpy) describes `state`, `params`,
`grade` (scalars with ranges from `grade.RANGES`, curves as 256-entry
tables, `hsl` by band, `pointColor`, `colorGrading`), `crop`, `mask`
(component kinds and `combine`), `heal`, `optics`, `preset`, `recipe`,
`watch`, `ingest request`, `query spec`, `collection rules`, and the job
record. Parity tests assert that every key a cleaner knows appears in the
schema and that every schema key survives its cleaner: the same discipline
`grade.py` and `web/gl.js` keep with `paritytest.html`. `/api/options`
serves the enumerations so the schema never hardcodes a stock list.

### 2.3 An MCP server

`lighttable mcp` speaks JSON-RPC over standard input and output for the
subset the protocol needs (`initialize`, `tools/list`, `tools/call`,
`resources/list`, `resources/read`), implemented with the standard library
(Decision 3). Tools are the manifest with `readOnlyHint` and
`destructiveHint` annotations; destructive tools return the dry-run plan
unless called with `confirm: true`. `render` and `compare` return `image`
content so the agent sees the photo; `analyze` returns numbers. Resources
expose the schema, `AGENTS.md`, and the current UI state. One-line setup for
Claude Code, Claude Desktop, Codex, and Cursor is documented in `CLI.md`.

### 2.4 Teaching the vocabulary

- `AGENTS.md` at the repository root and in `app/`: how to find or start a
  server, the review profile for experiments, why the catalog has one writer,
  the photo reference grammar, the safety rules, and six recipes. It points
  at `lighttable --help` and `lighttable schema` rather than repeating them.
- `.claude/skills/lighttable/SKILL.md` carries the same guidance for Claude
  Code with the trigger phrases (cull, rate, export, preset, render, catalog).
- `CLI.md`, generated, is the reference.

### 2.5 The cookbook

Recipes, each a tested script under `examples/`: cull a shoot (reject blur
by `analyze`, pick by rating, trash rejected with a plan); apply a look to a
folder and export with a recipe; migrate a catalog headlessly and report the
skips; match a reference scan; keyword from the local AI index results;
verify preview/export parity after a render change; set up a watched folder
with a preset for tethered work.

## Phase 3 — Observe and drive the live window (3 days)

### 3.1 What the user sees

`web/ui-bridge.js` posts a debounced `POST /api/ui/state` whenever the
current photo, selection, visible order length, view mode, pane, zoom,
compare, filter, sort, or active tool changes, tagged with the page's client
id. `GET /api/ui/state` returns the latest report and its age;
`lighttable ui state --json` prints it, and `@current` and `@selection`
resolve from it.

### 3.2 Driving it

`POST /api/ui/command {command, args, timeout}` relays through the event
stream as `ui.command {id, ...}`; the page executes it and answers with
`POST /api/ui/result {id, ok, result}`; the server completes the pending
request or times out with 504. The vocabulary is `performNativeMenuCommand`'s
existing set (`flag:`, `rating:`, `label:`, `view:`, `pane:`, undo, redo,
next and previous photo, zoom, compare, survey, and the rest) plus new verbs:
`goto NAME`, `select set|add|remove|clear NAMES...`, `filter …`, `sort …`,
`mask show ID`, `overlay on|off`, `slider KEY=VALUE` (through the same
control-dispatch path `web/midi.js` uses, so undo and history behave as if
the user moved it), `toast TEXT`, `reveal`. `performNativeMenuCommand`
becomes the one dispatcher shared by the menu, MIDI, and the bridge.

### 3.3 Keeping the user in charge

An external change shows a toast naming its origin with an Undo button; the
History panel shows the origin on each step. A preference, "Allow automation
to control this window", gates `ui.command` execution (state reports and
catalog edits are unaffected; they are already visible and undoable). Its
default is Decision 4.

### 3.4 Window capture (optional)

A host command `captureWindow` in `app/main.swift` writes a PNG of the
window and `lighttable ui screenshot -o` fetches it; Windows reports it
unavailable. Headless work uses `render` instead.

## Phase 4 — Proof, packaging, documentation (4 days)

### 4.1 Tests

- Unit: parsing, selectors, output formatting, exit codes, instance
  discovery with stale pids, strict-validation differ against each cleaner.
- Integration: start `server.py` on `demo-assets` with an isolated
  environment exactly as `bench/benchmark.py` does, then run the command end
  to end: add a source, list, rate, `edit set`, `render`, `analyze`, save
  and apply a preset, `export run --wait`, create a collection, `history
  restore`, and confirm the catalog contents and the export sidecar.
- Contract: every `/api/` string in `server.py` appears in the manifest or
  in the internal list with a reason; every manifest route exists in
  `server.py`.
- Schema parity (2.2), MCP smoke (`initialize`, `tools/list`, one call),
  event stream (a state write reaches a subscriber; a slow subscriber gets
  `resync`), `Host`/`Origin` rejection with the window and the command still
  working, token enforcement, instance file lifecycle.
- Optional browser test through the existing Playwright harness: a UI
  command changes the page and `ui state` reflects it.

### 4.2 Packaging

`build-app.sh`, `scripts/build-release.sh`, `scripts/update-personal-app.sh`,
and `scripts/windows/build-release.ps1` carry the launcher; the bundle
contract tests (`tests/test_personal_build.py`, the bundle test in
`tests/test_performance_contracts.py`) assert its presence.
`scripts/install-cli.sh` is documented in the README.

### 4.3 Documentation

README gains "Command line and agents"; `CLI.md` is generated; the docs site
gets a page when the command ships in a release.

### 4.4 Performance proof

`bench/benchmark.py` before and after Phase 0: slider, render, library, and
export numbers within noise, since the event hooks sit outside the render
path. Command cold start at or under 100 ms, `/api/state` latency
unchanged, and the strict differ under one millisecond per request.

## Decisions to make

1. **The command's name.** `lighttable` is recommended: it matches the
   `LIGHTTABLE_*` environment, the bundle identifier, and the Application
   Support directory. A `filmlab` alias is a one-line addition later.
2. **Thin client over HTTP, or an in-process library command.** Thin client
   is recommended: one catalog writer, the warm GPU engine and caches, and
   live updates in the open window all come for free, and the command stays
   dependency-free. In-process would be faster for a single headless render
   but would mean two writers whenever the app is open.
3. **MCP with the standard library, or the `mcp` package.** Standard library
   is recommended: the protocol subset needed is small, the bundled runtime
   lock stays unchanged, and startup stays fast. Revisit if the protocol's
   streaming features become worth having.
4. **Automation control of the window on by default, with the toast and
   Undo, or off until enabled.** On is recommended for a local tool whose
   changes are all visible and reversible; the preference exists either way.
5. **Require the per-instance token for mutating requests.** Yes is
   recommended, together with the `Host` and `Origin` checks that need no
   token at all. The command reads the token from the instance file, so
   nothing changes for the user.
6. **Whether `render` should accept files outside the catalog.**
   `render_cli.py` already covers file-level full-resolution renders for
   engine work; keep it, and add `lighttable render --file` only if the
   catalog-free path is asked for.

## Acceptance for the milestone

- Every `/api/*` route is reachable from `lighttable` or listed as internal
  with a reason, and the contract test enforces it.
- A fresh agent session can, using only `lighttable --help`,
  `lighttable schema`, and `AGENTS.md`: find or start a server, list
  photos, apply an edit, look at the result with `render` and `analyze`,
  export, and undo.
- An edit made by the command appears in the open window within one second
  and in the History panel with its origin; Undo reverts it.
- Strict mode rejects an unknown stock, an out-of-range grade value, and an
  unknown mask type with the field path, instead of substituting.
- Two servers never open one catalog; a stale instance file is cleaned up.
- Cross-site local POSTs are rejected while the window and the command keep
  working.
- Benchmark numbers unchanged within noise; command cold start at or under
  100 ms.

## Not in this milestone

- A scripting language or plugin API inside the app; the command and MCP
  server are the extension points.
- Access from other machines, authentication beyond the local token, or
  multiple users.
- Driving the window through screenshots or pixel automation; the Playwright
  harness stays a test tool.
- Windows semantic masks in headless mode (no local segmentation runtime;
  reported unavailable, as in GAPS-ROADMAP).
- Any AI editing feature of its own. This plan gives an agent the levers,
  the eyes, and the manners; it does not ship an agent.
