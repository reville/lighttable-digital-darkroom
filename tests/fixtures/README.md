# Lightroom Classic migration fixture

`lightroom-classic-15-migration.lrcat.gz` is a populated catalog authored by
Adobe Photoshop Lightroom Classic 15.5.1 (catalog schema `1504001`). It refers
to the two existing source images `demo-assets/cc0-raw/contact-sheet.jpg` and
`demo-assets/cc0-raw/portrait-contact-sheet.jpg`.

The fixture contains ratings, flags, colour labels, hierarchical keywords,
IPTC and GPS metadata, develop settings and crop geometry, a folder stack, a
collection set with regular and smart collections, edit history, and a virtual
copy with independent settings. After Lightroom closed, the catalog was
checkpointed, vacuumed, and its source root was sanitised to
`/lightroom-classic-15-fixture/` before deterministic gzip compression. Preview
and `.lrcat-data` sidecars are intentionally omitted because the importer does
not read them.

`PopulatedLightroomCatalogTests` extracts the catalog into a temporary
directory, copies the two source images there, remaps the sanitised root, and
performs an end-to-end import. Routine migration testing therefore does not
require Lightroom Classic to be installed; Lightroom is only needed when this
fixture must be regenerated for a new catalog schema.

# GPU grade precision fixture

`gpu-grade-dark-uniformity.npy` contains only the pixel values of a 17×17
float32 RGB crop from a dark photograph. Its center pixel exposed
Point Color luminance uniformity amplifying tiny earlier GPU rounding
differences. The regression tiles this crop without resampling so its center
pixel retains the exact neighbor context while exercising export-size routing.
