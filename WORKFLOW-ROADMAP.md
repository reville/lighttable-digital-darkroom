# Workflow and migration milestone

Status: **built**, 2026-09-03. Written as a plan on 2026-09-02 and implemented
against it; the measured results are at the end of this document. Phases 0-4
are complete. Phase 5 is complete apart from Windows parity for the two
Swift-only helpers, and apart from the Enhance model itself, which is not
bundled and cannot be — see below. Companion to [DEVELOP-ROADMAP.md](DEVELOP-ROADMAP.md),
whose develop-tool parity work is complete.

This milestone covers what still stops a photographer with an existing
catalog-based workflow from moving to LightTable: bringing an existing library
in, holding a large library, the daily ingest / cull / tag / export loop, and
the image-quality features people ask about first. The editing tools are
already close to parity; almost nothing here adds a grade control.

Line numbers were read on 2026-09-02 while `server.py` was being edited in
another session. Treat them as approximate and search by function name.

## Scope

| # | Gap | Where it lands |
| --- | --- | --- |
| 1 | Third-party catalog and sidecar import | Phase 2 (sidecars in 0.4) |
| 2 | Central catalog, several sources at once, large-library scale | Phase 1 |
| 3 | Camera and file coverage, supported-camera list | Phase 0.1, Phase 5 |
| 4 | Card ingest | Phase 3.1 |
| 5 | Color labels, IPTC, GPS, keyword hierarchy, watermark, export metadata | Phase 0.2, 0.3, 3.3 |
| 6 | Culling speed: keys, auto-advance, survey view, A/B compare, trash rejected | Phase 0.2, 3.4 |
| 7 | Persistent per-image edit history | Phase 3.5 |
| 8 | Learned denoise and super-resolution | Phase 4.3 |
| 9 | Auto-straighten and guided upright | Phase 4.2 |
| 10 | A good default rendering when Film is off | Phase 4.1, Phase 5 |
| + | Batch rename | Phase 3.2 |
| + | Video cataloguing, sidecar writing, camera-profile files | Phase 5 |

## Ordering and tracks

Phase 1 (the catalog) is the dependency for most of the rest. Collections
across roots, labels, keyword hierarchy, history, ingest, and rename all need a
store that is not a JSON file inside one photo folder. Phase 0 collects the
work that does not depend on the catalog so a second track can run in
parallel. Phase 2 writes into the catalog, so it follows Phase 1, but its
parser can be developed against fixtures at the same time.

- **Track A (library):** 0.1 → 1a → 1b → 1c → 1d → 2 → 3.1 → 3.2 → 3.5
- **Track B (editing and metadata):** 0.2 → 0.3 → 0.4 → 4.2 → 4.1 → 3.3 → 3.4 → 4.3

Rough sizes are engineering days for one person who knows the codebase.

| Phase | Days |
| --- | --- |
| 0 Quick wins | 8 |
| 1 Catalog | 15–20 |
| 2 Catalog import | 8–10 |
| 3 Daily workflow | 21 |
| 4 Image quality | 15–17 |
| 5 Long tail | open |

## Rules that apply to every item

- **Windows allowlist.** `scripts/windows/build-release.ps1` copies an explicit
  list of Python modules (around lines 74–83) that already omits `edits.py`,
  `semantic_masks.py`, `export_workflow.py`, `library_workflow.py`,
  `merge_workflow.py`, `soft_proof.py`, and all of `film_lab_ai/`. Every new
  module goes on that list, and 0.1 adds a contract test so it cannot be
  forgotten again.
- **Cache identity.** Any parameter that changes decoded pixels goes into
  `color_pipeline.raw_decode_fingerprint()` and bumps its version token. Any
  change to rendered output bumps `RENDER_CACHE_VERSION`.
- **Preview and export parity.** Pixel operations are added to both
  `finish_export()` in `server.py` and `render_cli.py`, with a test that runs
  both on the same input.
- **Naming.** Third-party products are named only where import compatibility
  is documented (0.4 and Phase 2), per the napkin directive.
- **Tests.** `unittest`, inline fixtures as in `tests/test_preset_io.py`,
  `tempfile.TemporaryDirectory()` for filesystem cases. There is no CI job for
  the Python suite today; 0.1 adds one.
- **Phase exit.** Unit tests green, `bench/benchmark.py` before and after on
  the Mac benchmark machine, Windows runtime smoke, README and this document
  updated, napkin updated.

## Phase 0 — Independent quick wins (≈8 days)

### 0.1 File coverage, contract tests, CI (1 day)

- Widen `RAW_EXTS` in `server.py` with the formats the bundled LibRaw 0.22
  already decodes: `.pef .srw .3fr .fff .iiq .rwl .crw .nrw .mrw .erf .mef
  .mos .kdc .dcr .sr2 .srf .raw`. Verify each against LibRaw's list before
  adding; leave Foveon `.x3f` out unless LibRaw confirms support.
- Keep the accepted formats in `media-formats.json`. Python, the macOS and
  Windows shells, the browser payload, ingest, camera-list generation, and the
  benchmark consume that manifest; `ExtensionListContractTests` verifies the
  wiring instead of comparing duplicated constants.
