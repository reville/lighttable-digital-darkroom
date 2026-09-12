# SPDX-License-Identifier: GPL-3.0-only
import unittest

import library_workflow as library


class LibraryWorkflowTests(unittest.TestCase):
    def test_virtual_copy_round_trip_preserves_source(self):
        name = library.virtual_name("trip/photo.RAF", "copy-1")
        self.assertTrue(library.is_virtual(name))
        self.assertEqual(library.source_name(name), "trip/photo.RAF")

    def test_stacks_do_not_allow_one_photo_in_two_stacks(self):
        stacks = library.clean_stacks([
            {"members": ["a.jpg", "b.jpg"]},
            {"members": ["b.jpg", "c.jpg"]},
        ])
        self.assertEqual(len(stacks), 1)

    def test_smart_collection_rules_are_bounded(self):
        collection = library.clean_collections([{
            "name": "Five star RAW", "type": "smart",
            "rules": {"ratingMin": 99, "kind": "raw", "flag": "wrong"},
        }])[0]
        self.assertEqual(collection["rules"]["ratingMin"], 5)

    def test_malformed_rating_rule_does_not_raise(self):
        for value in ("abc", {"x": 1}, [1], None):
            with self.subTest(value=value):
                collection = library.clean_collections([{
                    "name": "Smart", "type": "smart",
                    "rules": {"ratingMin": value},
                }])[0]
                self.assertEqual(collection["rules"]["ratingMin"], 0)
        self.assertEqual(collection["rules"]["flag"], "all")


if __name__ == "__main__":
    unittest.main()
