# Preset interchange

LightTable presets carry named, explicitly included settings. The default
application changes those settings and keeps other edits. The separate
**Replace adjustment settings** action resets broader adjustments for older and
full-edit presets.

Built-in and community presets use **look scope**: creative adjustments with
photo corrections protected. LightTable continues to read older native presets
and approximate conversions from other editors.

## Supported formats

| Source or target | Import | Export | Notes |
| --- | --- | --- | --- |
| LightTable `.ltpreset` | Yes | Yes | Native look and legacy full-edit recipes, including their declared settings. |
| Lightroom / Camera Raw `.xmp` | Yes | Yes | Approximate control conversion. ZIP bundles are accepted. |
| Lightroom legacy `.lrtemplate` | Yes | No | Imports declarative scalar settings and point curves without evaluating Lua. |
| Capture One `.costyle` | Yes | Yes | Approximate control conversion. |
| Capture One `.costylepack` | Yes | No | Imports supported `.costyle` members from the package. |

## What maps

The converter maps controls with a defensible counterpart: exposure; basic
tone; temperature and tint where the source carries usable relative data;
vibrance and saturation; texture, clarity, and dehaze; point curves; the
eight-band colour mixer; vignette; sharpening; luminance and colour noise
reduction; and manual red/cyan or blue/yellow chromatic-aberration correction.

Ranges and colour engines differ, so a numeric round trip between products is
not colourimetric proof. LightTable stores the imported source, mapped-control
count, skipped operations, and conversion notes with each preset and displays
that report before application.

## What does not map across external formats

Camera profiles, proprietary looks and LUT payloads, local or AI masks,
healing edits, geometry and crop, lens-profile identities, subject or sky
selection, and engine-specific rendering operations are skipped when moving
between different editors. Legacy/full-edit native `.ltpreset` files can
preserve LightTable masks, healing, and lens geometry. Version 3 look files
exclude those photo corrections. LightTable does not silently bake an
unsupported operation into a hidden transform or claim that a similarly named
control is equivalent.

## Application behavior

- Click a preset card to apply it immediately to the main photo. Click it again
  to disable it; re-enabling restores the chosen amount. All rendering is local.
- **Amount** runs from 0% (the edit before the preset) to 100% (the full preset).
  Double-click the slider to reset it to 100%. Numeric adjustments, curves,
  color corrections, and added local-edit opacity scale with Amount. Film
  stock, on/off switches, and other fixed choices use the preset at any nonzero
  amount. This adjusts settings; it is not an opacity blend of two rendered images.
- Switching cards uses the edit before the first preset. Manual changes outside
  the preset are retained. Changing a preset-controlled setting retires its
  Amount adjustment and starts a new baseline for the next application.
- Selection, enabled state, amount, and baseline are stored with the photo and
  included in Undo/Redo and persistent History. Closing details only hides them.
- **Look · keep photo corrections** protects Edit Exposure and Temp/Tint, capture
  white balance, RAW settings, film format, crop, geometry, masks, healing,
  sharpening, noise reduction, and lens corrections.
- **Full edit · include photo corrections** can save these corrections. Its
  **All adjustment settings** and **Changed settings only** choices retain the
  legacy style/tool inclusion behavior.
- For legacy/full-edit presets, **Apply mapped adjustments** changes included
  grade controls, appends included masks/healing, and layers non-default optics.
  **Replace adjustment settings** resets the grade, masks, healing and optics
  before applying the recipe. Both keep the crop.
- `filmMode` declares **on**, **off**, or **preserve** for a look. Legacy presets
  use `includeFilm` and their stored Film parameters. The explicit **Turn Film
  off for the actions below** option overrides either recipe when using the
  management application controls.

External imports can recommend turning Film off for a closer match to the
source editor. This is a recommendation: selecting or applying an imported
preset does not automatically honor that suggestion. The user chooses the
Film-off option in Manage presets.

The Mac app accepts individual preset files, ZIP bundles, or a whole folder.
Folder imports are recursive, which makes it practical to select an existing
Camera Raw settings directory or a downloaded preset pack in one step.

