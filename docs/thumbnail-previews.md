# Library thumbnail previews

The grid uses a 1,024-pixel source preview immediately, then replaces it with a
1,024-pixel rendition of the current edits when ready. The Detail filmstrip
uses 240-pixel source thumbnails and 320-pixel edited thumbnails. Small originals
are never enlarged during thumbnail generation. Camera-embedded RAW previews
remain the fast source path; RAWs without an embedded preview retain the existing
decode fallback. Source appearance can differ from the final edited appearance.

Adding or rescanning a folder prepares the large source tier on one background
worker. Scanning only queues work after catalog rows commit; it never waits for
image decoding. A full 256-entry queue records a per-source catalog cursor and
refills in bounded pages, including subfolders, rather than losing the rest of
the import. The worker yields to interactive rendering and retries when foreground
thumbnail requests occupy the decoder slots. Missing files, cloud-only files,
empty files, and videos are excluded from overflow backfill. Individual decode
failures do not stop following photos.

The existing 30-minute worker lifetime still bounds each preparation pass. App
exit or expiration discards remaining speculative work; on-demand requests and
a later scan can prepare it again. This is not a promise that an arbitrarily
large library finishes preparing in one pass. Full edited renditions remain
on-demand work for visible photos, avoiding full RAW development across an
entire newly added library.

Source cache paths separate the strip and grid tiers. Edited cache keys include
source identity, renderer version, saved/default visual state, and output size.
The shared thumbnail disk budget still applies. The browser retains at most 64
edited object URLs and discards queued work for photos that have scrolled away.
Reused DOM nodes refresh rendition identity when entering a different view.

## Validation

- Regression tests cover source tier dimensions and cache reuse, EXIF rotation,
  overflow backfill and busy-worker retries, initial grid requests, stale queue
  removal, and rendition identity across grid/Detail transitions.
- The actual shared UI was checked in an isolated hidden browser, with real
  JPEG fixtures and the packaged renderer: both grid layouts delivered
  1,024-pixel images and Detail delivered 320-pixel edited filmstrip images.
- Three JPEG samples took about 14–17 ms to generate a 1,024-pixel source preview
  on the development Mac, versus 4–9 ms at 240 pixels. These small-sample,
  warm-file timings are not a general hardware or cold-library benchmark.
- Sampled HIF files produced black source previews at both sizes; the HIF opened
  in Detail also reported an ImageIO decoding failure. HIF visual correctness
  is not established by this change. No installed app was replaced.

## Design references

[Lightroom Classic import previews](https://helpx.adobe.com/lightroom-classic/desktop/import-photos/photo-video-import-options.html)
distinguish minimal/embedded previews from rendered standard previews.
[Capture One preview settings](https://support.captureone.com/hc/en-us/articles/360002484457-Capture-One-Preferences-Settings-Image-tab)
relate preview size to display needs. LightTable follows the same broad approach:
useful source previews immediately, cached preparation in the background, and
edited refinement as needed.
