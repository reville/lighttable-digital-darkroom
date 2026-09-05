# Preset interchange

LightTable treats a preset as a named set of included controls, not as an opaque
look. A **style** replaces the current grade; a **tool preset** layers only its
included settings. Either may include the Film pipeline, apply after it, or
recommend switching Film off.

## Supported formats

| Source or target | Import | Export | Notes |
| --- | --- | --- | --- |
| LightTable `.ltpreset` | Yes | Yes | Lossless for LightTable settings and style/tool semantics. |
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
between different editors. Native `.ltpreset` files preserve LightTable masks,
healing, and lens geometry losslessly. LightTable does not silently bake an
unsupported operation into a hidden transform or claim that a similarly named
control is equivalent.

## Application behavior

- **Replace edit / full style** resets the grade, then applies the preset's
  included controls.
- **Layer mapped settings / additive** changes only included controls and
  leaves every other current edit intact.
- **Turn Film profile off when applied** makes the preset an alternative to the
  physical Film workflow.
- **Include Film profile and physical settings** makes a native LightTable preset
  restore Film state as well as grade state.
- Native presets also preserve masks, healing, and lens corrections; additive
  application appends local edits and layers non-default geometry settings.

Imported presets default to Film off because their tone and colour decisions
were authored against a different base renderer. You can uncheck that choice
to use the converted settings additively after a Film render.

The Mac app accepts individual preset files, ZIP bundles, or a whole folder.
Folder imports are recursive, which makes it practical to select an existing
Camera Raw settings directory or a downloaded preset pack in one step.

Profile-only files sometimes appear alongside ordinary presets. LightTable
keeps them visible with a `0 mapped settings` report, but disables Apply so an
unsupported profile or embedded look cannot accidentally reset the current edit.

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
