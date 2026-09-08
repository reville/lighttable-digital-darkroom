"""Windows metadata contract simulations; these are not native Win32 proof."""
from contextlib import ExitStack
import ctypes
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import catalog_scan
import file_identity
import server
from tests import test_bug_hunt_export_storage as export_tests
from tests import test_identity_collisions as collision_tests
from tests import test_server_file_identity as server_tests


class SimulatedWindows:
    """Use host change times as native counters, with a fixed creation time."""

    def __init__(self):
        self.epoch = 0
        self.opened = []
        self.native = file_identity._windows_bindings() if os.name == "nt" else None

    def open_metadata_fd(self, path):
        # Test fixture access only. Native CreateFile access flags are tested
        # independently below; no Windows filesystem behavior is claimed here.
        fd = os.open(path, os.O_RDONLY)
        self.opened.append(fd)
        return fd

    def change_time(self, fd):
        changed = self.native.change_time(fd) if self.native else os.fstat(fd).st_ctime_ns
        return changed + self.epoch

    def bump(self):
        self.epoch += 1

    def install(self, case):
        stack = ExitStack()
        case.addCleanup(stack.close)
        original_fields = file_identity._stat_fields
        stack.enter_context(mock.patch.object(file_identity, "_stat_fields",
            side_effect=lambda stat: original_fields(stat)[:4] + (0,)))
        stack.enter_context(mock.patch.object(file_identity, "_WINDOWS", True))
        stack.enter_context(mock.patch.object(file_identity, "_windows_bindings",
                                              return_value=self))


class WindowsSignatureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "image.tif"
        self.path.write_bytes(b"unchanged fixture")

    def test_posix_signatures_remain_stat_only_and_unchanged(self):
        stat = self.path.stat()
        expected = (stat.st_dev, stat.st_ino, stat.st_size,
                    stat.st_mtime_ns, stat.st_ctime_ns)
        with mock.patch.object(file_identity, "_WINDOWS", False), \
                mock.patch.object(file_identity, "_windows_bindings",
                                  side_effect=AssertionError("native query")):
            self.assertEqual(file_identity.stat_signature(stat, path="missing"), expected)
            self.assertEqual(file_identity.signature_key(stat), ":".join(map(str, expected)))

    def test_windows_counter_changes_with_fixed_creation_time_and_mtime(self):
        windows = SimulatedWindows()
        windows.install(self)
        stat = self.path.stat()
        before = file_identity.stat_signature(stat, path=self.path)
        windows.bump()
        after = file_identity.stat_signature(stat, path=self.path)
        self.assertEqual(before[:5], after[:5])
        self.assertEqual(after[-1], before[-1] + 1)
        self.assertNotEqual(before, after)

    def test_windows_identity_cannot_silently_use_a_bare_stat(self):
        with mock.patch.object(file_identity, "_WINDOWS", True):
            with self.assertRaisesRegex(OSError, "path or descriptor"):
                file_identity.stat_signature(self.path.stat())

    def test_windows_path_and_fd_ctime_asymmetry_uses_common_birthtime(self):
        fields = dict(st_dev=1, st_ino=2, st_size=3, st_mtime_ns=4, st_birthtime_ns=5)
        path_stat = SimpleNamespace(**fields, st_ctime_ns=5)
        fd_stat = SimpleNamespace(**fields, st_ctime_ns=900)
        api = SimpleNamespace(change_time=mock.Mock(return_value=900))
        with mock.patch.object(file_identity, "_WINDOWS", True), \
                mock.patch.object(file_identity, "_windows_bindings", return_value=api), \
                mock.patch.object(file_identity.os, "fstat", return_value=fd_stat):
            # The supplied path stat and the already-open handle represent
            # the same file even when CPython exposes different ctime fields.
            self.assertEqual(file_identity.stat_signature(path_stat, fd=7),
                             file_identity.stat_signature(fd_stat, fd=7))
            self.assertEqual(file_identity.stat_signature(fd_stat, fd=7),
                             (1, 2, 3, 4, 5, 900))

    def test_owned_metadata_descriptor_closes_after_success_and_query_failure(self):
        windows = SimulatedWindows()
        windows.install(self)
        for error in (None, OSError("unsupported query")):
            with self.subTest(error=error):
                with mock.patch.object(windows, "change_time", side_effect=error,
                                       return_value=123):
                    if error:
                        with self.assertRaisesRegex(OSError, "unsupported query"):
                            file_identity.stat_signature(self.path.stat(), path=self.path)
                    else:
                        file_identity.stat_signature(self.path.stat(), path=self.path)
                with self.assertRaises(OSError):
                    os.fstat(windows.opened[-1])

    def test_opened_handle_must_match_supplied_file_identity(self):
        windows = SimulatedWindows()
        windows.install(self)
        other = self.path.with_name("replacement.tif")
        other.write_bytes(self.path.read_bytes())
        with self.assertRaisesRegex(OSError, "changed before querying"):
            file_identity.stat_signature(self.path.stat(), path=other)
        with self.assertRaises(OSError):
            os.fstat(windows.opened[-1])

    def test_mutation_during_metadata_query_is_rejected_and_descriptor_closed(self):
        windows = SimulatedWindows()
        windows.install(self)
        with mock.patch.object(windows, "change_time", side_effect=[100, 101]):
            with self.assertRaisesRegex(OSError, "changed while querying"):
                file_identity.stat_signature(self.path.stat(), path=self.path)
        with self.assertRaises(OSError):
            os.fstat(windows.opened[-1])

    def test_borrowed_hash_descriptor_is_neither_reopened_nor_closed(self):
        windows = SimulatedWindows()
        windows.install(self)
        with self.path.open("rb") as stream:
            fd = stream.fileno()
            with mock.patch.object(windows, "open_metadata_fd",
                                   side_effect=AssertionError("reopened stream")):
                file_identity.stat_signature(os.fstat(fd), fd=fd)
                with mock.patch.object(windows, "change_time", side_effect=OSError("query failed")):
                    with self.assertRaises(OSError):
                        file_identity.stat_signature(os.fstat(fd), fd=fd)
            self.assertEqual(stream.read(), b"unchanged fixture")

    def test_hash_retains_counter_from_before_reading(self):
        windows = SimulatedWindows()
        windows.install(self)
        real_digest = file_identity.hashlib.blake2b(digest_size=16)

        def update(block):
            real_digest.update(block)
            windows.bump()

        changed_digest = SimpleNamespace(update=update, hexdigest=real_digest.hexdigest)
        with mock.patch.object(file_identity.hashlib, "blake2b", return_value=changed_digest):
            with self.assertRaisesRegex(OSError, "changed while hashing"):
                file_identity.content_hash(self.path)


