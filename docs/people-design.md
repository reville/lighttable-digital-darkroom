# People: research and implementation

Reviewed 2026-09-08. An optional local catalog feature using SFace INT8.

## Prior art

| App | Useful pattern | LightTable choice |
| --- | --- | --- |
| [Google Photos](https://support.google.com/photos/answer/6128838?co=GENIE.Platform%3DDesktop&hl=en) | Face gallery, private names, merging duplicate groups, and a chosen cover photo | Start with a face gallery; names become searchable. Keep corrections close to the photographs. |
| [Lightroom Classic](https://helpx.adobe.com/lightroom-classic/desktop/organize-photos-in-lightroom-classic/face-recognition.html) | Named and unnamed face stacks; review and confirmation; catalog-wide or as-needed scanning | Separate Unnamed and Review matches views; explicit scan progress and pause. |
| [digiKam](https://docs.digikam.org/en/left_sidebar/people_view.html) | Face crops, confirmed versus unconfirmed matches, reversible ignored faces, batch tagging | Crop faces for comparison, allow batch splitting, keep Hidden reversible. Remember rejected pairings, which digiKam's documentation says can recur. |

## Interface

Visual thesis: a quiet dark library surface with face photographs providing the
hierarchy, one existing accent color, and ample space between groups.

Content plan: People gallery; named group with Faces and Photos views; side-by-side
possible matches; corrections, pause, and deletion. Setup explains the download
size and local processing before the first scan.

Interaction thesis: a short workspace entrance, restrained face-hover zoom,
and a status pulse only during scanning. Respect reduced-motion preferences.

## Matching policy

The [OpenCV Zoo SFace](https://github.com/opencv/opencv_zoo/tree/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_recognition_sface)
INT8 weights occupy 9,896,933 bytes. YuNet detection/alignment adds 232,589 bytes.
The OpenCV CPU runtime is packaged separately from these downloadable weights.
Both files are pinned to an immutable revision, size, and SHA-256. Downloads
are atomic; photos are never sent to a network service.

Automatic unnamed grouping requires conservative exemplar similarity, agreement
across reference faces, and a margin over the runner-up. Named matches remain
suggestions. Two detected faces in one photograph are not automatically matched;
manual merging can handle mirrors/collages. Suggestions use a lower threshold
and never display uncalibrated probability percentages.

Thresholds are product starting points, not a measured personal-library accuracy
claim. Blur, profile, occlusion, small faces, and age changes need library
validation. No names are inferred; pets are not supported by this model.

## Persistence and scope

Face vectors, crops, names, and correction constraints are in a separate SQLite
database keyed by catalog path under Application Support/AI Index/People.
The generated Vision index and originals are unaffected. Unchanged photos are
reused. Refreshed photos retain group assignments only when a face still overlaps
spatially and its embedding remains similar. Rejected pairings survive restarts
and merges; splitting creates an exclusion between the resulting groups.
Stable catalog image IDs preserve face assignments when files move or are renamed.
Pausing keeps names searchable. Deleting face data requires a dedicated
confirmation and leaves model downloads available.

Undo stores one prior grouping state and excludes vector/crop blobs. Scans that
add, update, or remove photos invalidate it rather than rewinding new analysis.
Catalog backups do not include this separate face database; in-app Help states
this limitation.

## Validation

`scripts/smoke-face-matching.py --model-dir <downloaded-model-directory>` runs
real quantized inference against the CC0 portrait fixture, a smaller recompressed
and darkened version, and a blank image. It requires verified weights already
present unless `--download` is explicitly supplied. The packaged OpenCV 4.13
runtime passed this check (one face in each portrait; similarity 0.9553; no face
in the blank image).

An isolated catalog of 45 locally derived review previews produced 56 detected
faces, 19 initial groups, and zero processing errors. These counts demonstrate
the workflow, not accuracy against labeled identities. Real UI checks covered
name search, split/undo, match rejection/undo, merge/undo, and hiding/restoring a
group. Private review photos and their face data are not repository fixtures.
