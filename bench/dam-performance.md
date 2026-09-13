# DAM catalog performance

The catalog benchmark creates disposable 50,000- and 100,000-image databases,
then calls the production `Catalog` query and save methods. It creates **zero
photo files** and never opens an existing catalog. Fixture insertion uses
1,000-row batches. A separate subprocess per size has a 240-second limit.

```sh
python3 bench/dam_benchmark.py --output bench/results/dam-catalog.json
node bench/dam_grid_benchmark.mjs --output bench/results/dam-grid.json
node bench/dam_view_benchmark.mjs --output bench/results/dam-view.json
python3 -m unittest discover -s tests -p 'test_dam_benchmark.py'
python3 -m unittest discover -s tests -p 'test_view_performance.py'
```

Use the application's Python executable in place of `python3` when comparing
its runtime. The JSON records Python/SQLite versions and the catalog source
hash; use the same machine and runtime for before/after comparisons. Timing
thresholds are deliberately absent from tests because host load and hardware
vary. The fixture's counts, page membership, saved ratings, search updates,
and absence of directory scanning are asserted.

The fixture spreads captures over folders, two sources, seven years, cameras,
lenses, numeric EXIF values, ratings, labels, keywords, captions, edit state,
and capture-time overrides. One source directory is absent. Queries must still
return its catalog entries while `os.scandir` is blocked. Smart collections
exercise the four-star / 50mm / 2026 example. FTS tests are lexical caption and
keyword searches, not semantic image search.

## Recorded run: September 8, 2026

macOS 26.6.2 on Apple Silicon; SQLite 3.53.4. The table below uses the app
repository's Python 3.13.12. Query values are medians of seven calls and include
counting matches, producing a 500-record page, and loading its state/keywords.
JSON serialization is recorded separately in the raw output.

| Catalog operation | 50,000 images | 100,000 images |
| --- | ---: | ---: |
| Reopen existing catalog | 0.88 ms | 0.71 ms |
| First page by capture time | 44.9 ms | 84.5 ms |
| Last page by capture time | 68.4 ms | 145.9 ms |
| Caption search: dog snow | 23.9 ms | 34.3 ms |
| Page from unavailable source | 29.4 ms | 46.8 ms |
| Combined numeric EXIF filter | 25.6 ms | 40.1 ms |
| Four-star / 50mm / 2026 smart collection | 26.3 ms | 39.4 ms |
| Single rating transaction | 0.023 ms | 0.024 ms |
| 100-rating transaction, one sample | 0.348 ms | 0.362 ms |
| Keyword save including FTS update | 0.088 ms | 0.071 ms |

The scratch databases occupied 35.34 / 71.43 MiB. Worker peak RSS was 47.22 /
46.86 MiB; Python allocations for one 500-record query peaked at 1.575 / 1.615
MiB. RSS includes fixture creation and SQLite allocations; the Python page
allocation measurement runs separately from timing. It does not measure browser
memory or resident photo-rendering caches. Standard-library RSS is unavailable
on Windows and is reported as `null` there.

For a same-runtime comparison, the original and optimized catalogs were also
measured on Python 3.14.7. At 100,000 records, last-page queries changed from
681.4 to 164.5 ms, unavailable-source queries from 240.1 to 57.3 ms, and the
100-rating transaction from 1,020.1 to 0.395 ms. The optimized fixture also
populates the new numeric EXIF fields. First-page capture queries did **not**
improve in that run: 66.1 to 87.0 ms. These changes address specific costs;
they do not establish that every catalog operation became faster.

The measured changes have three causes:

- Rating and edit-only saves no longer rebuild unrelated full-text entries.
  Keyword indexing addresses FTS rows by their indexed rowid.
- Pagination sorts narrow image IDs and joins wide metadata/edit fields only
  for the requested page. An independent Python oracle checks 252 combinations
  of sort direction, scope, and page position, including a virtual copy and
  missing rating state.
- The `(source_id, missing)` index avoids traversing source rows in filename
  order and supplies a covering index for the source count.

## Grid geometry

The Node 24.18.0 harness imports the production viewport geometry code. At
100,000 records and a 1,400 by 900 viewport, initial layout took 15.5 ms in
square mode and 7.4 ms in photo mode. Across 1,000 deterministic jumps, the
viewport selected at most 63 / 83 positions; p95 lookup time was 0.011 / 0.009
ms. The same position bounds held at 50,000 records. Periodic independent scans
verify that every position intersecting the viewport is included.

This proves bounded viewport **geometry work**, not smooth browser scrolling.
DOM creation, thumbnail decoding/upload, UI event handling, and browser memory
are outside this harness. Its peak Node RSS at 100,000 records was 127.11 MiB,
including both layout modes and coverage checks.

