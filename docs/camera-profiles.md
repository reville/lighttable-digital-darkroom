# Camera profiles

LightTable reads DNG camera profiles (`.dcp`) and applies them following
chapter 6 of the DNG 1.6 specification. One profile is bundled, the modelled
`LightTable Standard` look; every other profile comes from a folder of the
user's own files. This document is the reference for how each part of a
profile is applied, what is deliberately approximate, and how the bundled look
is defined. The user-facing summary lives in Help under Settings and defaults.

## Where profiles come from

- `LightTable Standard` (`profiles/lighttable_standard.dcp`) resolves by its
  bare name without any folder, in the editor, the command line and exports.
- The profile folder is the `cameraProfileFolder` preference. When it is
  empty, the first existing default is used, in this order:
  1. `CameraProfiles` inside LightTable's data directory:
     `$XDG_DATA_HOME/lighttable/CameraProfiles` (Linux, default
     `~/.local/share/lighttable/CameraProfiles`),
     `%LOCALAPPDATA%\LightTable\CameraProfiles` (Windows),
     `~/Library/Application Support/LightTable/CameraProfiles` (macOS).
  2. On macOS and Windows only, the locations Adobe Camera Raw and Lightroom
     install their DCP profiles to. Linux has no vendor location.
- Files are indexed by bare name within two folder levels, so an edit stores a
  file name and never a path.

The Camera profile picker of a RAW photo lists `LightTable Standard` first and
then the folder's files that name the photo's camera model, either in the file
name or in the profile's `UniqueCameraModel` or `ProfileName` tag. The tag
match reads only the two text tags from each file's header
(`camera_profile.profile_identity`), cached per file version, so a folder of
thousands of profiles is scanned once.

## What is applied, and where

A RAW photo passes through two stages. `camera_calibration` handles the first,
`camera_profile.apply_profile` the second.

**Decode (`color_pipeline.decode_raw`)**, when the profile carries
`ColorMatrix1`:

1. LibRaw demosaics and applies the white-balance multipliers, and is asked
   for camera-native output (`output_color=raw`) instead of its own ProPhoto
   conversion. The changed option keys a separate demosaic cache entry.
2. The capture illuminant's temperature is estimated from the camera neutral
   (the reciprocal of the multipliers LibRaw balanced by) with the
   specification's fixed-point iteration: interpolate the colour matrix at a
   guessed temperature, map the neutral to XYZ through its inverse, read the
   temperature back from the chromaticity (McCamy), repeat.
3. `ColorMatrix1`/`2` and `ForwardMatrix1`/`2` are blended entry-wise in
   reciprocal temperature between the two calibration illuminants.
4. With a forward matrix, white-balanced camera RGB goes to XYZ D50 through
   it. Without one, the inverse colour matrix gives XYZ under the scene
   illuminant and a Bradford transform adapts the neutral's white to D50.
5. XYZ D50 goes to linear ProPhoto RGB (ROMM matrix), scaled so the neutral
   is exactly equal RGB, clipped to range and stored as the scene-linear
   master. Film and Develop both start from it.

The remembered capture temperature travels to the Develop stage in a bounded
in-process memory keyed by source identity, white-balance basis and profile;
when it is missing, it is re-estimated from the RAW header without a decode.

**Develop (`color_pipeline.linear_prophoto_to_display`)**, Film off,
Standard or Soft profile, in this order:

1. `ProfileHueSatMapData1`/`2`, blended at the capture temperature with the
   same 1/T weight, in the encoding `ProfileHueSatMapEncoding` names.
2. The Develop exposure normalisation (the existing 99.5th-percentile gain).
3. `BaselineExposureOffset` as a linear gain of 2^offset.
4. `ProfileLookTableData` in its `ProfileLookTableEncoding`.
5. `ProfileToneCurve`, hue-preserving, replacing the built-in Standard or
   Soft curve. A profile without a curve keeps the built-in curve.

`DefaultBlackRender` is honoured trivially: LightTable applies no automatic
black subtraction, so `Auto` and `None` render alike. `ProfileEmbedPolicy` is
read and reported; nothing embeds a profile anywhere. The Linear develop
profile ignores the look stage, as before.

A photo whose edit selects `Built-in` (an empty `camera_profile`) takes none
of this and renders byte-identically to earlier releases;
`tests/test_camera_profile_develop.py` holds the recorded digests.

## What remains approximate

- Demosaic, highlight reconstruction and sensor noise reduction come from
  LibRaw, not from a DNG reference converter. Two converters agreeing on the
  matrices still differ slightly in interpolated pixels.
- Automatic white balance: LibRaw does not expose the multipliers it chose,
  so the as-shot neutral stands in for the temperature estimate. The pixels
  are still balanced by the automatic multipliers.
- The calibration illuminants are taken at nominal temperatures
  (`camera_profile.ILLUMINANT_TEMPERATURES`); the chromaticity-to-temperature
  step is McCamy's approximation, within a few kelvin over the calibrated
  range.
- `AnalogBalance` and `ReductionMatrix` are DNG file tags LibRaw does not
  expose; they are taken as identity.
- A profile whose second-illuminant matrix has no first (forbidden by the
  specification) or whose encoding tag is outside the two defined values is
  reported in `profile_summary["unsupported"]` and applied with the parts
  that are usable.

## The bundled look

`LightTable Standard` is a modelled generic rendition, not a measurement of
any camera and not derived from any other profile. Its complete definition is
the numeric targets in `profiles/build_lighttable_standard.py`: a tone curve
that lands scene middle grey (0.18) at display code 0.52, keeps the mid-tone
slope a little above one and rolls the highlights off into white, plus a
hue/saturation map of at most four degrees of hue rotation and eight percent
of saturation lift that fades out for fully saturated colours. It carries no
colour matrices, so the decoder's camera calibration stays in charge and the
RAW decode is unchanged by selecting it.

`tests/test_lighttable_standard_profile.py` regenerates the file from the
script and requires the tracked bytes to match, and checks the stated targets
in the file the reader sees.

It is the Develop Defaults camera profile for RAW photos with no saved edit
state (`newPhotoDefaults.cameraProfile`, `standard` by default, `builtin` to
start from the analytic curve). `DEFAULT_PARAMS["camera_profile"]` stays
empty, so a saved edit that predates the control keeps rendering with the
built-in curve; the editor also pins such an edit to `Built-in` when it
opens one.

## Writing profiles

`camera_profile_write` assembles a `.dcp` from plain values: a TIFF header
and one IFD of metadata tags, in either byte order. The bundled look and the
test suite are both built with it, so the reader is tested against files
whose contents are known to the byte.
