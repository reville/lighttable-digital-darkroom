"""Tests for camera_profile, exercising the real reader on real .dcp bytes.

Every profile used here is synthesised in the test by hand-assembling a TIFF
header and one IFD of metadata tags — the same shape as a shipped ``.dcp``,
which carries no image data. Nothing is mocked, no fixture files are read and
nothing touches the network.
"""
from __future__ import annotations

import colorsys
import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

import camera_profile


# --- .dcp synthesis -------------------------------------------------------

ASCII = 2
SHORT = 3
LONG = 4
SRATIONAL = 10
FLOAT = 11


def ascii_tag(text: str) -> tuple[int, int, bytes]:
    payload = text.encode("utf-8") + b"\x00"
    return (ASCII, len(payload), payload)


def short_tag(values, byteorder: str = "<") -> tuple[int, int, bytes]:
    values = list(values)
    return (SHORT, len(values),
            struct.pack(f"{byteorder}{len(values)}H", *values))


def long_tag(values, byteorder: str = "<") -> tuple[int, int, bytes]:
    values = list(values)
    return (LONG, len(values),
            struct.pack(f"{byteorder}{len(values)}I", *values))


def float_tag(values, byteorder: str = "<") -> tuple[int, int, bytes]:
    array = np.asarray(list(values), dtype=f"{byteorder}f4")
    return (FLOAT, int(array.size), array.tobytes())


def srational_tag(values, byteorder: str = "<",
                  denominator: int = 1000000) -> tuple[int, int, bytes]:
    values = list(values)
    pairs: list[int] = []
    for value in values:
        pairs.extend((int(round(value * denominator)), denominator))
    return (SRATIONAL, len(values),
            struct.pack(f"{byteorder}{len(pairs)}i", *pairs))


def build_tiff(tags: dict, byteorder: str = "<") -> bytes:
    """Assemble a TIFF header plus one IFD holding only ``tags``.

    ``tags`` maps a tag code to a ``(type, count, payload)`` triple. The IFD
    has no strip offsets and no byte counts, exactly like a camera profile.
    """
    magic = b"II" if byteorder == "<" else b"MM"
    header = struct.pack(f"{byteorder}2sHI", magic, 42, 8)
    entries = sorted(tags.items())
    data_offset = 8 + 2 + len(entries) * 12 + 4
    directory = b""
    blobs = b""
    for code, (kind, count, payload) in entries:
        if len(payload) <= 4:
            value = payload + b"\x00" * (4 - len(payload))
        else:
            value = struct.pack(f"{byteorder}I", data_offset + len(blobs))
            blobs += payload
            if len(blobs) % 2:
                blobs += b"\x00"
        directory += struct.pack(f"{byteorder}HHI", code, kind, count) + value
    return (header
            + struct.pack(f"{byteorder}H", len(entries))
            + directory
            + struct.pack(f"{byteorder}I", 0)
            + blobs)


def grid_bytes(hue_divisions: int, sat_divisions: int, value_divisions: int,
               triple, byteorder: str = "<"):
    """Build a hue/sat grid, value slowest then hue then saturation fastest.

    ``triple`` is called with (hue index, sat index, value index) and returns
    (hue shift in degrees, saturation scale, value scale).
    """
    values: list[float] = []
    for value_index in range(value_divisions):
        for hue_index in range(hue_divisions):
            for sat_index in range(sat_divisions):
                values.extend(triple(hue_index, sat_index, value_index))
    return float_tag(values, byteorder)


T = camera_profile


