# SPDX-License-Identifier: GPL-3.0-only
"""Concurrent content hashing during a scan must not change what the catalog records."""
from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

import catalog
import catalog_scan
import file_identity


class ScanHashPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "photos"
        self.root.mkdir()

    def photos(self, count):
        paths = []
        for index in range(count):
            path = self.root / f"roll-{index % 3}" / f"frame-{index:03d}.jpg"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(bytes([index % 251]) * (70000 + index * 13))
            paths.append(path)
        return paths

    def rows(self, workers):
        with mock.patch.object(catalog_scan, "HASH_WORKERS", workers), \
                mock.patch.object(catalog_scan, "_HASH_POOL", None):
            cat = catalog.Catalog(Path(self.temp.name) / f"library-{workers}.sqlite3")
            try:
                source = cat.add_source(self.root)
                result = catalog_scan.scan_source(cat, source, read_metadata_for_new=False)
                self.assertTrue(result["complete"], result)
                return [dict(row) for row in cat.connection.execute(
                    "SELECT relpath, header_hash, content_hash FROM files ORDER BY relpath")]
            finally:
                cat.close()

    def test_pooled_hashing_records_the_same_identities_as_serial(self):
        paths = self.photos(12)
        serial = self.rows(1)
        pooled = self.rows(4)
        self.assertEqual(serial, pooled)
        self.assertEqual(len(pooled), 12)
        expected = {str(path.relative_to(self.root)): file_identity.content_hash(path)
                    for path in paths}
        self.assertEqual({row["relpath"]: row["content_hash"] for row in pooled}, expected)
        self.assertEqual(len({row["content_hash"] for row in pooled}), 12)

    def test_identify_records_reports_failures_in_walk_order(self):
        paths = self.photos(6)
        records = []
        for path in paths:
            stat = path.stat()
            records.append({"path": str(path), "size": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                            "content_signature": file_identity.signature_key(stat, path=path)})
        # A revision mismatch makes the hasher refuse two of the six files.
        records[1]["mtime_ns"] += 1
        records[4]["mtime_ns"] += 1
        errors = []
        with mock.patch.object(catalog_scan, "HASH_WORKERS", 4), \
                mock.patch.object(catalog_scan, "_HASH_POOL", None):
            catalog_scan.identify_records(records, errors.append)
        self.assertEqual([record.get("identity_error", False) for record in records],
                         [False, True, False, False, True, False])
        self.assertEqual(len(errors), 2)
        self.assertIn(paths[1].name, errors[0])
        self.assertIn(paths[4].name, errors[1])
        for index in (0, 2, 3, 5):
            self.assertEqual(records[index]["content_hash"],
                             file_identity.content_hash(paths[index]))
            self.assertEqual(records[index]["header_hash"],
                             catalog_scan.header_hash(paths[index]))

    def test_worker_count_honours_override_and_windows_serial_rule(self):
        with mock.patch.dict(os.environ, {"LIGHTTABLE_SCAN_HASH_WORKERS": "3"}):
            self.assertEqual(catalog_scan.hash_worker_count(), 3)
        with mock.patch.dict(os.environ, {"LIGHTTABLE_SCAN_HASH_WORKERS": "0"}):
            self.assertEqual(catalog_scan.hash_worker_count(), 1)
        with mock.patch.dict(os.environ, {"LIGHTTABLE_SCAN_HASH_WORKERS": "many"}), \
                mock.patch.object(catalog_scan.os, "name", "nt"):
            self.assertEqual(catalog_scan.hash_worker_count(), 1)
        with mock.patch.dict(os.environ, {"LIGHTTABLE_SCAN_HASH_WORKERS": ""}), \
                mock.patch.object(catalog_scan.os, "name", "posix"), \
                mock.patch.object(catalog_scan.os, "cpu_count", return_value=16):
            self.assertEqual(catalog_scan.hash_worker_count(), 4)
        with mock.patch.dict(os.environ, {"LIGHTTABLE_SCAN_HASH_WORKERS": ""}), \
                mock.patch.object(catalog_scan.os, "name", "posix"), \
                mock.patch.object(catalog_scan.os, "cpu_count", return_value=2):
            self.assertEqual(catalog_scan.hash_worker_count(), 2)


if __name__ == "__main__":
    unittest.main()
