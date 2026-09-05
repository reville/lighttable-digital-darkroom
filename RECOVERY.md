# Library Health and Recovery

LightTable keeps one SQLite catalog for every source folder, a few JSON
documents (preferences, presets, the optional per-folder state mirror), and
several generated caches. This document lists the ways that data and the app
around it can fail, what the app does about each one on its own, and where a
person has to decide.

The rule throughout: **the app never discards user work without asking.**
Anything that replaces the catalog file first copies what was there into
`Catalog/Recovery/<timestamp>/`, and the person chooses between salvaging,
restoring a backup, or starting fresh with the contents of each option in
front of them.

## Failure modes and responses

| What can go wrong | What the app does | Where a person decides |
| --- | --- | --- |
| The server never answers the launcher | The shell polls the cheap `/api/health` endpoint (not the first library page) with a generous timeout, reads the server's phase file to show "Opening the catalog…" and to adopt a moved port, and stops the moment the server reports a failure. | The alert offers Try Again, Safe Mode, Show Server Log, Quit. |
| Another copy already has the catalog open | The process lease refuses a second writer; the server reports which process and port hold it. | The alert offers to quit the other copy and retry. |
| The launcher's chosen port is taken before the server binds | The server binds any free port and reports it; the shell adopts it. | — |
| The launch folder is unmounted or renamed | With a catalog the folder is one source among many, so the app opens anyway and shows a banner. | — |
| The server crashes mid-session (a decoder segfault, an out-of-memory kill) | The shell relaunches it with a short delay, up to three times, passing the exit status; the web window shows "Reconnecting…" and reloads when a new process answers. | After three crashes in a row the alert offers Safe Mode. |
| One photo crashes the server every time it is opened | Before decoding or rendering, the server records the photo in `inflight.json`. A session that ends without a clean ending is a crash; the photo it was processing takes a strike, and two strikes set it aside. Its previews are refused with a clear message instead of taking the process down again. | Library Health lists set-aside photos with a Release button. |
| The catalog file is structurally damaged (power loss, disk fault, a sync service) | Startup runs `quick_check` (fast) and, on failure, opens in folder mode with the damaged file untouched. Background maintenance runs the exhaustive `integrity_check` daily. | Library Health shows Salvage (rows readable now), Restore (each backup with its date and contents), or Start fresh. |
| Rows lost their parents; the search index drifted | `verify` counts orphans per table and compares the full-text index with the images it describes. `repair` takes a before-repair snapshot, removes orphans, rebuilds the index, checkpoints. | Repair is a two-click button. |
| A newer build's catalog | Refused as incompatible, never downgraded or "recovered". | Update the app. |
| Backups are all from after the damage | Retention is tiered: the newest eight, then one a day for a week, one a week for five weeks, one a month for six months. | — |
| The write-ahead log grows for days | Maintenance checkpoints every fifteen minutes when the renderer is idle, and on shutdown. | — |
| The disk is nearly full | Backups pause; the banner and Library Health say so. | Free space. |
| The catalog sits inside a syncing folder | Library Health names the service and warns. | Move it. |
| `prefs.json` or `presets.json` is unreadable | The last valid `.backup` is used; Library Health says which copy is live. A rewrite sets the unreadable bytes aside instead of erasing them. | — |
| A generated cache bundle is truncated or malformed | Discarded and regenerated (existing behaviour). Library Health can clear every preview cache. | — |
| The local AI index is damaged | Quarantined and rebuilt automatically; it holds only generated data (existing behaviour). | — |
| The server log is truncated on each launch | The shell rotates `server.log` → `server.1.log` → `server.2.log` and writes a launch header. | Help ▸ Diagnostics ▸ Show Server Log. |

## Pieces

- `recovery.py` — `StartupReporter` (phase file for the launcher),
  `SessionLedger` (session/inflight markers, crash ledger),
  `PhotoQuarantine`, damaged-file custody, disk and sync-folder checks, log
  rotation helpers.
- `catalog.py` — `quick_check_ok`, `verify`, `repair`, `checkpoint`,
  `retire`, `rebuild_search_index`; module functions `salvage`,
  `rebuild_catalog`, `restore_backup`, `create_fresh_catalog`,
  `list_backups`, `backup_summary`, tiered `prune_backups`.
- `server.py` — `open_catalog` reports damage instead of restoring silently
  (`LIGHTTABLE_AUTO_RECOVER=1` restores the old headless behaviour);
  `guard_photo` and in-flight markers around decode, render, and export;
  `maintenance_loop`; `GET /api/recovery`, `GET /api/recovery/log`,
  `POST /api/recovery` with actions `verify`, `repair`, `checkpoint`,
  `backup`, `maintenance`, `rescan`, `rebuild-search`, `release`,
  `set-aside`, `clear-caches`, `restore`, `rebuild`, `reset`, `restart`;
  `LIGHTTABLE_SAFE_MODE=1` disables every background service; the server
  exits with status 75 when it wants a clean relaunch.
- `web/recovery.js` — the Library Health dialog, the top banner, and the
  reconnect overlay. Reachable from the Catalog pane, Settings ▸ Catalog,
  and Help ▸ Diagnostics ▸ Library Health….
- `app/main.swift` — log rotation, startup phase reading, health-based
  readiness, the failure alert, crash restart with backoff, Safe Mode, and
  the Diagnostics menu items.

## Files beside the catalog

```
Catalog/
  library.sqlite3            the catalog
  library.sqlite3.process-lock
  session.json               current session; an ending is written on quit
  inflight.json              the photo being processed (absent when idle)
  crashes.jsonl              the last fifty unexpected endings
  photo-quarantine.json      strikes and set-aside photos
  Backups/                   verified zips, tiered retention
  Recovery/<stamp>-<why>/    whatever was replaced, with REASON.txt
```

## Exercising it

`tests/test_recovery.py` covers the ledger, quarantine, custody, verify and
repair, salvage of a page-damaged file, restore of a chosen backup, tiered
pruning, the process lease, and the server's recovery policy. To see the
dialog with a damaged catalog, point `LIGHTTABLE_CATALOG_FILE` at a copy of a
catalog, overwrite a few late pages, and start the server: the banner offers
the choices and `lighttable doctor` reports the same facts.
