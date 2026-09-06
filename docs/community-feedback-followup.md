# Community feedback follow-up

This work uses RapidRAW discussions, issues, and pull requests as evidence of real photo workflows. A complaint about another application is a lead to investigate, not proof of the same defect in LightTable. Closed threads, proposed fixes, and merged code are separate from verified product behavior.

## Editing and organization delivered in this follow-up

- Copy Settings opens a remembered chooser for Film, RAW development, Tone, Color, Detail, Lens corrections, Crop/geometry, Masks, and Healing. Unchecked groups preserve the destination. An edited destination must load successfully before merging. Paste uses the edit-save queue and History; failed saves retain the intended patch for Retry.
- AI mask transfer detects supported selections again for each destination. Object selections and painted AI refinements require a fresh selection and are refused with an explanation. Manual masks and healing positions are explicit choices intended for matching framing. No source subject bitmap is silently assigned to another photo.
- Crop Done and Enter accept the current frame. Cancel and Escape restore the crop and geometry present when the tool opened, including after a temporary before/after comparison. Other edits and earlier history are preserved.
- The viewer offers dark gray, neutral gray (`#777777`), and white surrounds. Detail status distinguishes a refining RAW or incomplete preview from delivered source detail at 100%.
- Same-source, same-folder RAW+JPEG pairs support Both, Prefer RAW, and Prefer JPEG views, a companion switch, and optional linked ratings, flags, labels, and keywords. A filter that matches only the companion can still reveal it. Virtual copies and ambiguous multiple-file matches remain independent. Explicit selected export and Trash operations retain their chosen file identities.
- Files & Metadata exposes the existing portable `.lighttable-state.json` mirror preference. Turning it off does not delete existing mirrors. XMP writing remains independently selectable.
- Info → Correct capture time previews an exact selection before applying a clock shift, explicit UTC offset, or restoration of original camera times. Overrides survive rescanning and travel in portable LightTable state and XMP. They affect capture-date sorting and exported metadata, while original file contents and filesystem dates stay unchanged. Apply and before/after History are transactional; capture-time History restores only the date correction. Missing camera dates are reported rather than replaced with filesystem modification times.

Capture correction supports up to 5,000 explicitly selected photos, with optional RAW+JPEG companions. Time-zone assignment does not also shift the clock: enter a shift separately when the camera clock itself is wrong. “Remove” clears an incorrect UTC offset. Naive camera timestamps remain unzoned; the application does not infer a location or daylight-saving rule from them.

## Areas that still need separate work or evidence

- **Camera and recording-mode qualification:** a filename extension or camera model in a decoder list is not proof that every compression, bit-depth, pixel-shift, dual-pixel, or high-resolution mode works. A publishable compatibility matrix needs representative original samples, metadata checks, decoding, and visual comparison for each mode. This patch does not claim universal RAW support or replace missing samples with generated fixtures.
- **Localization:** the application remains English. A translation system, human-reviewed terminology, locale-aware formatting, and layout testing in target languages remain open. This patch does not ship mass machine translation.
- **Custom keyboard remapping:** the two existing schemes and MIDI mapping remain available. Arbitrary action remapping requires one conflict-checked mapping shared by web keyboard handling, native menu key equivalents, text inputs, and modal workflows. That complete remapping system is not included here.
- **Full-gamut editing proof:** selecting an ICC export profile, enabling soft proof, and displaying an image on a wide-gamut monitor do not independently prove that every interactive processing stage preserves out-of-sRGB color. A separate measured pipeline audit and wide-gamut reference images are still needed before claiming full-gamut edit/preview equivalence.
- **Hardware and photographic quality:** Windows/native GPU behavior, unusual cameras, and denoise/mask quality need representative hardware and photographs. Automated state and pixel tests are evidence for the cases they exercise; they are not a substitute for those qualification passes or a photographer usability study.

Publication status and native running-product verification belong in the release or pull-request record for the integrated revision, rather than being inferred from this implementation inventory.
