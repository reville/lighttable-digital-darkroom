# SPDX-License-Identifier: GPL-3.0-only
"""A render must store its pixels under the key that describes them.

`preview_variant` reads the shared input cache, which other threads prune by
size. Asking it again between choosing the cache key and choosing the source
TIFF could answer "full" for the key and hand back the draft demosaic for the
pixels, storing a draft as the accurate render for the life of the cache.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import server


class RenderKeyVariantTests(unittest.TestCase):
    def test_the_key_uses_the_variant_it_is_given(self):
        with mock.patch.object(server, "file_key", return_value="k"), \
                mock.patch.object(server, "is_raw", return_value=True), \
                mock.patch.object(server, "preview_variant") as asked:
            fast = server.render_key("a.CR2", {}, 1100, "rs", "fast")
            full = server.render_key("a.CR2", {}, 1100, "rs", "full")
        self.assertNotEqual(fast, full)
        asked.assert_not_called()

    def test_the_key_still_asks_when_no_variant_is_pinned(self):
        with mock.patch.object(server, "file_key", return_value="k"), \
                mock.patch.object(server, "is_raw", return_value=True), \
                mock.patch.object(server, "preview_variant",
                                  return_value="full") as asked:
            server.render_key("a.CR2", {}, 1100, "rs")
        asked.assert_called_once()


class SelectedPreviewTiffTests(unittest.TestCase):
    """A pinned "full" render never silently falls back to the draft."""

    def test_a_pruned_full_tiff_is_rebuilt_rather_than_downgraded(self):
        built = []

        def build(name, width, quality, params=None):
            built.append(quality)
            return Path(f"/tmp/{quality}.tif")

        with mock.patch.object(server, "raw_preview_path",
                               return_value=Path("/tmp/missing.tif")), \
                mock.patch.object(server, "valid_tiff_cache", return_value=False), \
                mock.patch.object(server, "build_raw_preview", side_effect=build):
            server.selected_preview_tiff("a.CR2", 1100, {}, "full")
        self.assertEqual(built, ["full"],
                         "a render keyed as accurate must not be given draft pixels")

    def test_an_unpinned_request_still_prefers_the_quick_draft(self):
        built = []

        def build(name, width, quality, params=None):
            built.append(quality)
            return Path(f"/tmp/{quality}.tif")

        with mock.patch.object(server, "raw_preview_path",
                               return_value=Path("/tmp/missing.tif")), \
                mock.patch.object(server, "valid_tiff_cache", return_value=False), \
                mock.patch.object(server, "build_raw_preview", side_effect=build):
            server.selected_preview_tiff("a.CR2", 1100, {})
        self.assertEqual(built, ["fast"])

    def test_a_present_full_tiff_is_used_unchanged(self):
        with mock.patch.object(server, "raw_preview_path",
                               return_value=Path("/tmp/full.tif")), \
                mock.patch.object(server, "valid_tiff_cache", return_value=True), \
                mock.patch.object(server, "build_raw_preview") as build:
            self.assertEqual(
                server.selected_preview_tiff("a.CR2", 1100, {}, "full"),
                Path("/tmp/full.tif"))
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
