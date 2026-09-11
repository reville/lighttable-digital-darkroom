# SPDX-License-Identifier: GPL-3.0-only
"""Tests for the Enhance plumbing that surrounds a model that is not present.

No model, helper binary, or fixture file exists in this repository, so every
test here drives the path with an injected stub runner or asserts that the
unavailable path is reported honestly. Nothing touches the network.
"""
import json
import struct
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import tifffile

import color_pipeline
import enhance_workflow as enhance


def _noise(height, width, seed=11):
    rng = np.random.default_rng(seed)
    return rng.random((height, width, 3), dtype=np.float32)


def _identity_runner(tile, mode, params):
    return tile


def _nearest_upscale_runner(tile, mode, params):
    factor = int(params["scale"])
    return np.repeat(np.repeat(tile, factor, axis=0), factor, axis=1)


def _empty_model_environment(folder):
    """Point discovery at an empty folder and a helper that cannot exist."""
    return mock.patch.dict(os.environ, {
        enhance.MODEL_DIR_ENV: str(folder),
        enhance.HELPER_ENV: str(Path(folder) / "no-such-LightTableEnhance"),
    })


class TilingTests(unittest.TestCase):
    def test_identity_round_trip_is_exact_at_every_size(self):
        # Sizes that divide evenly by the tile, that do not, and one smaller
        # than a single tile. An identity model must reassemble bit-for-bit
        # to float32 rounding or a real model would inherit the error.
        for height, width in ((100, 100), (512, 512), (1000, 700), (64, 64)):
            with self.subTest(size=(height, width)):
                image = _noise(height, width)
                tiles = enhance.tile_image(image)
                merged = enhance.merge_tiles(tiles, image.shape)
                self.assertEqual(merged.shape, image.shape)
                self.assertLess(float(np.max(np.abs(merged - image))), 1e-6)

    def test_tiles_cover_every_pixel_and_respect_the_tile_size(self):
        image = _noise(1000, 700)
        tiles = enhance.tile_image(image, tile=256, overlap=32)
        covered = np.zeros(image.shape[:2], dtype=bool)
        for tile in tiles:
            self.assertLessEqual(tile["y1"] - tile["y0"], 256)
            self.assertLessEqual(tile["x1"] - tile["x0"], 256)
            self.assertEqual(tile["image"].shape,
                             (tile["y1"] - tile["y0"],
                              tile["x1"] - tile["x0"], 3))
            covered[tile["y0"]:tile["y1"], tile["x0"]:tile["x1"]] = True
        self.assertTrue(covered.all())

    def test_small_image_makes_one_tile(self):
        tiles = enhance.tile_image(_noise(64, 64))
        self.assertEqual(len(tiles), 1)
        self.assertEqual(tiles[0]["image"].shape, (64, 64, 3))

    def test_feathering_leaves_no_seam_at_tile_boundaries(self):
        # Every tile is offset by a different constant, the worst case for a
        # model whose output drifts tile to tile. The raised-cosine feather
        # must spread the largest difference across the overlap, so no step
        # exceeds difference * pi / (2 * overlap) -- bounded here by the
        # looser difference * 2 / overlap.
        flat = np.full((1000, 700, 3), 0.5, dtype=np.float32)
        offsets = np.linspace(-0.2, 0.2, 24)
        tiles = enhance.tile_image(flat)
        for index, tile in enumerate(tiles):
            tile["image"] = tile["image"] + float(offsets[index % offsets.size])
        merged = enhance.merge_tiles(tiles, flat.shape)
        difference = float(offsets.max() - offsets.min())
        bound = difference * 2.0 / enhance.DEFAULT_OVERLAP
        horizontal = float(np.max(np.abs(np.diff(merged, axis=1))))
        vertical = float(np.max(np.abs(np.diff(merged, axis=0))))
        self.assertLess(horizontal, bound)
        self.assertLess(vertical, bound)
        # An unfeathered join would step by the whole difference.
        self.assertLess(max(horizontal, vertical), difference * 0.1)

    def test_merge_rejects_mixed_scales_and_wrong_tile_sizes(self):
        image = _noise(300, 300)
        tiles = enhance.tile_image(image, tile=128, overlap=32)
        tiles[0]["scale"] = 2
        with self.assertRaises(ValueError):
            enhance.merge_tiles(tiles, image.shape)
        broken = enhance.tile_image(image, tile=128, overlap=32)
        broken[0]["image"] = broken[0]["image"][:-1]
        with self.assertRaises(ValueError):
            enhance.merge_tiles(broken, image.shape)


