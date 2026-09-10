# Digital asset management

LightTable catalogs originals in their existing folders. The SQLite catalog
holds searchable metadata, organization, and edit state; generated image caches
live separately. Collections and virtual copies do not duplicate originals.

## Filter and save a working set

Open **Filter** beside Sort, then expand **Camera, exposure & date**. Combine
camera and lens names, ISO, focal length, aperture, shutter time, capture dates,
and a keyword path with file type, rating, flag, color label, and edit status.

- Camera and lens match part of a name.
- Numeric limits are inclusive. Put the same value in both fields for an exact
  match. Shutter time accepts seconds or a fraction such as `1/250`.
- Capture-date ranges include both end days.
- A keyword filter for `Places` also matches `Places > Boston`.
- A photo without the required exposure metadata does not match that filter.

For four-star photos taken at 50mm in 2026, choose **4 stars and up**, enter
`50` in both focal-length fields, and set dates from January 1 through December
31, 2026. Use **Save filters as smart collection** beside Collections. Membership
updates when relevant metadata changes. Additional live filters narrow the
saved rules; they do not replace them. The current folder is not a saved rule.

Existing catalogs gain exposure values on their next scan. Use **Folders ••• →
Rescan folders** if those values are missing. Filter choices persist across
reloads and have removable chips above the grid.

The same exposure rules are available through the CLI:

```sh
lighttable photos list --where 'rating>=4' --where focal-length=50 \
  --where from=2026-01-01 --where to=2026-12-31 --jsonl
```

See [CLI.md](CLI.md) for server selection and command options.

## Cull and tag in the grid

The existing flag, rating, and label shortcuts work on grid selections. Survey
compares different frames; Detail's Compare checks an edit against its original.
Grid marking actions use the selected photos still shown by the current folder,
collection, and filters. Check the marking toolbar count after changing the view.

Paste and **Selected photos** export also use the selection still in view, falling
back to the active photo when no selected photos remain visible. Export's
**Picked only**, **Rated 1+**, and **All except rejected** instead search the
active catalog and can include photos outside the current view.

Open **Info** beside Photo Grid or Culling Grid. Enter keywords, check the
selected-photo count, and choose **Add to selection** or **Remove from
selection**. Each photo keeps its other tags. Removing a keyword removes that
exact path, rather than all of its descendants. **Add to photo** and Enter affect
the active photo.

**Undo keyword change** restores the last batch held by the window, provided
none of those keywords has changed afterward. Photo History also retains the
before and after states. Batches support up to 5,000 photos, including RAW/JPEG
companions when **Link pair metadata** is enabled.

### Assisted culling

Choose a criterion to review matching photos in the grid, then apply pick or
reject flags to the displayed matches. Existing flags are preserved unless
**Replace existing flags** is enabled. Counts include linked RAW/JPEG files;
**Undo last flag batch** restores the batch without overwriting later decisions.

**Review similar photos** opens conservative groups in Survey, using visual
similarity and actual capture times within 30 seconds. Focus and eye checks can
suggest a strongest frame; ties or insufficient evidence show no recommendation.
These groups do not change flags or create catalog stacks. New analysis requires
the macOS Photo Index; existing records refresh through its worker.

## Exchange metadata through XMP

1. In the other editor, save metadata to adjacent XMP sidecars.
2. Choose **Folders ••• → Import sidecars…** in LightTable. This menu action reads
   supported metadata; it does not apply another editor's develop settings or
   crop. Review the import report.
3. To write later changes beside originals, enable automatic XMP writing in
   **Settings → Files & Metadata**.

Supported fields include ratings, pick/reject marks, standard color labels,
flat and hierarchical keywords, and descriptive IPTC fields. Explicit empty
values clear fields; absent values preserve them. Unsupported custom color
labels are reported as skipped. The importer reads every catalog page, up to
100,000 photos per import, and leaves virtual-copy metadata independent.

LightTable merges changed fields with existing XMP and preserves unrelated
properties. It retains the first sidecar backup before updating it. Failed or
offline writes stay queued, including after restarting. Catalog moves update
the queued write's location without discarding its conflict checks.

If another application changes the same XMP property after the last read or
sync, writing stops for that sidecar and reports a conflict. Review the external
values and import sidecars to accept them before making further changes. Use
one naming convention per original: `photo.xmp` or `photo.RAW.xmp`. If both
exist, inspect them and resolve the ambiguity before syncing.

Photo Mechanic and digiKam conventions are covered by
[representative fixtures](tests/fixtures/xmp/README.md). Live round trips through
those applications have not been verified. Metadata compatibility does not
imply equivalent rendering or complete catalog interchange.

## Keep recovery and performance claims separate

XMP is not a complete catalog backup. Back up the catalog for collections,
history, and variants; back up originals separately. Portable LightTable edits
use `.lighttable-state.json` when enabled.

The [50,000- and 100,000-photo benchmark](bench/dam-performance.md) measures
catalog queries and state updates. It found faster deep pagination and rating
transactions, but not a faster first-page query in every run. It does not
measure RAW decoding or prove smooth scrolling. Face identity recognition and
map-based browsing are not part of these additions.
