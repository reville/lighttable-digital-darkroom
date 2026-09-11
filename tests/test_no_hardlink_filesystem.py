# SPDX-License-Identifier: GPL-3.0-only
"""Exercise native no-replace operations when a volume rejects hard links."""
import errno
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import durable_io


class NoHardlinkFilesystemTests(unittest.TestCase):
    def test_publish_copy_and_move_on_volumes_without_hard_links(self):
        for operation in (durable_io.publish_file_no_replace,
                          durable_io.copy_file_no_replace,
                          durable_io.move_file_no_replace):
            with self.subTest(operation=operation.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, target = root / "original.jpg", root / "published.jpg"
                source.write_bytes(b"complete photo bytes")
                with mock.patch.object(durable_io.os, "link",
                        side_effect=OSError(errno.ENOTSUP, "hard links unsupported")):
                    operation(source, target)
                self.assertEqual(target.read_bytes(), b"complete photo bytes")
                self.assertEqual(source.exists(), operation is durable_io.copy_file_no_replace)
                self.assertFalse(list(root.glob(".*")))

    def test_existing_destination_survives_native_fallback(self):
        for operation in (durable_io.publish_file_no_replace,
                          durable_io.copy_file_no_replace,
                          durable_io.move_file_no_replace):
            with self.subTest(operation=operation.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, target = root / "original.jpg", root / "published.jpg"
                source.write_bytes(b"new photo")
                target.write_bytes(b"other photo")
                with mock.patch.object(durable_io.os, "link",
                        side_effect=OSError(errno.ENOTSUP, "hard links unsupported")):
                    with self.assertRaises(FileExistsError):
                        operation(source, target)
                self.assertEqual(source.read_bytes(), b"new photo")
                self.assertEqual(target.read_bytes(), b"other photo")
                self.assertFalse(list(root.glob(".*")))

    def test_raced_destination_survives_after_hard_link_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "original.jpg", root / "published.jpg"
            source.write_bytes(b"new photo")
            def raced(*_):
                target.write_bytes(b"arrived during publication")
                raise OSError(errno.ENOTSUP, "hard links unsupported")
            with mock.patch.object(durable_io.os, "link", side_effect=raced):
                with self.assertRaises(FileExistsError):
                    durable_io.publish_file_no_replace(source, target)
            self.assertEqual(source.read_bytes(), b"new photo")
            self.assertEqual(target.read_bytes(), b"arrived during publication")

    def test_backup_manifest_creation_on_volume_without_hard_links(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "recipe.json"
            with mock.patch.object(durable_io.os, "link",
                    side_effect=OSError(errno.EPERM, "operation unsupported")):
                durable_io.atomic_create_json(target, {"complete": True})
            self.assertEqual(durable_io.load_json(target, None), {"complete": True})
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_disk_errors_do_not_attempt_a_rename_fallback(self):
        for number in (errno.EEXIST, errno.ENOSPC, errno.EROFS, errno.EXDEV, errno.EIO):
            with self.subTest(errno=number), tempfile.TemporaryDirectory() as directory:
                source, target = Path(directory) / "source", Path(directory) / "target"
                source.write_bytes(b"complete")
                with mock.patch.object(durable_io.os, "link", side_effect=OSError(number, "failure")), \
                        mock.patch.object(durable_io, "_rename_no_replace") as fallback:
                    with self.assertRaises(OSError) as result:
                        durable_io.publish_file_no_replace(source, target)
                    self.assertEqual(result.exception.errno, number)
                    fallback.assert_not_called()
                self.assertEqual(source.read_bytes(), b"complete")
                self.assertFalse(target.exists())
