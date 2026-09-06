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

## Wide-gamut export boundary

Develop exports without global color adjustments, enabled masks, healing,
optics adjustments, or watermarks preserve source colors when exporting to
Display P3 or ProPhoto RGB. RAW files use the same tone curve and brightness
normalization as the sRGB preview, then convert the unclipped rendering to
the delivery color space. Processed photos use their embedded source profile.
Rotation in 90-degree steps, crop, and output resizing remain available.
The output carries the selected ICC profile; missing required profiles stop
the export with an error.

The preview remains sRGB. Colors inside sRGB keep the same appearance; wider
source colors can appear richer in a color-managed viewer on a capable display.
Film rendering and Develop images with color or local adjustments still use
the existing bounded sRGB rendering, then convert to the selected output
profile. These exports report that limitation as a per-file warning.

**Full wide-gamut grading is not implemented.** The CPU grade, local edits,
watermarks, WebGL shader, and native preview currently share an sRGB contract.
Changing only their input primaries or removing clamps would make preview and
export disagree. Before enabling wide-gamut grading, all those paths need an
explicit shared working-space contract, matched transfer functions and gamut
mapping, and preview/export tests for saturated RAW colors, neutral ramps,
global and local edits, soft proofing, and calibrated output profiles. Existing
sRGB edits must retain their appearance or receive an explicit migration.
