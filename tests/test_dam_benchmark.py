# SPDX-License-Identifier: GPL-3.0-only
"""Keep the synthetic scale benchmark honest about filters and offline state."""
import unittest
import tempfile
from datetime import datetime
from pathlib import Path

import catalog
from bench.dam_benchmark import run_worker, seed_catalog


class DAMBenchmarkTests(unittest.TestCase):
    def test_fixture_exercises_nonempty_filters_and_persists_offline_ratings(self):
        result = run_worker(1000, 1, 25)
        self.assertEqual(result["source_image_files"], 0)
        self.assertTrue(result["offline_queries_without_scan"])
        self.assertTrue(result["rating_saved_after_reopen"])
        self.assertEqual(result["queries"]["capture_deep_page"]["returned"], 25)
        self.assertEqual(result["queries"]["offline_source"]["total"], 500)
        for name, query in result["queries"].items():
            self.assertGreater(query["total"], 0, name)
            self.assertLessEqual(query["returned"], 25, name)
        # A filter silently ignored by query() must not pass this benchmark.
        self.assertLess(result["queries"]["numeric_exif_filter"]["total"], 1000)

    def test_paged_sort_orders_match_an_independent_in_memory_oracle(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "synthetic.sqlite3"
            fixture = seed_catalog(path, 1000)
            cat = catalog.Catalog(path)
            try:
                virtual = cat.add_virtual_copy(1, "alternate", "Alternate")
                cat.save_state(virtual, {"rating": 5, "status": "approved", "label": "purple"})
                # Missing state should use the same NULL ordering as an
                # unrated imported record, without dropping it from a page.
                with cat.write() as conn:
                    conn.execute("DELETE FROM image_state WHERE image_id=2")
                rows = [dict(row) for row in cat.connection.execute(
                    "SELECT i.id, i.copy_ident, i.created_at, f.source_id, f.folder_id,"
                    " f.filename, f.relpath, f.size, f.lens, s.rating, s.status, s.label,"
                    " COALESCE(ct.capture_time,f.capture_time,f.mtime_iso) AS captured"
                    " FROM images i JOIN files f ON f.id=i.file_id"
                    " LEFT JOIN image_state s ON s.image_id=i.id"
                    " LEFT JOIN capture_overrides ct ON ct.file_id=f.id")]
                scopes = [
                    ({}, lambda row: True),
                    ({"scope": "source", "sourceId": fixture["sources"][1]}, lambda row: row["source_id"] == fixture["sources"][1]),
                    ({"scope": "folder", "folderId": 1, "includeSubfolders": False}, lambda row: row["folder_id"] == 1),
                    ({"scope": "collection", "collectionId": fixture["regular"]}, lambda row: row["id"] <= 1000),
                    ({"scope": "collection", "collectionId": fixture["smart"]}, lambda row: (row["rating"] or 0) >= 4 and "50mm" in row["lens"] and row["captured"].startswith("2026")),
                    ({"filter": {"ratingMin": 4}}, lambda row: (row["rating"] or 0) >= 4),
                ]
                fields = {
                    "capture": lambda row: datetime.fromisoformat(row["captured"]).timestamp(),
                    "name": lambda row: row["filename"].casefold(),
                    "rating": lambda row: row["rating"],
                    "status": lambda row: row["status"],
                    "label": lambda row: row["label"],
                    "added": lambda row: row["created_at"],
                    "size": lambda row: row["size"],
                }
                for scope, predicate in scopes:
                    for field, value in fields.items():
                        for direction in ("asc", "desc"):
                            expected = sorted((row for row in rows if predicate(row)),
                                key=lambda row: (row["filename"].casefold(), row["id"]))
                            # Stable two-stage ordering keeps filename/id ties
                            # ascending even when the primary sort descends.
                            expected.sort(key=lambda row: (value(row) is not None, value(row)),
                                reverse=direction == "desc")
                            for offset in (0, 17, max(0, len(expected) - 17)):
                                spec = {**scope, "sort": {"field": field, "dir": direction},
                                        "offset": offset, "limit": 17}
                                with self.subTest(spec=spec):
                                    actual = cat.query(spec, include_state=True)
                                    page = expected[offset:offset + 17]
                                    self.assertEqual(actual["total"], len(expected))
                                    self.assertEqual([item["id"] for item in actual["items"]],
                                                     [row["id"] for row in page])
                                    self.assertEqual([item["rating"] for item in actual["items"]],
                                                     [row["rating"] or 0 for row in page])
            finally:
                cat.close()


if __name__ == "__main__":
    unittest.main()
