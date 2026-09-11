# SPDX-License-Identifier: GPL-3.0-only
"""Moving a photo must carry every state key that belongs to it.

Folder mode keys edits by path. A virtual copy's key is the photo's path plus
`::lighttable-copy::<id>`, which is neither the path nor a child of it, so a
prefix rule written for folders left the copy's edits behind while the copy
itself was renamed onto the new path.
"""
from __future__ import annotations

import unittest

import catalog as catalog_module
import server


class StateKeyMoveTests(unittest.TestCase):
    def test_the_photo_itself_moves(self):
        self.assertTrue(
            server._state_key_moves("Inbox/one.jpg", "Inbox/one.jpg",
                                    "Archive/one.jpg"))

    def test_a_virtual_copy_of_that_photo_moves_with_it(self):
        key = "Inbox/one.jpg" + catalog_module.VIRTUAL_MARKER + "copy-1"
        self.assertTrue(
            server._state_key_moves(key, "Inbox/one.jpg", "Archive/one.jpg"))

    def test_a_folder_still_carries_its_children(self):
        self.assertTrue(
            server._state_key_moves("Inbox/sub/x.jpg", "Inbox", "Archive"))

    def test_an_unrelated_photo_stays_put(self):
        self.assertFalse(
            server._state_key_moves("Inbox/other.jpg", "Inbox/one.jpg",
                                    "Archive/one.jpg"))

    def test_a_similarly_named_neighbour_is_not_dragged_along(self):
        self.assertFalse(
            server._state_key_moves("Inbox/one.jpg.bak", "Inbox/one.jpg",
                                    "Archive/one.jpg"))


class RemapStateTests(unittest.TestCase):
    def test_a_move_carries_the_copy_edits_and_its_memberships(self):
        copy_key = "Inbox/one.jpg" + catalog_module.VIRTUAL_MARKER + "copy-1"
        state = {
            "images": {
                "Inbox/one.jpg": {"rating": 3},
                copy_key: {"rating": 5, "label": "red"},
            },
            "virtualCopies": [{"source": "Inbox/one.jpg", "name": copy_key}],
            "collections": [{"name": "Picks", "members": [copy_key]}],
            "stacks": [{"members": ["Inbox/one.jpg", copy_key]}],
        }
        written = {}

        def fake_write(value):
            written.update(value)

        original_load, original_write = server.load_state, server.write_state
        server.load_state = lambda: state
        server.write_state = fake_write
        try:
            server._remap_state_paths_many(
                [("Inbox/one.jpg", "Archive/one.jpg")])
        finally:
            server.load_state, server.write_state = original_load, original_write

        moved_copy = "Archive/one.jpg" + catalog_module.VIRTUAL_MARKER + "copy-1"
        self.assertIn("Archive/one.jpg", written["images"])
        self.assertIn(moved_copy, written["images"])
        self.assertEqual(written["images"][moved_copy]["rating"], 5)
        self.assertNotIn(copy_key, written["images"])
        self.assertEqual(written["virtualCopies"][0]["name"], moved_copy)
        self.assertEqual(written["virtualCopies"][0]["source"], "Archive/one.jpg")
        self.assertEqual(written["collections"][0]["members"], [moved_copy])
        self.assertEqual(written["stacks"][0]["members"],
                         ["Archive/one.jpg", moved_copy])


if __name__ == "__main__":
    unittest.main()