class UpscaleTests(unittest.TestCase):
    def test_two_times_upscale_reassembles_to_the_scaled_shape(self):
        image = _noise(300, 220, seed=3)
        result = enhance.run_model(image, "upscale", scale=2, tile=128,
                                   overlap=32, runner=_nearest_upscale_runner)
        self.assertEqual(result.shape, (600, 440, 3))
        reference = np.repeat(np.repeat(image, 2, axis=0), 2, axis=1)
        self.assertLess(float(np.max(np.abs(result - reference))), 1e-6)

    def test_upscale_of_an_image_smaller_than_one_tile(self):
        image = _noise(70, 50, seed=5)
        result = enhance.run_model(image, "upscale", scale=4,
                                   runner=_nearest_upscale_runner)
        self.assertEqual(result.shape, (280, 200, 3))

    def test_denoise_never_rescales(self):
        image = _noise(600, 400, seed=9)
        result = enhance.run_model(image, "denoise", scale=4,
                                   runner=_identity_runner)
        self.assertEqual(result.shape, image.shape)

    def test_runner_returning_the_wrong_size_is_rejected(self):
        with self.assertRaises(ValueError):
            enhance.run_model(_noise(200, 200), "upscale", scale=2,
                              runner=_identity_runner)


class RequestTests(unittest.TestCase):
    def test_clean_request_clamps_strength_scale_and_tile(self):
        clamped = enhance.clean_request({
            "mode": "upscale", "strength": 9.5, "scale": 99,
            "tile": 100000, "overlap": 100000})
        self.assertEqual(clamped["mode"], "upscale")
        self.assertEqual(clamped["strength"], 1.0)
        self.assertEqual(clamped["scale"], 4)
        self.assertEqual(clamped["tile"], enhance.MAX_TILE)
        self.assertEqual(clamped["overlap"], enhance.MAX_TILE // 2)

        floored = enhance.clean_request({
            "mode": "nonsense", "strength": -3.0, "scale": 0,
            "tile": 1, "overlap": -50})
        self.assertEqual(floored["mode"], "denoise")
        self.assertEqual(floored["strength"], 0.0)
        self.assertEqual(floored["scale"], 1)
        self.assertEqual(floored["tile"], enhance.MIN_TILE)
        self.assertEqual(floored["overlap"], enhance.MIN_OVERLAP)

        # Unsupported factors step down, junk falls back, overlap stays
        # under half a tile so the two feathers cannot collide.
        self.assertEqual(enhance.clean_request({"scale": 3})["scale"], 2)
        self.assertEqual(enhance.clean_request({"strength": "loud"})["strength"], 1.0)
        self.assertEqual(
            enhance.clean_request({"strength": float("nan")})["strength"], 1.0)
        narrow = enhance.clean_request({"tile": 64})
        self.assertLessEqual(narrow["overlap"], narrow["tile"] // 2)
        self.assertEqual(enhance.clean_request(None)["tile"], enhance.DEFAULT_TILE)


class CapabilityTests(unittest.TestCase):
    def test_capabilities_reports_a_reason_when_nothing_is_installed(self):
        with tempfile.TemporaryDirectory() as folder:
            with _empty_model_environment(folder):
                report = enhance.capabilities()
        self.assertFalse(report["available"])
        self.assertTrue(report["reason"].strip())
        self.assertFalse(report["bundledModel"])
        self.assertFalse(any(report["modes"].values()))
        for mode in enhance.MODES:
            self.assertFalse(report["models"][mode]["installed"])
            self.assertFalse(report["models"][mode]["bundled"])

    def test_model_root_follows_the_environment_override(self):
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.dict(os.environ, {enhance.MODEL_DIR_ENV: folder}):
                self.assertEqual(enhance.model_root(), Path(folder))
                state = enhance.available_models()
        self.assertEqual(set(state), {"denoise", "upscale", "helper"})
        self.assertFalse(state["denoise"])
        self.assertFalse(state["upscale"])

    def test_an_installed_model_is_detected_and_identified(self):
        # A placeholder file, not a model: this proves discovery works, it
        # does not make inference possible.
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / enhance.MODEL_FILES["denoise"]).write_text("x")
            (Path(folder) / enhance.MODEL_INDEX_FILE).write_text(
                json.dumps({"denoise": {"version": "1.2", "license": "MIT"}}))
            with _empty_model_environment(folder):
                self.assertTrue(enhance.available_models()["denoise"])
                info = enhance.model_info("denoise")
                report = enhance.capabilities()
        self.assertTrue(info["installed"])
        self.assertEqual(info["version"], "1.2")
        self.assertNotEqual(info["identity"], "none")
        # The helper is still missing, so Enhance stays off with a reason.
        self.assertFalse(report["available"])
        self.assertIn("helper", report["reason"].lower())


class UnavailableTests(unittest.TestCase):
    def test_run_model_without_a_model_or_runner_refuses_to_run(self):
        image = _noise(120, 120)
        with tempfile.TemporaryDirectory() as folder:
            with _empty_model_environment(folder):
                with self.assertRaises(enhance.EnhanceUnavailable) as caught:
                    enhance.run_model(image, "denoise")
        self.assertTrue(str(caught.exception).strip())

    def test_helper_runner_reports_the_missing_helper(self):
        with tempfile.TemporaryDirectory() as folder:
            with _empty_model_environment(folder):
                with self.assertRaises(enhance.EnhanceUnavailable):
                    enhance.helper_runner(_noise(16, 16), "denoise",
                                          {"strength": 1.0, "scale": 1})

    def test_enhance_file_returns_the_reason_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.tif"
            color_pipeline.save_export_image(_noise(80, 60), source, fmt="tif")
            destination = Path(folder) / enhance.ENHANCED_FOLDER_NAME / "out.tif"
            with _empty_model_environment(folder):
                result = enhance.enhance_file(source, destination, "denoise")
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])
        self.assertFalse(destination.exists())

    def test_enhance_file_reports_a_missing_source(self):
        with tempfile.TemporaryDirectory() as folder:
            result = enhance.enhance_file(Path(folder) / "gone.tif",
                                          Path(folder) / "out.tif", "denoise",
                                          runner=_identity_runner)
        self.assertFalse(result["ok"])
        self.assertIn("gone.tif", result["error"])

    def test_unknown_modes_are_rejected(self):
        with self.assertRaises(ValueError):
            enhance.run_model(_noise(32, 32), "sharpen", runner=_identity_runner)
        result = enhance.enhance_file("a.tif", "b.tif", "sharpen",
                                      runner=_identity_runner)
        self.assertFalse(result["ok"])


