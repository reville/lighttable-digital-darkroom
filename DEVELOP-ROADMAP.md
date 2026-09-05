# Develop tool parity milestone

LightTable now covers the essential raw-editing path without requiring a Film
render: neutral raw development, global light and colour controls, tone curves,
an eight-band colour mixer, Point Color, tonal Color Grading, local masks and gradients, deterministic heal and
clone spots, matched lens profiles, perspective correction, crop and rotation,
histogram and clipping, conventional detail controls, presets, versions,
copy/paste, reusable full-resolution export recipes, photo merge, and soft
proofing.

The six high-value parity gaps from the review are complete:

1. **Advanced RAW quality.** Capture-stage profiles, highlight reconstruction,
   sensor-aware denoise, and per-camera defaults are cache- and export-aware.
2. **Semantic masking.** Subject, Sky, and seeded Object selections compose as
   ordered Add, Subtract, and Intersect components in preview and export.
3. **Export workflow.** Recipes retain destination, filename template, format,
   dimensions, color space, and collision behavior.
4. **Library organization.** Regular/smart collections, stacks, virtual copies,
   minimum-rating and file-kind filters are persisted beside the catalog.
5. **Advanced color.** Point Color and shadow/midtone/highlight/global grading
   share one schema and render contract between WebGL and full-size export.
6. **Output workflows.** HDR exposure fusion, feature-aligned panorama merge,
   and non-destructive soft proofing are available from one output panel.

Useful follow-ons, outside this milestone, are guided upright lines, red-eye
correction, duplicate detection, richer metadata editing, background ingest
previews, custom printer-profile proofing, and optional super-resolution.

Native presets preserve local masks, healing, and lens geometry. Cross-engine
preset import still does not pretend proprietary masks, profiles, or geometry
are portable: the conversion report names unsupported operations as skipped.

The follow-ons above, together with the central catalog, catalog and sidecar
import, card ingest, and metadata work, are planned in
[WORKFLOW-ROADMAP.md](WORKFLOW-ROADMAP.md).