- Add `WindowsAllowlistContractTests`: every top-level module imported by
  `server.py` must appear in the PowerShell allowlist.
- Fix the camera Make bug: `_EXIF_FIELDS` in `server.py` (around line 905)
  includes `Make` but is never used; the live list `platform_image.EXIF_FIELDS`
  lacks it, so `raw_camera_identity()` and `edits.lens_profile_for()` always
  see an empty make. Add `Make` (`Exif.Image.Make`) to `EXIF_FIELDS`, delete
  the dead list, and add a test that the per-camera key contains the make.
- Add `.github/workflows/python-tests.yml` running `python -m unittest
  discover tests` on macOS (the engine-parity script stays optional).

### 0.2 Color labels and culling keys (1 day)

- State: add `label` (`none|red|yellow|green|blue|purple`) to `entry_for()`,
  the `/api/state` allowlist, and the `/api/state/bulk` key filter in
  `server.py`; `clean_label()` beside `clean_keywords()`. Include `label` in
  `smart` rules (`library_workflow.clean_collections`) and its JS twin
  `photoMatchesRules()`.
- UI: a colour bar on grid and strip cells, a `#labelFilter` select beside
  `#ratingFilter`, sort by label, and a label row under the stars in the
  toolbar. `visible()` in `web/app.js` reads the new filter.
- Keys in the handler near `web/app.js` line 4184: `u` → `setStatus('pending')`
  (unflag); `6`–`9` toggle red, yellow, green, blue. Caps Lock (via
  `e.getModifierState('CapsLock')`) or Shift with a digit rates and advances,
  matching what `setStatus()` already does for flags. Update the shortcut list
  in `web/index.html` (`shortcut-details`).

### 0.3 Export: metadata embedding, watermark, sidecar policy (3 days)

Today `color_pipeline.save_export_image()` writes a fresh image with only an
ICC profile: every export is stripped of EXIF and XMP, and every export gets a
`.lighttable.json` sidecar. Client deliveries need copyright in the file and
no sidecar next to it.

- Recipe schema (`export_workflow.clean_recipe()`): add `metadata`
  (`none|copyright|all|all-except-location`), `sidecar` (bool, default true;
  false in a new "Client delivery" builtin), and `watermark`
  (`{enabled, kind: text|image, text, imagePath, anchor, inset, scale, opacity}`).
- Watermark: `export_workflow.apply_watermark(rgb, spec)` applied after
  `color_pipeline.resize_float()` so the mark scales with the output, in both
  `finish_export()` and `render_cli.py`. Text is rasterised with Pillow
  `ImageFont` from the bundled `web/fonts`; images are RGBA PNG composited
  with premultiplied alpha.
- Metadata: new `platform_image.write_metadata(dst, source, policy, fields)`
  using the exiv2 binding already pinned (`exiv2==0.18.1` exposes
  `exifData()`, `iptcData()`, `xmpData()`, `writeMetadata()`). Copy EXIF from
  the source filtered by policy, drop GPS for `all-except-location`, set
  `Exif.Image.Orientation` to 1 because pixels are already oriented, refresh
  pixel dimensions and `Software`, and write `dc:creator`, `dc:rights`,
  `dc:title`, `dc:description`, `xmp:Rating`, `xmp:Label`, `dc:subject`, and
  `lr:hierarchicalSubject` from the catalog fields (labels and IPTC arrive in
  0.2 and 3.3; wire what exists now). Run it after `save_export_image()` in
  both call sites. Confirm the ICC profile survives the rewrite in a test.
- Tests: `clean_recipe` defaults, watermark anchor geometry on a synthetic
  image, and a filesystem test that exports a JPEG and a TIFF, then reads the
  copyright and the ICC profile back with exiv2.

### 0.4 Per-image sidecar import (3 days)

Sidecars written by Lightroom and Camera Raw (`photo.xmp` or `photo.RAF.xmp`)
carry rating, label, keywords, IPTC, GPS, crop, and develop settings. Today the
server only moves them (`server.py` around line 632) and never reads them, and
`preset_io._xmp_attrs()` only sees attribute-form `crs:` keys.

- New module `xmp_sidecar.py` (add to the Windows allowlist) with a real XML
  parser (`xml.etree`) that reads both attribute and element form and returns
  a normalised dict: `rating`, `label`, `keywords` (from `dc:subject`),
  `keywordPaths` (from `lr:hierarchicalSubject`), `title`, `caption`,
  `creator`, `copyright`, `gps`, `orientation`, `crop`, and the flat `crs`
  attribute dict. Also read embedded XMP from JPEG, TIFF, HEIC, and DNG via
  exiv2 `xmpData()` with the same parser.
- Refactor `preset_io` so the crs mapping is callable on a dict
  (`map_crs_settings(attrs) -> (grade, curves, ignored)`), then reuse it here
  for develop settings. Crop maps directly: `CropLeft/Top/Right/Bottom` are
  normalised fractions that match the app's `{x,y,w,h}`; `CropAngle` maps to
  `optics.rotate` when within ±15° and is reported as skipped otherwise. Note
  in the report that the pick flag is never present in sidecars.