class EnhanceFileTests(unittest.TestCase):
    def test_writes_a_16_bit_master_and_a_manifest_naming_the_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "frame 001.tif"
            image = _noise(300, 200, seed=21)
            color_pipeline.save_export_image(image, source, fmt="tif")
            destination = enhance.enhanced_destination(folder, "frame 001", "denoise")
            self.assertEqual(destination.parent.name,
                             enhance.ENHANCED_FOLDER_NAME)

            result = enhance.enhance_file(source, destination, "denoise",
                                          request={"strength": 0.6},
                                          runner=_identity_runner)

            self.assertTrue(result["ok"], result["error"])
            self.assertIsNone(result["error"])
            self.assertTrue(destination.is_file())

            written = tifffile.imread(destination)
            self.assertEqual(written.dtype, np.uint16)
            self.assertEqual(written.shape, (300, 200, 3))
            self.assertEqual((result["width"], result["height"]), (200, 300))

            sidecar = Path(result["manifest"])
            self.assertEqual(sidecar.name, destination.name + ".lighttable.json")
            manifest = json.loads(sidecar.read_text())

        self.assertEqual(manifest["kind"], "enhance")
        self.assertEqual(manifest["mode"], "denoise")
        self.assertEqual(manifest["sourceName"], "frame 001.tif")
        self.assertIn("frame 001.tif", manifest["source"])
        self.assertEqual(len(manifest["sourceSha256"]), 64)
        self.assertEqual(manifest["outputSpace"], "prophoto")
        self.assertEqual(manifest["bitDepth"], 16)
        self.assertEqual(manifest["parameters"]["strength"], 0.6)
        self.assertEqual(manifest["tiling"]["feather"], "raised-cosine")
        # Honesty: a stub ran, no model was installed, nothing was measured.
        self.assertEqual(manifest["runner"], "injected")
        self.assertTrue(manifest["modeled"])
        self.assertFalse(manifest["measured"])
        self.assertFalse(manifest["model"]["bundled"])
        self.assertTrue(manifest["disclosure"].strip())
        self.assertTrue(manifest["created"].endswith("Z"))

    def test_destination_names_are_safe_and_do_not_collide(self):
        with tempfile.TemporaryDirectory() as folder:
            first = enhance.enhanced_destination(folder, "trip/final", "upscale")
            self.assertEqual(first.name, "trip_final.tif")
            first.parent.mkdir(parents=True, exist_ok=True)
            first.touch()
            second = enhance.enhanced_destination(folder, "trip/final", "upscale")
            self.assertEqual(second.name, "trip_final-2.tif")

    def test_an_orphan_manifest_also_reserves_the_enhanced_name(self):
        with tempfile.TemporaryDirectory() as folder:
            expected = (Path(folder) / enhance.ENHANCED_FOLDER_NAME
                        / "frame.tif.lighttable.json")
            expected.parent.mkdir(parents=True)
            expected.write_text("preserve")

            destination = enhance.enhanced_destination(
                folder, "frame", "denoise")

            self.assertEqual(destination.name, "frame-2.tif")

    def test_upscaled_master_is_written_at_the_scaled_size(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "small.tif"
            color_pipeline.save_export_image(_noise(120, 90, seed=4), source,
                                             fmt="tif")
            destination = Path(folder) / "out.tif"
            result = enhance.enhance_file(source, destination, "upscale",
                                          request={"scale": 2, "tile": 64,
                                                   "overlap": 16},
                                          runner=_nearest_upscale_runner)
            self.assertTrue(result["ok"], result["error"])
            self.assertEqual(tifffile.imread(destination).shape, (240, 180, 3))

    def test_a_raced_destination_is_preserved_instead_of_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.tif"
            color_pipeline.save_export_image(_noise(32, 24), source, fmt="tif")
            destination = Path(folder) / "out.tif"
            destination.write_bytes(b"other completed output")

            result = enhance.enhance_file(
                source, destination, "denoise", runner=_identity_runner)

            self.assertFalse(result["ok"])
            self.assertEqual(destination.read_bytes(), b"other completed output")
            self.assertEqual(list(Path(folder).glob(".*.enhance.*")), [])


class FingerprintTests(unittest.TestCase):
    def test_denoise_fingerprint_separates_settings(self):
        off = enhance.denoise_fingerprint({})
        self.assertEqual(off, enhance.denoise_fingerprint(
            {"learnedDenoise": False}))
        on = enhance.denoise_fingerprint(
            {"learnedDenoise": True, "learnedDenoiseStrength": 0.5})
        stronger = enhance.denoise_fingerprint(
            {"learnedDenoise": True, "learnedDenoiseStrength": 0.9})
        self.assertNotEqual(off, on)
        self.assertNotEqual(on, stronger)
        self.assertTrue(off.startswith(enhance.DENOISE_FINGERPRINT_VERSION))
        self.assertTrue(on.startswith(enhance.DENOISE_FINGERPRINT_VERSION))

    def test_denoise_fingerprint_follows_the_installed_model(self):
        with tempfile.TemporaryDirectory() as folder:
            with _empty_model_environment(folder):
                missing = enhance.denoise_fingerprint({"learnedDenoise": True})
                (Path(folder) / enhance.MODEL_FILES["denoise"]).write_text("x")
                installed = enhance.denoise_fingerprint({"learnedDenoise": True})
        self.assertIn("none", missing)
        self.assertNotEqual(missing, installed)

    def test_denoise_fingerprint_is_stable_and_clamped(self):
        params = {"learnedDenoise": True, "learnedDenoiseStrength": 4.0}
        self.assertEqual(enhance.denoise_fingerprint(params),
                         enhance.denoise_fingerprint(
                             {"learnedDenoise": True,
                              "learnedDenoiseStrength": 1.0}))


class TileExchangeTests(unittest.TestCase):
    """The helper exchange must not colour-manage working pixels.

    An earlier version passed tiles as 16-bit TIFF. Core Image colour-manages
    anything it recognises, so the model was handed values that had been
    through an sRGB-to-linear conversion nobody asked for, measured as a 17 dB
    loss against a known-clean reference. The format carries no colour
    information for exactly that reason.
    """

    def test_round_trip_is_exact(self):
        rng = np.random.default_rng(3)
        tile = rng.random((64, 48, 3), dtype=np.float32)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.tile"
            enhance.write_tile_file(path, tile)
            back = enhance.read_tile_file(path)
        self.assertEqual(back.shape, tile.shape)
        np.testing.assert_array_equal(back, tile)

    def test_header_records_the_real_dimensions(self):
        tile = np.zeros((7, 11, 3), dtype=np.float32)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.tile"
            enhance.write_tile_file(path, tile)
            raw = path.read_bytes()
        self.assertEqual(raw[:4], enhance.TILE_MAGIC)
        width, height, channels = struct.unpack("<iii", raw[4:16])
        self.assertEqual((width, height, channels), (11, 7, 3))

    def test_extreme_values_survive(self):
        tile = np.array([[[0.0, 1.0, 0.5]]], dtype=np.float32)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.tile"
            enhance.write_tile_file(path, tile)
            back = enhance.read_tile_file(path)
        np.testing.assert_array_equal(back, tile)

    def test_a_foreign_file_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.tile"
            path.write_bytes(b"not a tile at all, really")
            with self.assertRaises(RuntimeError):
                enhance.read_tile_file(path)

    def test_a_truncated_tile_is_refused(self):
        tile = np.zeros((32, 32, 3), dtype=np.float32)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.tile"
            enhance.write_tile_file(path, tile)
            data = path.read_bytes()
            path.write_bytes(data[:len(data) // 2])
            with self.assertRaises(RuntimeError):
                enhance.read_tile_file(path)


class BatchRunnerTests(unittest.TestCase):
    def test_low_frequency_colour_is_restored_without_changing_identity(self):
        y, x = np.mgrid[:96, :128].astype(np.float32)
        source = np.stack((0.2 + x / 300, 0.3 + y / 400,
                           0.4 + (x + y) / 900), axis=2)
        shifted = np.clip(source + np.array([0.04, -0.03, 0.02]), 0, 1)
        corrected = enhance.preserve_low_frequency_color(source, shifted)
        self.assertLess(float(np.mean(np.abs(corrected - source))), 1e-5)
        np.testing.assert_array_equal(
            enhance.preserve_low_frequency_color(source, source), source)

    def test_short_edge_tiles_are_padded_and_cropped_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models = root / "models"
            models.mkdir()
            (models / "denoise.mlpackage").mkdir()
            helper = root / "helper"
            helper.write_text("#!/bin/sh\ncp \"$2\" \"$3\"\n")
            helper.chmod(0o755)
            image = _noise(73, 91)
            with mock.patch.dict(os.environ, {
                    enhance.MODEL_DIR_ENV: str(models),
                    enhance.HELPER_ENV: str(helper)}):
                result = enhance.helper_runner(
                    image, "denoise", {"tile": 512, "strength": 1.0})
        self.assertEqual(result.shape, image.shape)
        np.testing.assert_array_equal(result, image)

    def test_strength_zero_is_byte_identical_and_skips_runner(self):
        image = _noise(83, 79)
        runner = mock.Mock(side_effect=lambda tile, *_: tile * 0)
        result = enhance.run_model(image, "denoise", strength=0,
                                   runner=runner)
        self.assertEqual(result.tobytes(), image.tobytes())
        runner.assert_not_called()

    def test_batch_is_unavailable_without_a_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {
                    enhance.MODEL_DIR_ENV: directory,
                    enhance.HELPER_ENV: str(Path(directory) / "nope")}):
                with self.assertRaises(enhance.EnhanceUnavailable):
                    enhance.helper_batch_runner(
                        [np.zeros((8, 8, 3), np.float32)], "denoise", {})

    def test_batch_refuses_upscale(self):
        """Only denoise batches today; saying so beats a confusing failure."""
        with tempfile.TemporaryDirectory() as directory:
            models = Path(directory) / "models"
            models.mkdir()
            (models / "upscale.mlpackage").mkdir()
            helper = Path(directory) / "helper"
            helper.write_text("#!/bin/sh\nexit 0\n")
            helper.chmod(0o755)
            with mock.patch.dict(os.environ, {
                    enhance.MODEL_DIR_ENV: str(models),
                    enhance.HELPER_ENV: str(helper)}):
                with self.assertRaises(enhance.EnhanceUnavailable):
                    enhance.helper_batch_runner(
                        [np.zeros((8, 8, 3), np.float32)], "upscale", {})

    def test_empty_batch_is_a_no_op(self):
        self.assertEqual(enhance.helper_batch_runner([], "denoise", {}),
                         [])

    def test_run_model_still_honours_an_injected_runner(self):
        """An injected runner must not be replaced by the batch path."""
        calls = []

        def runner(tile, mode, params):
            calls.append(mode)
            return tile * 0.5

        image = np.full((600, 600, 3), 0.8, dtype=np.float32)
        out = enhance.run_model(image, "denoise", runner=runner)
        self.assertTrue(calls)
        self.assertEqual(out.shape, image.shape)
        np.testing.assert_allclose(out, image * 0.5, atol=1e-5)

    def test_batch_drains_large_helper_diagnostics_without_deadlocking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models = root / "models"
            models.mkdir()
            (models / "denoise.mlpackage").mkdir()
            helper = root / "helper"
            helper.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib, shutil, sys\n"
                "sys.stderr.write('diagnostic ' * 20000)\n"
                "sys.stderr.flush()\n"
                "pairs = pathlib.Path(sys.argv[2]).read_text().splitlines()\n"
                "for pair in pairs:\n"
                "    source, target = pair.split('\\t', 1)\n"
                "    shutil.copyfile(source, target)\n"
                "print('{\"progress\":1,\"total\":1}')\n")
            helper.chmod(0o755)
            image = _noise(32, 48)
            status = {}
            with mock.patch.dict(os.environ, {
                    enhance.MODEL_DIR_ENV: str(models),
                    enhance.HELPER_ENV: str(helper)}):
                result = enhance.helper_batch_runner(
                    [image], "denoise", {"tile": 64, "strength": 1.0},
                    status=status)
        np.testing.assert_array_equal(result[0], image)
        self.assertEqual(status, {"progress": 1, "total": 1})

    def test_batch_cancel_stops_the_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models = root / "models"
            models.mkdir()
            (models / "denoise.mlpackage").mkdir()
            helper = root / "helper"
            helper.write_text(
                "#!/usr/bin/env python3\n"
                "import time\n"
                "time.sleep(10)\n")
            helper.chmod(0o755)
            with mock.patch.dict(os.environ, {
                    enhance.MODEL_DIR_ENV: str(models),
                    enhance.HELPER_ENV: str(helper)}):
                with self.assertRaisesRegex(RuntimeError, "cancelled"):
                    enhance.helper_batch_runner(
                        [_noise(16, 16)], "denoise",
                        {"tile": 64, "strength": 1.0}, cancel=lambda: True)


if __name__ == "__main__":
    unittest.main()