## Server-side filtering and sorting (grid-3000, 50k, 100k)

The grid used to page the whole catalog into the browser and filter, sort, and
locate the current photo in JavaScript. `server.browser_catalog_query` (backed
by `Catalog.query`) now does this in SQL, so `bench/dam_benchmark.py`'s query
cases above are also this feature's server-side contract: `rating_filter`,
`camera_lens_filter`, `numeric_exif_filter`, `capture_year_filter`,
`fts_caption_search`, `hierarchical_keyword`, `smart_collection`, and
`regular_collection` all return a filtered, sorted, counted page rather than
the caller filtering loaded rows.

| Query | 50,000 images | 100,000 images |
| --- | ---: | ---: |
| Rating filter | 29.4 ms | 49.9 ms |
| Camera + lens filter | 71.7 ms | 139.1 ms |
| Numeric EXIF filter (ISO/focal/aperture/shutter) | 28.6 ms | 45.6 ms |
| Capture-year filter | 45.4 ms | 96.4 ms |
| Caption/keyword search (FTS) | 30.9 ms | 44.7 ms |
| Hierarchical keyword | 60.4 ms | 101.2 ms |
| Four-star / 50mm / 2026 smart collection | 40.4 ms | 69.4 ms |
| Sorted first page (name) | 38.2 ms | 67.3 ms |

Medians of seven calls, same run and machine as the table above; see
`bench/results/dam-catalog.json` for p95/max and the query plans.

`bench/dam_view_benchmark.mjs` exercises the production browser module
(`web/library-view.js`) directly: it opens a sorted spec, sweeps a synthetic
scroll across the entire catalog in 60-photo strides calling `ensureRange`
then `evict` after each stride (matching `web/app.js`'s render loop), then
changes the filter spec mid-session. A **grid-3000** run (3,000 images, the
size used for interactive UI review) is included alongside 50,000 and 100,000
to show the resident page cache does not grow with catalog size:

| | 3,000 images | 50,000 images | 100,000 images |
| --- | ---: | ---: | ---: |
| First page | 0.38 ms | 0.18 ms | 0.40 ms |
| Full-catalog scroll sweep (median stride) | 0.0014 ms | 0.0006 ms | 0.0005 ms |
| Max resident pages during sweep | 15 | 40 | 40 |
| Resident page cache bounded (\<= 40 pages / 8,000 rows) | yes | yes | yes |
| Filter spec change | 0.18 ms | 0.63 ms | 0.82 ms |
| Server queries issued during sweep | 16 | 251 | 501 |
| Process RSS growth over the run | 2.8 MiB | 20.5 MiB | 26.0 MiB |

The resident cache tops out at the same 40-page (8,000-row) bound at every
catalog size once the consumer calls `evict()` after each render, which is
what makes 50k/100k viable at all: the previous whole-catalog-into-`S.images`
approach loaded every row's object into the browser up front, so this
comparison has no "before" number to cite — the old design does not have an
analogous bounded figure at these sizes. Raw output is in
`bench/results/dam-view.json`.

This harness stands in for the server with a zero-delay synthetic query, so it
isolates the view's own bookkeeping (page cache, position index, eviction)
from HTTP and SQLite latency, which the table above already covers. It does
**not** measure real network round-trips, DOM creation, thumbnail
decode/paint, or browser scroll-input handling. A true browser (Playwright)
measurement of grid-3000/50k/100k end-to-end scrolling, in the style of
`bench/browser_benchmark.mjs`, is **NOT DONE**: this checkout has no
`playwright` package installed and `LIGHTTABLE_PLAYWRIGHT_MODULE` is unset, so
no browser could be launched here.

## Evidence and limits

Raw local outputs are in `bench/results/` (gitignored by default):

- `dam-20260908-baseline.json`: original catalog, Python 3.14.7, five samples.
- `dam-20260908-optimized.json`: optimized catalog, Python 3.14.7, seven samples.
- `dam-20260908-app-python.json`: optimized catalog, app Python 3.13.12, seven samples.
- `dam-20260908-grid.json`: Node geometry timings and source hash.

The measured catalog source hash in both optimized runs is
`89aa98dfe13e1d6806968eb9d0a8555291bd5d0dcb1ab785d3b4abee36987bea`.
Synthetic setup time is fixture generation, not scanning/indexing performance.
Catalog reopen excludes Python imports, server startup, and the native window.
Rating timings exclude HTTP, history orchestration, UI feedback, and XMP writes.
Missing-directory behavior is a simulated offline source, not a physical drive
disconnect test. No real RAW, thumbnail, next-photo, browser-paint, or native
render latency is established by these results. Use the existing
`bench/benchmark.py` real-photo/server harness and native runtime verification
for those measurements.