Profile-only files sometimes appear alongside ordinary presets. LightTable
keeps them visible with a `0 mapped settings` report, but disables Apply so an
unsupported profile or embedded look cannot accidentally reset the current edit.

## Native version 3

The envelope distinguishes native schema versions from individual recipe
versions:

```json
{
  "format": "LightTable Preset",
  "version": 3,
  "presets": [{
    "id": "lighttable/natural-finish",
    "version": "1.0.0",
    "name": "Natural Finish",
    "scope": "look",
    "filmMode": "off",
    "includeFilm": false,
    "params": {},
    "includedFilm": [],
    "grade": {"contrast": 0.12, "vibrance": 0.08},
    "includedGrade": ["contrast", "vibrance"],
    "description": "Gentle contrast and color for everyday photographs.",
    "tags": ["Everyday"],
    "author": {"name": "LightTable", "url": "https://lighttable.app"},
    "license": "CC0-1.0"
  }]
}
```

This is a format example, not the canonical Natural Finish recipe. The bundled
recipes live in `presets/builtin.json`.

Version 3 entries must use `scope: "look"` and a supported `filmMode`.
`includedGrade` and `includedFilm` declare exactly the keys carried in `grade`
and `params`. Unsupported keys, values, stock profiles, and photo corrections
are rejected rather than silently changing a shared recipe. Film-on looks must
identify a stock.

Creative grade settings include contrast, highlights, shadows, whites, blacks,
vibrance, saturation, texture, clarity, dehaze, vignette, curves, HSL, Point
Color, and Color Grading. Creative Film settings include stock, paper, workflow,
output recipe, development, Camera EV, metering, print exposure, grain,
halation, diffusion, and scan controls. Camera EV is an intentional creative
film treatment here; the separate Edit Exposure correction remains protected.
The authoritative allowlists are `CREATIVE_GRADE_KEYS` and
`CREATIVE_FILM_KEYS` in `preset_library.py`, mirrored by `web/presets.js`.

Version 1 and 2 files retain their old included-control behavior; full-edit
exports remain version 2. Version 3 export preserves the look's settings,
Film behavior, author, license, description, tags, recipe version, and optional
`parentId` attribution. Unsupported future envelope versions fail import with
an update message. Import assigns a separate local preset identity; an imported
copy does not become a subscription to community updates.

## Collection and catalog metadata

`GET /api/presets` combines read-only bundled presets (`collection: "builtin"`)
with saved presets (`collection: "yours"`). Stable IDs identify selections,
favorites, and hidden built-ins, so duplicate names do not collide. Built-ins
can be hidden/restored or saved as a named copy. Saving a variation keeps its
parent attribution while detaching it from community updates.

The community gallery is static data at
`https://lighttable.app/presets/catalog-v1.json`. The catalog's `schemaVersion`
is 1; each listing separately declares native recipe `schemaVersion: 3` and
supported `capabilities`, currently `look-v1`. Listings carry:

- A stable namespaced `id`, semantic recipe `version`, `name`, description,
  tags, author, license, `filmMode`, and optional parent attribution.
- `featured` and `publishedAt` for editorial and chronological browsing.
- `file: {url, sha256, bytes}` pointing to an immutable versioned recipe at
  `/presets/files/<author>/<slug>/<version>.ltpreset`.
- `previews` containing labeled before/after image URLs and optional
  `credit: {name, url, license}`, plus a public `pageUrl`.

The app fetches the catalog through its local server, validates recipes against
the listing, and checks their size and SHA-256 before installation. Catalog
assets must stay on the configured HTTPS gallery origin under `/presets/`;
redirects and arbitrary recipe URLs are refused. A catalog is limited to
2 MiB and 2,000 listings; a remote recipe to 512 KiB. The catalog is cached for
24 hours unless manually refreshed. An unavailable host falls back to the
cached catalog with an offline status. Cached recipes can be tried offline;
installed recipes remain in the local preset library independently of the
bounded download cache. Hosted example images can still need a connection.