class WindowsBindingOwnershipTests(unittest.TestCase):
    def bindings(self):
        # Exercise the actual wrapper with fake Win32/CRT calls. Loading and
        # invoking the real DLL still needs separate native Windows validation.
        api = file_identity._WindowsBindings.__new__(file_identity._WindowsBindings)
        api.create_file = mock.Mock(return_value=42)
        api.open_osfhandle = mock.Mock(return_value=7)
        api.close_handle = mock.Mock()
        api.invalid_handle = -1
        api.ctypes = SimpleNamespace(WinError=lambda: OSError("native query failed"))
        return api

    def test_metadata_handle_requests_attributes_only_and_transfers_ownership(self):
        api = self.bindings()
        self.assertEqual(api.open_metadata_fd("original.tif"), 7)
        api.create_file.assert_called_once_with("original.tif", 0x80, 0x7, None, 3, 0, None)
        api.open_osfhandle.assert_called_once_with(42, os.O_RDONLY)
        api.close_handle.assert_not_called()

    def test_failed_descriptor_transfer_closes_native_handle_exactly_once(self):
        api = self.bindings()
        api.open_osfhandle.side_effect = OSError("descriptor allocation failed")
        with self.assertRaisesRegex(OSError, "allocation failed"):
            api.open_metadata_fd("original.tif")
        api.close_handle.assert_called_once_with(42)

    def test_failed_open_has_no_handle_to_transfer_or_close(self):
        api = self.bindings()
        api.create_file.return_value = -1
        with self.assertRaises(OSError):
            api.open_metadata_fd("original.tif")
        api.open_osfhandle.assert_not_called()
        api.close_handle.assert_not_called()

    def test_native_change_query_errors_and_missing_counter_fail_closed(self):
        api = self.bindings()

        class Info(ctypes.Structure):
            _fields_ = [("ChangeTime", ctypes.c_longlong)]

        api.basic_info = Info
        api.get_osfhandle = mock.Mock(return_value=42)
        api.ctypes = SimpleNamespace(byref=ctypes.byref, sizeof=ctypes.sizeof,
                                     WinError=lambda: OSError("native query failed"))
        api.query_file = mock.Mock(return_value=False)
        with self.assertRaisesRegex(OSError, "native query failed"):
            api.change_time(7)
        api.query_file.return_value = True
        with self.assertRaisesRegex(OSError, "does not provide"):
            api.change_time(7)


