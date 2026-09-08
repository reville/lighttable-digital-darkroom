# XMP interoperability fixtures

Both `.xmp` files here are **hand-authored representative fixtures**, not exports
from installed Photo Mechanic or digiKam. Their invented photographs, people,
agency, profile, history and application markers are test data. They establish
parser/merge behavior for documented structures; they do not certify complete
application-to-application compatibility.

Field choices were checked against these primary implementation sources and
application documentation on 2026-09-08:

- [digiKam pick/color enums](https://github.com/KDE/digikam/blob/master/core/app/utils/digikam_globals.h),
  [metadata label reader/writer](https://github.com/KDE/digikam/blob/master/core/libs/metadataengine/dmetadata/dmetadata_labels.cpp),
  and [default keyword mappings](https://github.com/KDE/digikam/blob/master/core/libs/metadataengine/dmetadata/dmetadatasettingscontainer.cpp).
- [ExifTool's Photo Mechanic XMP definitions](https://github.com/exiftool/exiftool/blob/master/lib/Image/ExifTool/PhotoMechanic.pm):
  Tagged, ColorClass and the raw `tagged:class:rating:frame` Prefs layout.
- [Photo Mechanic color classes](https://camerabits.freshdesk.com/support/solutions/articles/48001142942-color-class-ratings)
  are customizable; a numeric class is not a portable color name.
- [digiKam sidecar settings](https://docs.digikam.org/en/setup_application/metadata_settings.html#sidecars-settings)
  document both `photo.raw.xmp` and commercial-compatible `photo.xmp` naming.

The tests cover standard ratings/rejection, Photo Mechanic tags, digiKam picks
and colors, keyword paths plus independent flat terms, Unicode IPTC text,
language alternatives, existing sidecar names, foreign editor data, and
external-edit conflicts while a write is pending. Color names outside
LightTable's five colors remain unsupported by the app, even though the XMP
module retains their text. Photo Mechanic's numeric color classes are preserved
without inventing a mapping to a user's customized labels.

Explicit clears are emitted as zero ratings, empty labels/keyword containers,
and empty IPTC values. The parser exposes `metadataPresent` so the importer
can distinguish these from omitted properties, and `metadataKeywords` uses
LightTable's `Parent > Child` notation. A partial update changes only supplied
metadata fields; native edit JSON is merged by field so changing a rating cannot
discard masks, film settings or unrelated Camera Raw data. Language alternatives
other than `x-default` survive caption/title/copyright changes. Changing the
creator field replaces its sequence; unrelated edits preserve all creators.

`sidecar_snapshot` stores property fingerprints for the server's persistent
outbox. `write_sidecar(..., expected_snapshot=...)` refuses a change when an
owned property differs from that baseline and the proposed value, while allowing
unrelated external changes to merge. Retries must retain the same baseline.
Fingerprint conflicts are conservative at the XMP-property level (for example,
the native edit JSON and Photo Mechanic Prefs each occupy one property). Both
existing sidecar filename forms are treated as ambiguous and require resolution.
An atomic filesystem replace and a final reread narrow concurrent-write races;
third-party applications do not share a lock, so this is not a cross-application
transaction protocol.

To add actual application evidence, create disposable copies in each installed
application, record its version and metadata preferences, export sidecars and
label their provenance separately. Test each application in both directions,
including clearing values, before claiming that application has passed.