**Add to Yours** installs as `community:<catalog id>` and records
`community: {id, version}`. **Update in Yours** deliberately replaces that local
recipe after validating the requested version. It does not rewrite existing
photo edits, which already contain their applied settings. Incompatible
listings remain viewable but cannot be installed or applied.

A `lighttable://preset/<author>/<slug>` link opens the preset's details in the
desktop app. It does not install or apply the recipe automatically. The browser
entry also accepts `?preset=<catalog id>`.

**Community submission** in saved-preset details first inspects a cheap,
read-only version 3 draft from `POST /api/presets/submission` with `{id}`. It
shows the exact `includedGrade`, `includedFilm`, Film behavior, and protected
photo corrections before enabling export.

**Prepare community submission…** then requests `{id, examples: true}`. A
bounded local renderer uses three fixed, licensed sample scenes from
`presets/samples`, independently of the user's catalog or photographs. The
returned ZIP contains the `.ltpreset`, six before/after JPEGs at a 960-pixel
long edge, `photo-credits.json`, and a rights README. The standard examples
contain no EXIF or location metadata. Only one standard-example bundle is
prepared at a time, with a 90-second subprocess limit; an error leaves the
saved preset available. `preset_submission.py` owns this export behavior.

**Download example on this photo** is a separate explicit local JPEG export of
the prepared shared look on the selected photograph. It uses the local preview
renderer at a 960-pixel long edge and leaves the edit unchanged. The interface
reminds the user to share only photos they have permission to publish.

Binary ZIP/JPEG downloads use `{encoding: "base64", content, contentType,
filename}`; text `.ltpreset` exports keep their existing plain content shape.
The browser decodes bytes before download, and native hosts receive the same
encoding declaration for their save dialogs.

The preparation response also supplies a GitHub submission-form link. The user
saves and inspects the package, attaches it in the form, and provides creator
details and separate recipe/photo publishing permissions. Preparation does not
publish anything or send photographs. Contributions are reviewed before
entering the public static catalog.

## Beyond presets

Two larger migration paths use the same converter and the same honesty about
its limits.

**Per-image XMP sidecars.** `photo.xmp` and `photo.CR2.xmp` are both
recognised, in attribute and element form, and embedded XMP inside JPEG, TIFF,
HEIC, and DNG is read the same way. Rating, colour label, keywords including
their hierarchy, IPTC, GPS, crop, and develop settings are read; metadata is
applied by default and develop settings are not, because they were authored
against a different renderer. Sidecars can also be written back, opt-in, so a
folder carries its own ratings, keywords, and rights to another machine or
another editor.

The library's **Import sidecars…** action reads metadata only, including
supported pick/reject conventions. Flat keywords and hierarchical paths are
retained together. An explicit empty field clears that value; an absent field
does not. Custom color labels outside LightTable's palette are reported as
skipped. If both sidecar naming conventions exist for one original, import and
write-back report the ambiguity.

Write-back merges changed fields, preserves unrelated metadata, retains the
first sidecar backup, and checks for conflicting external changes since the
last read or sync. Offline or failed writes remain queued. Representative
Photo Mechanic and digiKam fixtures exercise these conventions; live round
trips through those applications remain unverified. See the
[XMP workflow](DIGITAL-ASSET-MANAGEMENT.md#exchange-metadata-through-xmp) and
[fixture provenance](tests/fixtures/xmp/README.md).

**Lightroom Classic catalogs.** A `.lrcat` is copied and opened read-only, so
it can be imported while Lightroom is running, and every table and column is
checked before it is read: a version that stores something differently produces
a named skip rather than a failure. Ratings, flags, colour labels, keyword
hierarchies, collections and collection sets, stacks, virtual copies, IPTC,
GPS, develop settings, and optionally edit history are mapped. Not mapped, and
reported: AI masks and LUT payloads in `.lrcat-data`, smart-collection criteria
beyond rating, flag, label, and free text, crop where only the cropped
dimensions survive, straighten beyond the supported range, and video develop
settings.

## File safety

Imports are local. A preset file is limited to 25 MB, individual expanded
members to 3 MB, and archives to 250 members and 25 MB expanded data. Only
known preset extensions inside an archive are inspected.