- Server: `POST /api/import/sidecars {names|scope, apply:{metadata, develop,
  crop}, conflict: skip-existing|overwrite}` returning a per-image report.
  Until Phase 1 this writes into the existing state entry (keywords, rating,
  status untouched, `label` from 0.2, grade, crop, optics). After Phase 1 the
  same module feeds the catalog and the scanner offers "read sidecar ratings
  and keywords on first sight" as a preference (metadata on by default,
  develop settings off).
- UI: a Library menu item "Import sidecar metadata…" with a summary dialog
  built on the `#nameDialog` modal pattern.
- Tests: `tests/test_xmp_sidecar.py` with inline XMP in both forms, hierarchical
  keywords, crop mapping, both sidecar naming forms on disk, and a conflict
  policy case.

## Phase 1 — Central catalog (15–20 days)

### Why

`server.py` serves exactly one tree: `FOLDER` comes from `LIGHTTABLE_DIR`,
`library_snapshot()` walks it with `rglob`, and state lives in
`FOLDER/.lighttable-state.json`. Switching sources relaunches the whole server
from `app/main.swift` `launch(folder:)`. Identity is `file_key()` = md5 of
absolute path, size, and mtime, so a file moved in Finder loses its edits and
all of its caches. `/api/images` ships every image with its full edit state in
one payload, filtering and smart rules run in `visible()` on the client, the
grid is not virtualised, and `thumb/` and `orig/` are never pruned. That is
fine at 569 images and unworkable at 100 000.

### Design