def profile_tags(*, name="Film Lab Test Profile", copyright_text=None,
                 hue_sat_dims=None, hue_sat_map=None, hue_sat_map_2=None,
                 look_dims=None, look_table=None, tone_curve=None,
                 illuminant1=17, illuminant2=None, colour_matrix=None,
                 forward_matrix=None, embed_policy=1, signature=None,
                 byteorder="<") -> dict:
    tags: dict = {}
    if name is not None:
        tags[T.TAG_PROFILE_NAME] = ascii_tag(name)
    if copyright_text is not None:
        tags[T.TAG_PROFILE_COPYRIGHT] = ascii_tag(copyright_text)
    if signature is not None:
        tags[T.TAG_PROFILE_CALIBRATION_SIGNATURE] = ascii_tag(signature)
    if hue_sat_dims is not None:
        tags[T.TAG_PROFILE_HUE_SAT_MAP_DIMS] = long_tag(
            hue_sat_dims, byteorder)
    if hue_sat_map is not None:
        tags[T.TAG_PROFILE_HUE_SAT_MAP_DATA_1] = hue_sat_map
    if hue_sat_map_2 is not None:
        tags[T.TAG_PROFILE_HUE_SAT_MAP_DATA_2] = hue_sat_map_2
    if look_dims is not None:
        tags[T.TAG_PROFILE_LOOK_TABLE_DIMS] = long_tag(look_dims, byteorder)
    if look_table is not None:
        tags[T.TAG_PROFILE_LOOK_TABLE_DATA] = look_table
    if tone_curve is not None:
        tags[T.TAG_PROFILE_TONE_CURVE] = float_tag(
            np.asarray(tone_curve, dtype=np.float64).ravel(), byteorder)
    if illuminant1 is not None:
        tags[T.TAG_CALIBRATION_ILLUMINANT_1] = short_tag(
            [illuminant1], byteorder)
    if illuminant2 is not None:
        tags[T.TAG_CALIBRATION_ILLUMINANT_2] = short_tag(
            [illuminant2], byteorder)
    if colour_matrix is not None:
        tags[T.TAG_COLOR_MATRIX_1] = srational_tag(colour_matrix, byteorder)
    if forward_matrix is not None:
        tags[T.TAG_FORWARD_MATRIX_1] = srational_tag(forward_matrix, byteorder)
    if embed_policy is not None:
        tags[T.TAG_PROFILE_EMBED_POLICY] = long_tag([embed_policy], byteorder)
    return tags


def write_profile(path: Path, byteorder: str = "<", **kwargs) -> Path:
    path.write_bytes(build_tiff(
        profile_tags(byteorder=byteorder, **kwargs), byteorder))
    return path


def hsv_of(pixel) -> tuple[float, float, float]:
    """Independent HSV conversion, in degrees, via the standard library."""
    red, green, blue = (float(channel) for channel in np.asarray(pixel)[:3])
    hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
    return (hue * 360.0, saturation, value)


SAMPLE_PIXELS = np.asarray([
    [1.0, 0.0, 0.0],
    [0.0, 1.0, 0.0],
    [0.0, 0.0, 1.0],
    [1.0, 1.0, 0.0],
    [0.0, 1.0, 1.0],
    [1.0, 0.0, 1.0],
    [0.5, 0.25, 0.125],
    [0.2, 0.4, 0.9],
    [0.5, 0.5, 0.5],
    [0.0, 0.0, 0.0],
    [1.0, 1.0, 1.0],
], dtype=np.float32).reshape(1, 11, 3)


