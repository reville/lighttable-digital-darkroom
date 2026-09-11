# SPDX-License-Identifier: GPL-3.0-only
"""Identity reads stay bounded without weakening source-change guards."""
import os
from unittest import mock

import catalog
import catalog_scan
import file_identity
import server
from tests.test_identity_collisions import collision_pair
from tests.test_server_catalog import CatalogServerTestCase


class ServerFileIdentityTests(CatalogServerTestCase):
    def setUp(self):
        super().setUp()
        self.cache_patch = mock.patch.object(server, "_HEADER_HASH_CACHE", {})
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    def test_scanned_identity_needs_no_complete_file_reread(self):
        path = self.root / "a.jpg"
        row = self.catalog.connection.execute(
            "SELECT content_hash FROM files WHERE source_id=? AND relpath=?",
            (self.source, "a.jpg")).fetchone()
        with mock.patch.object(file_identity, "content_hash",
                               side_effect=AssertionError("full reread")):
            self.assertEqual(server.content_hash(path), row["content_hash"])

    def test_same_size_mtime_replacement_invalidates_scanned_and_memory_hashes(self):
        path, replacement = self.root / "first.tif", self.root.parent / "other.tif"
        collision_pair(path, replacement)
        catalog_scan.scan_source(self.catalog, self.source, read_metadata_for_new=False)
        original = server.content_hash(path)
        stat = path.stat()
        path.write_bytes(replacement.read_bytes())
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with mock.patch.object(file_identity, "content_hash",
                               wraps=file_identity.content_hash) as full_hash:
            changed = server.content_hash(path)
        self.assertNotEqual(original, changed)
        self.assertEqual(full_hash.call_count, 1)

    def test_catalog_identity_requires_exact_source_and_relative_path(self):
        path = self.root / "a.jpg"
        stat = path.stat()
        # Even a row carrying the same signature cannot identify another path.
        with self.catalog.write() as conn:
            conn.execute("UPDATE files SET content_hash=?, content_signature=?,"
                         " size=?, mtime_ns=? WHERE source_id=? AND relpath=?",
                         ("wrong-path-digest", file_identity.signature_key(stat, path=path),
                          stat.st_size, stat.st_mtime_ns, self.source, "sub/b.jpg"))
            conn.execute("DELETE FROM files WHERE source_id=? AND relpath=?",
                         (self.source, "a.jpg"))
        with mock.patch.object(file_identity, "content_hash",
                               wraps=file_identity.content_hash) as full_hash:
            self.assertNotEqual(server.content_hash(path), "wrong-path-digest")
        self.assertEqual(full_hash.call_count, 1)

    def test_catalog_identity_without_full_signature_is_rehashed(self):
        with self.catalog.write() as conn:
            conn.execute("UPDATE files SET content_signature=NULL")
        with mock.patch.object(file_identity, "content_hash",
                               wraps=file_identity.content_hash) as full_hash:
            server.content_hash(self.root / "a.jpg")
        self.assertEqual(full_hash.call_count, 1)

    def test_catalog_reuse_rechecks_source_before_caching_digest(self):
        path = self.root / "a.jpg"
        lookup = server._catalog_content_hash

        def replace_after_lookup(candidate, stat):
            digest = lookup(candidate, stat)
            candidate.write_bytes(candidate.read_bytes() + b"changed")
            return digest

        with mock.patch.object(server, "_catalog_content_hash", replace_after_lookup):
            with self.assertRaisesRegex(OSError, "Original changed"):
                server.content_hash(path)
        self.assertEqual(server._HEADER_HASH_CACHE, {})

    def test_folder_enumeration_is_partial_until_complete_identity_is_available(self):
        path = self.root / "a.jpg"
        stat = path.stat()
        legacy = catalog.source_revision(catalog_scan.header_hash(path),
                                        stat.st_size, stat.st_mtime_ns)
        with mock.patch.object(server, "catalog_handle", return_value=None):
            with mock.patch.object(file_identity, "content_hash",
                                   side_effect=AssertionError("full library read")):
                rows, _ = server.library_payload(limit=1)
            row = next(row for row in rows if row["name"] == "a.jpg")
            self.assertEqual(row["recoverySourceKey"], legacy)
            full = server.file_key("a.jpg")
            self.assertNotEqual(full, legacy)
            with mock.patch.object(file_identity, "content_hash",
                                   side_effect=AssertionError("full library reread")):
                rows, _ = server.library_payload(limit=1)
            row = next(row for row in rows if row["name"] == "a.jpg")
            self.assertEqual(row["recoverySourceKey"], full)