- **Location.** One SQLite catalog per user at
  `~/Library/Application Support/LightTable/Catalog/library.sqlite3`
  (`%LOCALAPPDATA%\LightTable\Catalog\` on Windows), overridable with
  `LIGHTTABLE_CATALOG_FILE` for tests and the benchmark. WAL mode,
  `PRAGMA integrity_check` on open, a `meta` table with `schema_version`.
  `run.sh` and `LIGHTTABLE_DIR` keep working as "folder mode": an ephemeral
  catalog under the cache directory seeded with that one source.
- **Schema v1.**
  - `sources(id, path, display_name, favorite, available, added_at,
    last_scan_at, bookmark BLOB)` — replaces the host's `folderSources`
    UserDefaults list; the server owns sources now.
  - `folders(id, source_id, parent_id, relpath, name)`.
  - `files(id, source_id, folder_id, relpath, filename, ext, kind
    raw|processed|video, size, mtime_ns, header_hash, capture_time,
    camera_make, camera_model, lens, width, height, orientation, missing,
    added_at)`, unique on `(source_id, relpath)`. `header_hash` is BLAKE2b of
    size plus the first 64 KiB; it is stable across moves, renames, and
    touches, and cheap enough for a first scan of 100 000 files on an SSD.
  - `images(id, file_id, virtual, copy_ident, display_name, created_at)` —
    one row per photo or virtual copy; this is what carries state.
    `library_workflow.VIRTUAL_MARKER` names map onto `copy_ident`.
  - `image_state(image_id, status, rating, label, params_json, grade_json,
    crop_json, masks_json, heals_json, optics_json, provenance_json,
    updated_at)` — the queryable fields become columns, the edit blobs stay
    JSON so the existing `clean_*` functions keep owning their schemas.
  - `keywords(id, parent_id, name, path UNIQUE)` and `image_keywords`.
  - `iptc(image_id, title, caption, creator, copyright, credit, source, city,
    state, country, gps_lat, gps_lon, gps_alt)` — filled in 3.3.
  - `collections(id, parent_id, name, type, rules_json, sort_order)`,
    `collection_images(collection_id, image_id, position)`, `stacks`,
    `stack_images`, `versions(id, image_id, name, created, state_json)`,
    `history(id, image_id, seq, created, label, state_blob, origin)` — the
    last two filled in 3.5 and Phase 2.
  - FTS5 `image_search(image_id UNINDEXED, filename, keywords, title,
    caption, camera, lens)`. The AI index stays in its own database so
    "Delete Index" keeps its guarantee; the server joins its captions and
    tags into search results at query time.
- **Identity in the API.** Introduce a qualified name with a numeric source
  prefix, parsed by one helper that `src_path()`, `file_key()`, and the cache
  paths call. This keeps most of `server.py` intact while making names unique
  across sources. `file_key()` becomes md5 of `header_hash`, size, and mtime
  so renames and moves keep their render caches. Thumbnails are keyed by
  `header_hash` alone.
- **Query moves server-side.** `POST /api/catalog/query {scope: all|source|
  folder|collection, includeSubfolders, filter:{status, ratingMin, label,
  kind, query, dateRange, camera, lens, keyword}, sort:{field, dir}, limit,
  offset}` returns `{total, items}` where each item is lean: id, qualified
  name, display name, folder, flags, rating, label, capture time, file key,
  dimensions, `hasEdits`. Full edit state is fetched per image on open
  (`GET /api/state?name=`). Smart-collection rules are evaluated in SQL by
  the same code path, and the JS twin `photoMatchesRules()` is deleted.
- **Grid virtualisation.** `renderGrid()` and `renderStrip()` render only the
  visible window plus overscan. Square Grid uses fixed cell heights; Photo
  Grid computes masonry rows from the stored width and height, falling back
  to a default aspect until the thumbnail worker records real dimensions.
- **Scanning.** `catalog_scan.py`: `os.scandir` walk per source in a
  background thread, diffed against `files` by relpath, size, and mtime. New
  or changed files get the header hash and scan-time metadata (capture time,
  camera, lens, dimensions, orientation) from the exiv2 binding on both
  platforms; the 50 ms exiftool subprocess is kept only for the detail panel.
  Missing files are flagged, not deleted, and relink automatically when the
  same header hash reappears anywhere in any source, which is the fix for
  Finder moves. The same hash gives "Find duplicates" for free.
- **Watching.** macOS host: an `FSEventStream` per source that posts a
  `sourceChanged {path}` bridge event; Windows shell: the `notify` crate.
  The server rescans only the changed subtree. Fallback: rescan on focus and
  on the existing `refresh` action.
- **Thumbnails.** A bounded background worker builds thumbs in visible-first
  order; `prune_cache()` is extended to `thumb/` and `orig/` with a
  byte budget and last-access ordering (default 2 GB).
- **Migration.** On first open with a catalog, every known source's
  `.lighttable-state.json` is imported (images, collections, stacks, virtual
  copies) and the file is left in place with its mtime recorded; if it is
  later touched by an older build the UI warns. A preference "Mirror state to
  a per-folder file" keeps writing that file best-effort for people who want
  edits to travel with the folder; mirror writes are skipped silently on
  read-only volumes, which also resolves today's HTTP 500 on read-only
  folders.
- **Backup.** Catalog › Back Up Now uses `sqlite3.Connection.backup()` into a
  dated zip under Application Support; a weekly prompt like the incumbent
  editors, off by default in folder mode.

### Steps

- **1a Data layer and migration (5 days).** `catalog.py` (schema, open,
  migrate, backup), `catalog_scan.py`, the qualified-name helper, the
  `.lighttable-state.json` importer, and a compatibility shim so the current
  `/api/images` reads from the catalog for the active source while the UI is
  unchanged. Tests: schema creation and upgrade, state-file import
  round-trip against `tests/test_state.py` fixtures, virtual copy mapping,
  relink by hash.
- **1b Query API and virtualised grid (5 days).** `/api/catalog/query`, the
  library rail with folder counts from SQL, All Photographs, the lean item
  shape, per-image state fetch, virtualised grid and strip, server-side
  smart rules. Tests: query semantics equal to the old `visible()` on a
  fixture library; a text-assert test that rule evaluation exists only in
  Python.
- **1c Scan, watch, relink, thumbnails (3–5 days).** Background scanner,
  host watchers, thumbnail worker, cache pruning, capture-time sort
  (`capture_time()` exists in `server.py` but is never called; date sort uses
  mtime today).
- **1d Hosts, backup, benchmarks (2–5 days).** The server starts once with
  `LIGHTTABLE_CATALOG_FILE`; `selectSource` becomes a client scope change;
  `addFolder`, `removeSource`, `toggleFavorite`, and `renameRoot` become
  `/api/catalog/sources` calls in both `main.swift` and `windows-shell`. The
  `sources` bridge event is now sourced from the server. Extend
  `bench/benchmark.py` `benchmark_large_library` to 100 000 hard-linked files
  in a realistic folder spread plus 5 000 real files for thumbnails, and
  record: cold first scan, warm startup to first grid paint, query latency
  for All Photographs sorted by capture time, and thumbnail throughput.

### Targets

| Measure | Target |
| --- | --- |
| Warm startup to first grid paint, 100 000 images | under 1.5 s |
| `/api/catalog/query`, page of 500, All Photographs by capture time | under 30 ms |
| Grid scroll | 60 fps, no full rebuilds |
| Cold first scan, 100 000 files on internal SSD | under 60 s |
| Edits after a Finder move or rename | preserved |

## Phase 2 — Importing a third-party catalog (8–10 days)

This section documents conversion compatibility, so the product is named.

Lightroom Classic catalogs (`.lrcat`) are SQLite databases. The importer reads
them read-only from a temporary copy (the file is locked while Lightroom is
open, and the `-wal` journal must be copied with it). Masks and AI data live in
the `.lrcat-data` folder and are skipped, as they are for presets; the
`-v13` filename suffix is only a name.

- **Fixtures first.** Acquire sample catalogs from Lightroom 6, 12 or 13, and
  the current release, and confirm the tables below with `PRAGMA table_info`.
  The importer checks column presence at runtime so version drift degrades to
  a reported skip rather than a crash.
- **Module** `catalog_import.py` (Windows allowlist), with the mapping:
  - Files: `AgLibraryRootFolder.absolutePath` + `AgLibraryFolder.pathFromRoot`
    + `AgLibraryFile.baseName` and `extension`. Each root becomes a catalog
    source; a root that no longer exists can be re-pointed in the dialog,
    and unmatched files are reported.
  - Images: `Adobe_images` gives `rating`, `pick` (1 flagged, −1 rejected),
    `colorLabels` (label text, mapped by name to the five colours and
    reported when custom), `captureTime`, `orientation` (AB/BC/CD/DA), and
    `copyName` + `masterImage` for virtual copies.
  - Keywords: `AgLibraryKeyword` (`name`, `parent`) → the hierarchy;
    `AgLibraryKeywordImage` → membership.
  - Collections: `AgLibraryCollection` (`name`, `parent`, `creationId`) with
    `AgLibraryCollectionImage` positions. Collection sets become parents.
    Smart collections are recreated only for the rule subset LightTable
    supports (rating, flag, label, text); otherwise the name is created empty
    and reported.
  - Stacks: `AgLibraryFolderStack` and `AgLibraryFolderStackImage`.
  - IPTC and GPS: `AgLibraryIPTC` (`caption`, `copyright`),
    `AgHarvestedExifMetadata` (`gpsLatitude`, `gpsLongitude`), and the
    per-image XMP in `Adobe_AdditionalMetadata.xmp` parsed by
    `xmp_sidecar.py` for title, creator, and the rest.
  - Develop settings: `Adobe_imageDevelopSettings.text` is a Lua table in
    the same shape as `.lrtemplate`, so `preset_io._legacy_template_as_xmp()`
    plus `map_crs_settings()` converts it; crop comes from the same table.
    The XMP block is the fallback. Imported edits default to Film off, as
    imported presets do, with the same checkbox to change that.
  - History: `Adobe_libraryImageDevelopHistoryStep` (`name`, `text`,
    `dateCreated`) becomes history rows with `origin = 'import'` once 3.5
    exists; until then it is skipped and counted.
- **Job and UI.** Library › Import from another catalog… opens a stepped
  dialog: choose file (new `chooseCatalogFile` bridge action in both hosts)
  → summary (images, roots found and missing, keywords, collections) →
  options (metadata, keywords, collections, develop edits, history, conflict
  policy for images already in the catalog) → background job with
  `/api/import/status`, following the export and merge job pattern → report
  with mapped and skipped counts by category, in the same voice as the preset
  conversion report.
- **Tests.** A synthetic minimal catalog built in-test with only the columns
  the importer reads, covering every mapping above, plus a real-catalog smoke
  test that runs only when `LIGHTTABLE_SAMPLE_CATALOG` is set.

Capture One catalogs and sessions are a later follow-on; their styles already
import.

## Phase 3 — Daily workflow (≈21 days)

### 3.1 Card ingest (7 days)

- **Hosts.** Detect removable volumes (`NSWorkspace` mount notifications with
  `volumeIsRemovableKey`; `GetDriveType == DRIVE_REMOVABLE` on Windows) and
  post a `volumes` bridge event. Any folder can also be chosen as an ingest
  source. Add `ejectVolume` and `chooseIngestSource` actions.
- **Engine** `ingest_workflow.py` (Windows allowlist): scan `DCIM/**` for
  photos and video; read capture time and camera from the card with header
  reads; detect suspected duplicates against the catalog by header hash;
  build a plan from a destination source, a folder template, and a rename
  template using `export_workflow.render_filename()` extended with date
  parts, camera, sequence, and custom text tokens; optional second copy to a
  backup destination; optional native preset and metadata preset applied on
  import; copy with verification (copy, fsync, compare size and header hash,
  or full hash in "verify" mode); add to the catalog; queue thumbnails;
  optionally eject.
- **Job.** `POST /api/ingest`, `GET /api/ingest/status`, cancellable and
  resumable (verified copies are skipped on rerun), same background pattern
  as export and merge.
- **UI.** A larger modal after the `#nameDialog` pattern: source picker,
  thumbnail grid with checkboxes (a guarded `/api/ingest/thumb?path=`
  limited to the chosen root), destination and templates with a live example
  name, options, progress with per-file errors.
- **Tests.** Plan generation from a temporary card tree, template rendering,
  duplicate detection, a corrupted-copy case that reports and leaves the
  source untouched, and resume.

### 3.2 Batch rename (2 days)

- `POST /api/photos/rename {names, template, start}` renames originals and
  both sidecar naming forms, updates `files.relpath`, and records the
  reverse mapping in a `rename_log` table so "Undo rename" is one action.
  Thumbnails survive because they are keyed by header hash; render caches
  survive because `file_key()` no longer includes the path (Phase 1).
- UI: "Rename…" in the selection menu with a preview of the first three
  names and a conflict check before commit.

### 3.3 Metadata: IPTC, GPS, keyword hierarchy, metadata presets (4 days)

- Scan-time read of title, caption, creator, copyright, keywords, and GPS
  from embedded XMP, IPTC, and EXIF so images tagged elsewhere arrive tagged.
- The Info pane becomes a Metadata pane with editable IPTC fields, GPS
  latitude and longitude with an "Open in Maps" link (no map view in this
  milestone), and a keyword tree with create, rename, drag to reparent, and
  `parent > child` entry. Metadata presets save a set of IPTC values for
  bulk apply and for ingest.
- `POST /api/metadata/bulk {names, fields}` mirrors `/api/state/bulk`.
  Title, caption, and creator join the FTS index. Metadata lives in the
  catalog; export embeds it (0.3); writing back into originals stays off
  until Phase 5.

### 3.4 Culling: survey view, A/B compare, trash rejected, key schemes (5 days)

- **Survey** (`n`): a new `#survey` container inside `#library` showing the
  multi-selection or the current stack as fit-size cells rendered through the
  existing preview endpoints at cell width and low priority. Each cell has
  rating, flag, and label controls and a remove button; the active cell
  receives the digit and flag keys; double-click opens Detail. It owns its own
  pointer scope and never touches `#cv` or the shared `GradeRenderer`.
- **A/B compare**: two cells from the survey model with a swap key and a
  "make select" action, distinct from the existing before/after compare.
- **Trash rejected**: `⌘⌫` gathers rejected images in scope, confirms with a
  count, and asks the host to move originals and sidecars to the Trash or
  Recycle Bin (`trashFiles {paths}` in both hosts); the catalog marks them
  removed.
- **Key schemes**: move the handler's key bindings into a keymap table with
  two schemes, the current one and a "Classic" scheme that matches the
  incumbent editors (`r` crop, `c` compare, `n` survey, `e` loupe, `g` grid),
  selectable in preferences. The shortcut list in `web/index.html` renders
  from the table.

### 3.5 Persistent history (3 days)

- The client already captures `beforeState` per interaction near
  `web/app.js` line 4045. Send a compact step with each committed interaction
  (`POST /api/history {name, label, state}`), coalescing repeated labels
  within two seconds. The server stores zlib-compressed snapshots in
  `history`, capped at 200 steps per image with a "Clear history" action.
- A `historyPane` with its toolrail button lists steps newest first; clicking
  one restores through `restore(json)`, and the next edit truncates later
  steps. "Create version from step" reuses the Versions code. Imported steps
  (Phase 2) show an origin badge.
- Undo and redo stay in memory as they are; history is the persistent layer.

## Phase 4 — Image quality (15–17 days)

### 4.1 A standard develop profile when Film is off (2 days)

Film-off preview and export both start from the neutral TIFF built by
`color_pipeline.linear_prophoto_to_display_srgb()`: a plain sRGB encode plus
a 99.5th-percentile white normalisation. That is honest and flat, and it is
what a newcomer sees first.

- Add `developProfile` (`linear|standard`) to `RAW_DEVELOP_KEYS` and
  `DEFAULT_PARAMS` in `film_pipeline.py` so it flows through the decode
  fingerprint, every cache key, and the per-camera defaults in `prefs.json`.
- `standard` applies a fixed base curve in linear light before encoding: a
  gentle toe, mid-tone contrast, and a highlight shoulder, expressed as a
  256-entry table so it can be inspected and tested for monotonicity. Make it
  the default for new users and for imported edits; keep `linear` for people
  matching scans.
- Camera-profile files (`.dcp`) are Phase 5.

### 4.2 Auto-straighten and guided upright (5 days)

- `geometry_auto.py` (Windows allowlist) works on the neutral preview at
  about 1100 px: `skimage.feature.canny` → `probabilistic_hough_line` →
  length-weighted clusters of near-horizontal and near-vertical lines. No new
  dependency; OpenCV is not in the runtime.
- Modes: Level sets `optics.rotate` from the dominant horizontal cluster;
  Vertical solves the existing keystone model in `edits.apply_manual_optics()`
  (`px = rx·(1 + vertical·0.45·ry)`) by a one-dimensional search that makes
  the vertical cluster parallel; Full does both plus horizontal. Each mode
  derives `optics.scale` so no empty corners remain. Rotation beyond the
  ±15° clamp is capped and reported.
- Guided: the user draws two to four lines on `#editOverlay` in a new lens
  pane tool mode with the same pointer ownership rules as masks and healing;
  lines are tagged by angle and solved by least squares against the same
  parametric model. The panel states that the model is parametric, not a full
  homography.
- `POST /api/geometry/auto {name, mode, guides}` returns an optics patch and a
  confidence; the client applies it through the existing preview edit cache.
- Tests: a synthetic tilted grid recovers its angle within 0.2°, and a
  synthetic keystone recovers `vertical` within tolerance.

### 4.3 Learned denoise and Enhance (8–10 days)

- **Runtime.** No CoreML, ONNX, or Torch exists in the runtime lock. On macOS
  the cheapest path is a new argv mode in the compiled Swift helper
  (`film_lab_ai/vision_helper.swift` already has `--foreground-mask`), or a
  sibling `LightTableEnhance` binary built and signed the same way in
  `build-app.sh` and `scripts/build-release.sh`: `--denoise in.tif out.tif
  --strength 0.6` and `--upscale 2 in.tif out.tif`, 16-bit TIFF in and out,
  tiled at 512 px with overlap to bound memory. Windows follows later with
  ONNX Runtime and DirectML in `packaging/runtime-windows.lock`; until then
  `capabilities()` reports the feature as unavailable there.
- **Models — evaluated 2026-09-03.** Recommendation: **SCUNet**
  (`scunet_color_real_psnr`) for denoise. Its code is Apache-2.0 and the
  weights were released first-party under MIT, both one-way compatible into
  this project's GPL-3.0. Two technical reasons decide it over NAFNet, which
  is also permissively licensed (MIT plus Apache-2.0, and SIDD itself appears
  to be MIT, so licensing does not rule NAFNet out):

  1. SCUNet is trained on a purely synthetic degradation pipeline and its
     authors state explicitly that SIDD and DND pairs were not used. That
     matters here because the app accepts files from 1275 camera models, so a
     denoiser has to generalise to sensor noise it has never seen rather than
     to the smartphone noise SIDD samples.
  2. It is blind — no noise-level input — which is what a single Denoise
     control wants. Take the PSNR variant, not the GAN one: fidelity, not
     invented texture.

  **Super-resolution deserves more scepticism than a license check.**
  Real-ESRGAN x4plus is BSD-3-Clause and unencumbered, but it is a GAN trained
  to make degraded web images look plausible; on a clean raw file it invents
  micro-detail. That sits badly in an application whose profile catalogue
  labels every assumption "modeled" or "measured". If it ships, it should be
  named as generative in the interface, not presented as recovered detail — or
  SwinIR classical (Apache-2.0) used instead for a fidelity-first 2×. Denoise
  is the feature worth having first.

  Convert with coremltools at development time to an fp16 `.mlpackage`; the
  512 px tiling already fixes input shapes, which is what Swin-style blocks
  need for a clean conversion. Download at package time with a pinned hash via
  a `fetch-models.py` following `fetch-color-profiles.py`, and keep each
  model's license text beside it. Weights are not committed to git.
- **Denoise seam.** `color_pipeline.apply_learned_denoise(rgb, params)` after
  `raw.postprocess()` in `decode_raw()`, so it is RGB-domain and
  post-demosaic (the current Light and Full options are LibRaw FBDD,
  pre-demosaic). Add `learnedDenoise` and its strength to
  `raw_decode_fingerprint()` and bump its `v2` token; add it to
  `RAW_DEVELOP_KEYS` so a camera default such as "on above ISO 3200" is
  possible. Preview applies it only in the accurate refinement stage with a
  "Denoising…" status; the draft stays fast.
- **Enhance.** Super-resolution is an output workflow, not a preview
  parameter: like Photo Merge, it writes a 16-bit ProPhoto TIFF master plus
  a manifest sidecar into the merges folder and adds it to the catalog. That
  avoids width-parameterised cache changes entirely.
- **Tests.** A text assertion that `build-app.sh` builds the helper (as
  `NativePreviewContractTests` does for the shader), a tiling test with an
  identity model that must reassemble exactly, and a fingerprint inclusion
  test.

## Phase 5 — Long tail

- **Video cataloguing.** Extension list, thumbnails and duration through an
  AVFoundation mode of the Swift helper, a `kind=video` filter, and playback
  through a range-capable `/api/video` route. No editing.
- **Sidecar writing.** An opt-in preference that writes rating, label,
  keywords, IPTC, and an approximate `crs:` block plus a native namespace
  block next to originals, using `platform_image.write_metadata()`. Makes
  edits portable and gives read-only volumes a clear story.
- **Supported-camera page.** `scripts/fetch-camera-list.py` pulls the
  camera list from the LibRaw source at the tag matching
  `rawpy.libraw_version` (rawpy exposes no camera list at runtime) and writes
  `docs/cameras.html`; the site documents the DNG conversion fallback and a
  policy of updating LibRaw within a release cycle of each upstream release.
- **Camera-profile files.** Parse `.dcp` (TIFF-based: colour matrices,
  forward matrices, hue-saturation maps, look table, tone curve) with
  `tifffile`, apply the hue-saturation map, look table, and tone curve on the
  LibRaw ProPhoto output as an explicitly approximate camera look, selected
  per camera through `developProfile`. Users point at their own profile
  folder; nothing is bundled.
- **Windows parity** for the Swift-only helpers (Vision, CoreML).
- **Duplicate detection UI** over the header hash from Phase 1.
- **Capture One catalog import**, and multiple catalogs with an "Open
  catalog…" picker.

## Acceptance for the milestone

A switcher walkthrough, scripted and timed on the benchmark Mac:

1. Import a third-party catalog of 20 000 images with keywords, collections,
   and edits; every file on disk is matched, and the report lists what was
   skipped.
2. Browse All Photographs sorted by capture time across three sources at 60
   fps; search by keyword and camera in under 100 ms.
3. Ingest a card of 500 RAW files into a dated folder with a backup copy and a
   metadata preset; the catalog shows them with thumbnails before the copy
   finishes.
4. Cull with keys only: flag, reject, rate, label, survey a burst, trash the
   rejects.
5. Export a client set with copyright embedded, a watermark, and no sidecars;
   read the copyright back from the JPEG.
6. Level a tilted frame automatically, denoise a high-ISO frame, and confirm
   the neutral Develop default looks finished rather than flat.
7. Move a folder in Finder, relaunch, and find every edit intact.

## What was built, and what was measured

Every number here is from a run on the development Mac, not an estimate.

### Catalog scale

A profile of the first scan showed `relink_by_hash` taking 98% of the time and
issuing 4.5 million `stat` calls: every new file was checking every existing
row that shared its content hash. Asking for already-missing rows first, and
bounding the on-disk check to `RELINK_CANDIDATE_LIMIT` candidates, took the
scan from **38 files/sec to 9,373**. The first version of the benchmark was
also fabricating its library with hard links to one seed file, so every file
shared a hash — a fixture that measured duplicate handling rather than
scanning. It now writes distinct bytes per file.

| Measure | Target | Measured |
| --- | --- | --- |
| Cold scan | 100k under 60 s | 10k in 1.1 s, so ~11 s extrapolated |
| Rescan, nothing changed | — | 0.08 s at 10k |
| Query, all by capture, page of 500 | under 30 ms | 6.6 ms at 10k |
| Query, deep page at offset 9,400 | — | 17 ms |
| Initial payload | lean first page of 600 rows | state fetched on open; later rows page in |
| Edits after a Finder move or rename | preserved | preserved |

Two design faults were found by running the thing rather than reading it. The
first startup blocked the HTTP port on a full synchronous scan, so a large
library never became reachable at all; scanning now runs behind the server.
`/api/images` then made one SQL round trip per photo for its state, which is
why it hung at scale. The initial payload is now a lean page without edit
blobs; the selected image fetches its state from `/api/state`, and the grid
pages the remaining catalog rows in the background.

### Auto level and upright

Recovered angle against synthetic truth: 3.50° → 3.506, −2.75° → −2.686,
8.00° → 7.970, 0.90° → 0.730, −11.00° → −11.004. Worst error **0.17°** against
a 0.2° budget. Keystone: 0.350 → 0.3469, −0.400 → −0.3968, 0.600 → 0.6012,
worst error **0.0032**. End to end through the server on a 3.5° tilted grid:
`rotate = -3.495`, scale 1.1, confidence 1.0, 38 lines. A rotation beyond the
±15° the geometry model supports is clamped and reported, not silently
half-applied.

### Develop profile

Standard holds 0.18 linear as a fixed point of the curve — measured
`curve(0.18) = 0.18000158` — so switching profiles changes contrast, not
exposure, and an 18% grey card still lands on Zone V. The curve is monotonic
across all 255 steps and maps 0→0 and 1→1 exactly. Linear is asserted
byte-for-byte identical to the previous render. The decode fingerprint token
moved to `linear-prophoto-v3`, which invalidates the neutral cache once for
everyone; that belongs in release notes.

### Export

A TIFF's ICC profile lives in `Exif.Image.InterColorProfile`, inside the EXIF
block, so copying source EXIF wholesale destroys it. The metadata writer
carries a structural denylist for that tag, strip and tile offsets, thumbnails,
and IFD pointers, and a test asserts the embedded profile is byte-identical
after the rewrite for both JPEG and TIFF.

### Enhance — what is real and what is not

Real and tested: model discovery, capability reporting, request clamping,
overlapped tiling with raised-cosine feathering (identity reconstruction to
2.4e-7), the 16-bit ProPhoto master, the manifest, and the cache fingerprint.
The Swift helper compiles and its `--probe` and no-model paths were exercised.

Not present: **no denoise or super-resolution model ships with LightTable**,
none is downloaded, and no Core ML, ONNX, or Torch runtime was added to the
lock file. Choosing a model is a licensing and quality judgement that needs
real evaluation on real files, so it was left to be made deliberately rather
than settled by whatever was convenient. There is deliberately no identity
fallback: with no model installed, Enhance reports itself unavailable and
refuses, because returning the input unchanged would be indistinguishable from
a model that did nothing.

### Camera profiles

Exact: trilinear interpolation over the hue/saturation/value grid with correct
hue wrap, and the DNG hue-preserving tone curve. Approximate, and named per
file in the profile's own `unsupported` list: no dual-illuminant interpolation,
no forward-matrix adaptation, and the look table is applied before the tone
curve rather than after.

### Coverage

443 tests, all passing, up from 123 at the start. The Windows shell compiles,
the Swift host and both helpers parse and build, and the Windows packaging
allowlist — which was silently omitting six shipped modules — is now checked by
a contract test against `server.py`'s own imports.

### Not done

- **Windows parity for the Swift-only helpers.** Vision indexing, video poster
  frames, and Enhance are macOS-only; the Windows shell reports them
  unavailable rather than pretending.
- **The Enhance model**, for the reasons above.
- **Capture One catalog import** and multiple catalogs with an open-catalog
  picker remain future work, as planned.
- Real `.lrcat` files were not available here. The importer is tested against
  synthetic catalogs built to the documented schema, checks every table and
  column before reading it, and degrades to a named skip on anything it does
  not recognise — but its first run against a genuine catalog is still its
  first run against a genuine catalog.