class ProfileReadingTests(unittest.TestCase):
    def test_round_trip_name_tone_curve_and_hue_sat_map(self):
        tone_curve = [[0.0, 0.0], [0.25, 0.15], [0.5, 0.55], [1.0, 1.0]]
        colour_matrix = [0.8, -0.2, -0.05, -0.3, 1.2, 0.1, 0.02, -0.15, 0.7]

        def triple(hue_index, sat_index, value_index):
            return (float(hue_index) * 3.0,
                    1.0 + 0.1 * sat_index,
                    1.0 - 0.05 * value_index)

        with tempfile.TemporaryDirectory() as directory:
            path = write_profile(
                Path(directory) / "round-trip.dcp",
                name="Film Lab Test Profile",
                copyright_text="(c) Film Lab test",
                signature="com.filmlab.test",
                hue_sat_dims=(4, 3, 2),
                hue_sat_map=grid_bytes(4, 3, 2, triple),
                tone_curve=tone_curve,
                illuminant1=17,
                illuminant2=21,
                colour_matrix=colour_matrix,
                forward_matrix=colour_matrix,
                embed_policy=1,
            )
            profile = camera_profile.read_profile(path)

        self.assertEqual(profile["name"], "Film Lab Test Profile")
        self.assertEqual(profile["copyright"], "(c) Film Lab test")
        self.assertEqual(profile["calibrationSignature"], "com.filmlab.test")
        self.assertEqual(profile["path"], str(path))
        self.assertEqual(profile["hueSatDims"], (4, 3, 2))
        self.assertEqual(profile["illuminant1"], 17)
        self.assertEqual(profile["illuminant2"], 21)
        self.assertEqual(profile["embedPolicy"], 1)
        self.assertIsNone(profile["lookTable"])
        self.assertIsNone(profile["hueSatMap2"])

        np.testing.assert_allclose(
            profile["toneCurve"], np.asarray(tone_curve), atol=1e-6)
        np.testing.assert_allclose(
            profile["colorMatrix1"],
            np.asarray(colour_matrix).reshape(3, 3), atol=1e-6)
        np.testing.assert_allclose(
            profile["forwardMatrix1"],
            np.asarray(colour_matrix).reshape(3, 3), atol=1e-6)

        table = profile["hueSatMap1"]
        self.assertEqual(table.shape, (2, 4, 3, 3))
        for value_index in range(2):
            for hue_index in range(4):
                for sat_index in range(3):
                    np.testing.assert_allclose(
                        table[value_index, hue_index, sat_index],
                        triple(hue_index, sat_index, value_index),
                        atol=1e-6)

    def test_reads_big_endian_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_profile(
                Path(directory) / "motorola.dcp",
                byteorder=">",
                name="Big Endian",
                hue_sat_dims=(2, 2, 1),
                hue_sat_map=grid_bytes(
                    2, 2, 1, lambda h, s, v: (10.0, 1.0, 1.0), ">"),
                tone_curve=[[0.0, 0.0], [1.0, 1.0]],
                illuminant1=21,
            )
            profile = camera_profile.read_profile(path)
        self.assertEqual(profile["name"], "Big Endian")
        self.assertEqual(profile["hueSatDims"], (2, 2, 1))
        self.assertEqual(profile["illuminant1"], 21)
        np.testing.assert_allclose(
            profile["hueSatMap1"].reshape(-1, 3)[:, 0], 10.0, atol=1e-6)

    def test_garbage_file_raises_profile_error_with_a_readable_message(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "garbage.dcp"
            path.write_bytes(b"this is not a camera profile at all" * 4)
            with self.assertRaises(camera_profile.ProfileError) as caught:
                camera_profile.read_profile(path)
        message = str(caught.exception)
        self.assertIn("garbage.dcp", message)
        self.assertIn("not a readable camera profile", message)

    def test_truncated_file_raises_profile_error(self):
        with tempfile.TemporaryDirectory() as directory:
            whole = build_tiff(profile_tags(
                hue_sat_dims=(4, 2, 1),
                hue_sat_map=grid_bytes(
                    4, 2, 1, lambda h, s, v: (0.0, 1.0, 1.0)),
            ))
            path = Path(directory) / "truncated.dcp"
            path.write_bytes(whole[:len(whole) // 3])
            with self.assertRaises(camera_profile.ProfileError) as caught:
                camera_profile.read_profile(path)
        self.assertIn("truncated.dcp", str(caught.exception))

    def test_empty_and_missing_files_raise_profile_error(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty.dcp"
            empty.write_bytes(b"")
            with self.assertRaises(camera_profile.ProfileError) as caught:
                camera_profile.read_profile(empty)
            self.assertIn("empty", str(caught.exception))

            with self.assertRaises(camera_profile.ProfileError):
                camera_profile.read_profile(Path(directory) / "absent.dcp")

    def test_a_non_path_argument_raises_profile_error(self):
        with self.assertRaises(camera_profile.ProfileError):
            camera_profile.read_profile(42)

    def test_tiff_without_profile_tags_raises_profile_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plain.dcp"
            path.write_bytes(build_tiff({270: ascii_tag("just a TIFF")}))
            with self.assertRaises(camera_profile.ProfileError) as caught:
                camera_profile.read_profile(path)
        self.assertIn("no camera-profile tags", str(caught.exception))

    def test_table_without_dimensions_raises_profile_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dimless.dcp"
            path.write_bytes(build_tiff(profile_tags(
                hue_sat_dims=None,
                hue_sat_map=grid_bytes(
                    2, 2, 1, lambda h, s, v: (0.0, 1.0, 1.0)),
            )))
            with self.assertRaises(camera_profile.ProfileError) as caught:
                camera_profile.read_profile(path)
        self.assertIn("dimensions", str(caught.exception))


class ProfileSummaryTests(unittest.TestCase):
    def test_summary_reports_tables_and_names_what_is_approximated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_profile(
                Path(directory) / "summary.dcp",
                name="Portra Look",
                copyright_text="(c) somebody",
                hue_sat_dims=(6, 2, 1),
                hue_sat_map=grid_bytes(
                    6, 2, 1, lambda h, s, v: (0.0, 1.0, 1.0)),
                hue_sat_map_2=grid_bytes(
                    6, 2, 1, lambda h, s, v: (5.0, 1.0, 1.0)),
                look_dims=(4, 2, 1),
                look_table=grid_bytes(
                    4, 2, 1, lambda h, s, v: (0.0, 1.05, 1.0)),
                tone_curve=[[0.0, 0.0], [1.0, 1.0]],
                illuminant1=17,
                illuminant2=21,
                colour_matrix=[0.8, -0.2, -0.05, -0.3, 1.2,
                               0.1, 0.02, -0.15, 0.7],
                forward_matrix=[0.9, 0.05, 0.02, 0.2, 0.7,
                                0.1, 0.0, 0.05, 0.8],
            )
            summary = camera_profile.profile_summary(path)

        self.assertTrue(summary["ok"])
        self.assertTrue(summary["approximate"])
        self.assertEqual(summary["name"], "Portra Look")
        self.assertEqual(summary["copyright"], "(c) somebody")
        self.assertEqual(summary["illuminant1Name"], "Standard light A")
        self.assertEqual(summary["illuminant2Name"], "D65")
        self.assertTrue(summary["dualIlluminant"])
        self.assertTrue(summary["hasHueSatMap1"])
        self.assertTrue(summary["hasHueSatMap2"])
        self.assertTrue(summary["hasLookTable"])
        self.assertTrue(summary["hasToneCurve"])
        self.assertTrue(summary["hasColorMatrix1"])
        self.assertTrue(summary["hasForwardMatrix1"])
        self.assertEqual(summary["hueSatDims"], (6, 2, 1))
        self.assertEqual(summary["lookDims"], (4, 2, 1))

        unsupported = " ".join(summary["unsupported"]).lower()
        self.assertIn("dual-illuminant", unsupported)
        self.assertIn("forward-matrix", unsupported)
        self.assertIn("colour-matrix", unsupported)
        self.assertIn("look-table", unsupported)
        self.assertIn("approximate", summary["notes"].lower())

    def test_summary_falls_back_to_the_file_stem_for_an_unnamed_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_profile(
                Path(directory) / "Nameless Camera.dcp",
                name=None,
                tone_curve=[[0.0, 0.0], [1.0, 1.0]],
                illuminant1=21,
            )
            summary = camera_profile.profile_summary(path)
        self.assertEqual(summary["name"], "Nameless Camera")
        self.assertFalse(summary["hasHueSatMap1"])
        self.assertFalse(summary["dualIlluminant"])


class ListProfilesTests(unittest.TestCase):
    def _identity_profile(self, path: Path, name: str) -> Path:
        return write_profile(
            path, name=name,
            hue_sat_dims=(2, 2, 1),
            hue_sat_map=grid_bytes(2, 2, 1, lambda h, s, v: (0.0, 1.0, 1.0)),
        )

    def test_scans_one_level_down_and_reports_unreadable_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._identity_profile(root / "alpha.dcp", "Alpha")
            (root / "broken.dcp").write_bytes(b"nowhere near a TIFF")
            (root / "notes.txt").write_text("not a profile")
            nested = root / "Camera Pack"
            nested.mkdir()
            self._identity_profile(nested / "beta.dcp", "Beta")
            deeper = nested / "deeper"
            deeper.mkdir()
            self._identity_profile(deeper / "gamma.dcp", "Gamma")

            found = camera_profile.list_profiles(root)

        names = [entry["name"] for entry in found]
        self.assertIn("Alpha", names)
        self.assertIn("Beta", names)
        self.assertNotIn("Gamma", names)
        self.assertNotIn("notes.txt", [entry["file"] for entry in found])

        broken = [entry for entry in found if entry["file"] == "broken.dcp"]
        self.assertEqual(len(broken), 1)
        self.assertFalse(broken[0]["ok"])
        self.assertIn("broken.dcp", broken[0]["error"])
        self.assertIn("not a readable camera profile", broken[0]["error"])

        good = [entry for entry in found if entry.get("ok")]
        self.assertEqual(len(good), 2)
        self.assertTrue(all(entry["approximate"] for entry in good))

    def test_missing_folder_raises_profile_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(camera_profile.ProfileError):
                camera_profile.list_profiles(Path(directory) / "absent")

    def test_scan_is_capped(self):
        self.assertEqual(camera_profile.MAX_PROFILES, 500)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(3):
                self._identity_profile(
                    root / f"profile-{index}.dcp", f"Profile {index}")
            found = camera_profile.list_profiles(root)
        self.assertEqual(len(found), 3)
        self.assertLessEqual(len(found), camera_profile.MAX_PROFILES)


class HueSatMapTests(unittest.TestCase):
    def _flat_map(self, hue_divisions, sat_divisions, value_divisions,
                  triple):
        values = []
        for value_index in range(value_divisions):
            for hue_index in range(hue_divisions):
                for sat_index in range(sat_divisions):
                    values.extend(triple(hue_index, sat_index, value_index))
        return np.asarray(values, dtype=np.float32)

    def test_identity_map_leaves_pixels_unchanged(self):
        dims = (6, 3, 3)
        table = self._flat_map(*dims, lambda h, s, v: (0.0, 1.0, 1.0))
        result = camera_profile.apply_hue_sat_map(SAMPLE_PIXELS, table, dims)
        np.testing.assert_allclose(result, SAMPLE_PIXELS, atol=1e-6)

    def test_pure_hue_shift_rotates_hue_and_holds_saturation_and_value(self):
        dims = (4, 2, 2)
        shift = 40.0
        table = self._flat_map(*dims, lambda h, s, v: (shift, 1.0, 1.0))
        result = camera_profile.apply_hue_sat_map(SAMPLE_PIXELS, table, dims)

        for source, rendered in zip(SAMPLE_PIXELS[0], result[0]):
            source_hue, source_sat, source_value = hsv_of(source)
            hue, saturation, value = hsv_of(rendered)
            self.assertAlmostEqual(saturation, source_sat, delta=1e-5)
            self.assertAlmostEqual(value, source_value, delta=1e-5)
            if source_sat <= 0.0:
                continue
            expected = (source_hue + shift) % 360.0
            self.assertAlmostEqual(
                min(abs(hue - expected), 360.0 - abs(hue - expected)),
                0.0, delta=0.01)

    def test_saturation_scaling_touches_only_saturation(self):
        dims = (3, 2, 2)
        table = self._flat_map(*dims, lambda h, s, v: (0.0, 0.5, 1.0))
        result = camera_profile.apply_hue_sat_map(SAMPLE_PIXELS, table, dims)

        for source, rendered in zip(SAMPLE_PIXELS[0], result[0]):
            source_hue, source_sat, source_value = hsv_of(source)
            hue, saturation, value = hsv_of(rendered)
            self.assertAlmostEqual(saturation, source_sat * 0.5, delta=1e-5)
            self.assertAlmostEqual(value, source_value, delta=1e-5)
            if source_sat > 0.0:
                self.assertAlmostEqual(
                    min(abs(hue - source_hue), 360.0 - abs(hue - source_hue)),
                    0.0, delta=0.01)

    def test_hue_axis_wraps_and_blends_across_the_360_boundary(self):
        # Two hue slices: 0 degrees shifts by nothing, 180 degrees shifts by
        # 60. A pixel at 270 degrees sits halfway between the second slice and
        # the first one wrapped around, so it must land on 30, not jump.
        dims = (2, 2, 2)
        table = self._flat_map(
            *dims, lambda h, s, v: (0.0 if h == 0 else 60.0, 1.0, 1.0))
        hues = [0.0, 90.0, 180.0, 270.0, 350.0]
        # Slice 0 sits at 0 degrees and slice 1 at 180; the fraction between
        # them wraps, so 350 degrees is 350/180 - 1 = 0.944 of the way from
        # slice 1 back round to slice 0.
        expected_shift = [0.0, 30.0, 60.0, 30.0, 60.0 * (1.0 - 350.0 / 180.0
                                                         + 1.0)]
        pixels = np.asarray(
            [colorsys.hsv_to_rgb(hue / 360.0, 1.0, 1.0) for hue in hues],
            dtype=np.float32).reshape(1, len(hues), 3)

        result = camera_profile.apply_hue_sat_map(pixels, table, dims)

        for index, hue in enumerate(hues):
            rendered_hue, saturation, value = hsv_of(result[0, index])
            expected = (hue + expected_shift[index]) % 360.0
            self.assertAlmostEqual(
                min(abs(rendered_hue - expected),
                    360.0 - abs(rendered_hue - expected)),
                0.0, delta=0.02,
                msg=f"hue {hue} shifted to {rendered_hue}, wanted {expected}")
            self.assertAlmostEqual(saturation, 1.0, delta=1e-5)
            self.assertAlmostEqual(value, 1.0, delta=1e-5)

        # The wrapped shift must be strictly between the two slice values.
        wrapped_hue, _, _ = hsv_of(result[0, 3])
        self.assertGreater((wrapped_hue - 270.0) % 360.0, 1.0)
        self.assertLess((wrapped_hue - 270.0) % 360.0, 59.0)

    def test_look_table_uses_the_same_grid_maths(self):
        dims = (3, 2, 2)
        table = self._flat_map(*dims, lambda h, s, v: (12.0, 0.9, 1.0))
        pixels = SAMPLE_PIXELS
        np.testing.assert_allclose(
            camera_profile.apply_look_table(pixels, table, dims),
            camera_profile.apply_hue_sat_map(pixels, table, dims),
            atol=0.0)

    def test_mismatched_dimensions_raise_profile_error(self):
        dims = (4, 2, 1)
        table = np.zeros(3 * 3, dtype=np.float32)
        with self.assertRaises(camera_profile.ProfileError) as caught:
            camera_profile.apply_hue_sat_map(SAMPLE_PIXELS, table, dims)
        self.assertIn("dimensions", str(caught.exception))

    def test_integer_input_is_scaled_rather_than_clipped_to_white(self):
        dims = (2, 2, 1)
        table = self._flat_map(*dims, lambda h, s, v: (0.0, 1.0, 1.0))
        eight_bit = np.asarray(
            [[[255, 128, 0], [0, 0, 0]]], dtype=np.uint8)
        result = camera_profile.apply_hue_sat_map(eight_bit, table, dims)
        np.testing.assert_allclose(
            result, eight_bit.astype(np.float32) / 255.0, atol=1e-6)

    def test_non_rgb_input_raises_profile_error(self):
        dims = (2, 2, 1)
        table = self._flat_map(*dims, lambda h, s, v: (0.0, 1.0, 1.0))
        with self.assertRaises(camera_profile.ProfileError):
            camera_profile.apply_hue_sat_map(
                np.zeros((4, 4), dtype=np.float32), table, dims)


class ToneCurveTests(unittest.TestCase):
    CURVE = [0.0, 0.0, 0.25, 0.1, 0.5, 0.45, 0.75, 0.8, 1.0, 1.0]

    def test_monotonic_curve_stays_monotonic_and_pins_the_endpoints(self):
        ramp = np.linspace(0.0, 1.0, 64, dtype=np.float32)
        image = np.repeat(ramp[:, None], 3, axis=1).reshape(1, 64, 3)
        result = camera_profile.apply_tone_curve(image, self.CURVE)

        channel = result[0, :, 0]
        self.assertAlmostEqual(float(channel[0]), 0.0, delta=1e-6)
        self.assertAlmostEqual(float(channel[-1]), 1.0, delta=1e-6)
        self.assertTrue(np.all(np.diff(channel) >= -1e-7))
        np.testing.assert_allclose(result[0, :, 1], channel, atol=1e-6)
        np.testing.assert_allclose(result[0, :, 2], channel, atol=1e-6)

        midpoint = camera_profile.apply_tone_curve(
            np.full((1, 1, 3), 0.5, dtype=np.float32), self.CURVE)
        np.testing.assert_allclose(midpoint, 0.45, atol=1e-6)

    def test_identity_curve_is_a_no_op(self):
        result = camera_profile.apply_tone_curve(
            SAMPLE_PIXELS, [0.0, 0.0, 1.0, 1.0])
        np.testing.assert_allclose(result, SAMPLE_PIXELS, atol=1e-6)

    def test_curve_accepts_pairs_and_holds_hue(self):
        pixel = np.asarray([[[0.8, 0.4, 0.2]]], dtype=np.float32)
        pairs = np.asarray(self.CURVE, dtype=np.float32).reshape(-1, 2)
        result = camera_profile.apply_tone_curve(pixel, pairs)
        source_hue = hsv_of(pixel[0, 0])[0]
        rendered_hue = hsv_of(result[0, 0])[0]
        self.assertAlmostEqual(rendered_hue, source_hue, delta=0.05)
        self.assertGreater(float(result[0, 0, 0]), float(pixel[0, 0, 0]))

    def test_odd_length_curve_raises_profile_error(self):
        with self.assertRaises(camera_profile.ProfileError):
            camera_profile.apply_tone_curve(SAMPLE_PIXELS, [0.0, 0.0, 1.0])


class ApplyProfileTests(unittest.TestCase):
    def _profile(self, directory: Path) -> dict:
        path = write_profile(
            directory / "look.dcp",
            name="Strength Test",
            hue_sat_dims=(4, 2, 2),
            hue_sat_map=grid_bytes(4, 2, 2, lambda h, s, v: (25.0, 0.8, 1.0)),
            look_dims=(2, 2, 1),
            look_table=grid_bytes(2, 2, 1, lambda h, s, v: (0.0, 1.1, 0.95)),
            tone_curve=[[0.0, 0.0], [0.5, 0.6], [1.0, 1.0]],
            illuminant1=17,
        )
        return camera_profile.read_profile(path)

    def test_strength_zero_returns_the_input_and_half_lands_halfway(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = self._profile(Path(directory))

        untouched = camera_profile.apply_profile(
            SAMPLE_PIXELS, profile, strength=0.0)
        np.testing.assert_allclose(untouched, SAMPLE_PIXELS, atol=0.0)

        full = camera_profile.apply_profile(SAMPLE_PIXELS, profile)
        self.assertGreater(
            float(np.abs(full - SAMPLE_PIXELS).max()), 0.01)

        half = camera_profile.apply_profile(
            SAMPLE_PIXELS, profile, strength=0.5)
        np.testing.assert_allclose(
            half, SAMPLE_PIXELS + (full - SAMPLE_PIXELS) * 0.5, atol=1e-6)

    def test_apply_profile_composes_map_then_look_then_tone(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = self._profile(Path(directory))
        staged = camera_profile.apply_hue_sat_map(
            SAMPLE_PIXELS, profile["hueSatMap1"], profile["hueSatDims"])
        staged = camera_profile.apply_look_table(
            staged, profile["lookTable"], profile["lookDims"])
        staged = camera_profile.apply_tone_curve(staged, profile["toneCurve"])
        np.testing.assert_allclose(
            camera_profile.apply_profile(SAMPLE_PIXELS, profile),
            staged, atol=0.0)

    def test_apply_profile_accepts_a_path_and_an_identity_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_profile(
                Path(directory) / "identity.dcp",
                name="Identity",
                hue_sat_dims=(6, 3, 3),
                hue_sat_map=grid_bytes(
                    6, 3, 3, lambda h, s, v: (0.0, 1.0, 1.0)),
                tone_curve=[[0.0, 0.0], [1.0, 1.0]],
            )
            result = camera_profile.apply_profile(SAMPLE_PIXELS, path)
        np.testing.assert_allclose(result, SAMPLE_PIXELS, atol=1e-6)

    def test_profile_without_tables_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_profile(
                Path(directory) / "matrix-only.dcp",
                name="Matrix Only",
                illuminant1=21,
                colour_matrix=[0.8, -0.2, -0.05, -0.3, 1.2,
                               0.1, 0.02, -0.15, 0.7],
            )
            profile = camera_profile.read_profile(path)
        np.testing.assert_allclose(
            camera_profile.apply_profile(SAMPLE_PIXELS, profile),
            SAMPLE_PIXELS, atol=0.0)

    def test_rejects_a_profile_that_is_not_a_dict_or_path(self):
        with self.assertRaises(camera_profile.ProfileError):
            camera_profile.apply_profile(SAMPLE_PIXELS, 42)


if __name__ == "__main__":
    unittest.main()