class WindowsSimulationMixin:
    def setUp(self):
        self.windows = SimulatedWindows()
        self.windows.install(self)
        super().setUp()


class SimulatedWindowsServerIdentityTests(WindowsSimulationMixin,
                                        server_tests.ServerFileIdentityTests):
    def test_file_key_retains_the_counter_from_before_content_lookup(self):
        def changed_content(path):
            self.windows.bump()
            return "complete-digest"

        with mock.patch.object(server, "content_hash", side_effect=changed_content):
            with self.assertRaisesRegex(OSError, "Original changed"):
                server.file_key(self.qualified("a.jpg"))

    def test_recovery_retains_the_counter_from_before_legacy_hash_read(self):
        def changed_header(path):
            self.windows.bump()
            return "legacy-digest"

        with mock.patch.object(catalog_scan, "header_hash", side_effect=changed_header):
            result = server.recovery_state_for(self.qualified("a.jpg"))
        self.assertIsNone(result["_recoverySourceKey"])
        self.assertIsNone(result["_recoveryLegacySourceKey"])

    def test_failed_query_cannot_reuse_a_populated_memory_cache(self):
        path = self.root / "a.jpg"
        self.assertTrue(server.content_hash(path))
        with mock.patch.object(self.windows, "change_time", side_effect=OSError("unsupported query")):
            with self.assertRaisesRegex(OSError, "unsupported query"):
                server.content_hash(path)


class SimulatedWindowsCollisionTests(WindowsSimulationMixin,
                                    collision_tests.IdentityCollisionTests):
    def test_failed_query_marks_scan_incomplete_without_hiding_previous_photos(self):
        original = self.photos / "original.tif"
        collision_tests.collision_pair(original, self.root / "other.tif")
        self.scan()
        original.rename(self.photos / "moved.tif")
        with mock.patch.object(self.windows, "change_time", side_effect=OSError("unsupported query")):
            result = self.scan()
        self.assertFalse(result["complete"])
        self.assertEqual(result["missing"], 0)
        self.assertEqual(result["added"], 0)
        self.assertEqual(self.cat.query()["total"], 1)
        self.assertIn("unsupported query", result["error"])

    def test_old_windows_creation_time_signature_is_backfilled_once(self):
        original = self.photos / "original.tif"
        collision_tests.collision_pair(original, self.root / "other.tif")
        self.scan()
        legacy_signature = ":".join(map(str, file_identity._stat_fields(original.stat())))
        with self.cat.write() as connection:
            connection.execute("UPDATE files SET content_signature=?", (legacy_signature,))
        with mock.patch.object(file_identity, "content_hash", wraps=file_identity.content_hash) as hashing:
            self.scan()
            self.scan()
        self.assertEqual(hashing.call_count, 1)

    def test_windows_scan_uses_full_stat_instead_of_zero_directory_file_ids(self):
        original = self.photos / "original.tif"
        collision_tests.collision_pair(original, self.root / "other.tif")
        entry = SimpleNamespace(name=original.name, path=str(original),
            is_dir=lambda **kwargs: False, is_file=lambda **kwargs: True,
            stat=mock.Mock(side_effect=AssertionError("DirEntry has no Windows file ID")))
        with mock.patch.object(catalog_scan.os, "scandir", return_value=[entry]):
            self.assertEqual(len(list(catalog_scan.walk_source(self.photos))), 1)
        entry.stat.assert_not_called()


class SimulatedWindowsExportTests(WindowsSimulationMixin, export_tests.ExportSnapshotTests):
    pass
