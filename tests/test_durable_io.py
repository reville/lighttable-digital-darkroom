import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import durable_io


class AtomicWriteTests(unittest.TestCase):
    def test_failed_publish_keeps_the_previous_file_and_cleans_the_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            target.write_text('{"rating": 4}')

            with mock.patch.object(durable_io.os, "replace",
                                   side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    durable_io.atomic_write_json(target, {"rating": 5})

            self.assertEqual(json.loads(target.read_text()), {"rating": 4})
            self.assertEqual(list(Path(directory).glob(".*.tmp.*")), [])

    def test_json_keeps_a_last_valid_backup_and_recovers_from_damage(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            durable_io.atomic_write_json(target, {"rating": 4})
            durable_io.atomic_write_json(target, {"rating": 5})
            target.write_text("{interrupted")

            self.assertEqual(durable_io.load_json(target, {}), {"rating": 4})

            # Saving after damage must not replace the valid backup with the
            # damaged primary.
            durable_io.atomic_write_json(target, {"rating": 6})
            self.assertEqual(
                json.loads(durable_io.backup_path(target).read_text()),
                {"rating": 4},
            )

    def test_backup_once_retains_the_pre_app_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "frame.xmp"
            target.write_text("external metadata")

            durable_io.atomic_write_text(
                target, "first app state", keep_backup=True, backup_once=True)
            durable_io.atomic_write_text(
                target, "second app state", keep_backup=True, backup_once=True)

            self.assertEqual(target.read_text(), "second app state")
            self.assertEqual(
                durable_io.backup_path(target).read_text(), "external metadata")


class PublishTests(unittest.TestCase):
    def test_a_failed_publish_never_removes_the_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "delivery.jpg"
            staged = root / ".delivery.part.jpg"
            target.write_bytes(b"previous good export")
            staged.write_bytes(b"new complete export")

            with mock.patch.object(durable_io.os, "replace",
                                   side_effect=OSError("volume disappeared")):
                with self.assertRaises(OSError):
                    durable_io.publish_file(staged, target)

            self.assertEqual(target.read_bytes(), b"previous good export")
            self.assertEqual(staged.read_bytes(), b"new complete export")

    def test_copy_no_replace_refuses_a_raced_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.xmp"
            target = root / "target.xmp"
            source.write_bytes(b"source metadata")
            target.write_bytes(b"other metadata")

            with self.assertRaises(FileExistsError):
                durable_io.copy_file_no_replace(source, target)

            self.assertEqual(target.read_bytes(), b"other metadata")
            self.assertEqual(source.read_bytes(), b"source metadata")

    def test_move_no_replace_refuses_a_raced_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            target = root / "target.jpg"
            source.write_bytes(b"source photo")
            target.write_bytes(b"other photo")

            with self.assertRaises(FileExistsError):
                durable_io.move_file_no_replace(source, target)

            self.assertEqual(target.read_bytes(), b"other photo")
            self.assertEqual(source.read_bytes(), b"source photo")

    def test_atomic_json_create_never_overwrites_an_existing_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "output.tif.lighttable.json"
            target.write_text('{"owner": "other"}')

            with self.assertRaises(FileExistsError):
                durable_io.atomic_create_json(target, {"owner": "lighttable"})

            self.assertEqual(json.loads(target.read_text()), {"owner": "other"})
            self.assertEqual(list(Path(directory).glob(".*.create.*")), [])


if __name__ == "__main__":
    unittest.main()
