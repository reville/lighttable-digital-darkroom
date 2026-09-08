# DAM catalog performance

The catalog benchmark creates disposable 50,000- and 100,000-image databases,
then calls the production `Catalog` query and save methods. It creates **zero
photo files** and never opens an existing catalog. Fixture insertion uses
1,000-row batches. A separate subprocess per size has a 240-second limit.

```sh
python3 bench/dam_benchmark.py --output bench/results/dam-catalog.json
node bench/dam_grid_benchmark.mjs --output bench/results/dam-grid.json
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
