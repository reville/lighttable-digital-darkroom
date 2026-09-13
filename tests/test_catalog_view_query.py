# SPDX-License-Identifier: GPL-3.0-only
"""The query contract the browser grid relies on once filtering, sorting,
hiding and locating all happen in the catalog rather than over loaded rows."""
from unittest import mock

import catalog_scan
import server
from test_server_catalog import CatalogServerTestCase, make_photo


class ViewQueryContractTests(CatalogServerTestCase):
    def names(self, spec):
        page = server.browser_catalog_query(spec)
        return [item["name"] for item in page["items"]], page

    def test_sort_direction_is_honoured_for_every_field(self):
        server.save_image_state(self.qualified("a.jpg"), {"rating": 1, "label": "red"})
        server.save_image_state(self.qualified("sub/b.jpg"), {"rating": 4, "label": "blue"})
        a, b = self.qualified("a.jpg"), self.qualified("sub/b.jpg")
        for field, ascending in (("name", [a, b]), ("rating", [a, b]), ("label", [a, b])):
            names, _ = self.names({"sort": {"field": field, "dir": "asc"}})
            self.assertEqual(names, ascending, field)
            names, _ = self.names({"sort": {"field": field, "dir": "desc"}})
            self.assertEqual(names, list(reversed(ascending)), field)

    def test_label_sort_follows_the_colour_order_not_the_alphabet(self):
        make_photo(self.root / "c.jpg")
        catalog_scan.scan_source(self.catalog, self.source, read_metadata_for_new=False)
        server.save_image_state(self.qualified("a.jpg"), {"label": "purple"})
        server.save_image_state(self.qualified("sub/b.jpg"), {"label": "yellow"})
        server.save_image_state(self.qualified("c.jpg"), {"label": "green"})
        names, _ = self.names({"sort": {"field": "label", "dir": "asc"}})
        self.assertEqual(names, [self.qualified("sub/b.jpg"), self.qualified("c.jpg"),
                                 self.qualified("a.jpg")])

    def test_locate_reports_the_position_in_the_ordering_or_none(self):
        server.save_image_state(self.qualified("a.jpg"), {"rating": 5})
        _, page = self.names({"sort": {"field": "name", "dir": "desc"},
                              "limit": 1, "locate": self.qualified("a.jpg")})
        self.assertEqual(page["located"], 1)
        self.assertEqual(page["total"], 2)
        _, page = self.names({"filter": {"ratingMin": 5}, "locate": self.qualified("sub/b.jpg")})
        self.assertIsNone(page["located"])
        _, page = self.names({"locate": "9:nowhere.jpg"})
        self.assertIsNone(page["located"])
        names_page = server.browser_catalog_query({"namesOnly": True, "sort": {"field": "name"},
                                                   "locate": self.qualified("sub/b.jpg")})
        self.assertEqual(names_page["located"], 1)

    def test_name_lists_restrict_exclude_and_extend_a_search(self):
        a, b = self.qualified("a.jpg"), self.qualified("sub/b.jpg")
        names, page = self.names({"names": [b, "9:missing.jpg"]})
        self.assertEqual((names, page["total"]), ([b], 1))
        names, page = self.names({"excludeNames": [b], "sort": {"field": "name"}})
        self.assertEqual((names, page["total"]), ([a], 1))
        # A search that matches nothing in the catalog still lists the
        # photos the browser's local index matched, in catalog order.
        names, _ = self.names({"filter": {"query": "zzzz-no-such-word"}})
        self.assertEqual(names, [])
        names, _ = self.names({"filter": {"query": "zzzz-no-such-word"},
                               "searchAlsoNames": [b]})
        self.assertEqual(names, [b])

    def test_pair_hiding_binds_scope_parameters_in_the_right_order(self):
        make_photo(self.root / "c.jpg")
        (self.root / "c.dng").write_bytes(b"not-a-real-raw")
        catalog_scan.scan_source(self.catalog, self.source, read_metadata_for_new=False)
        both, page = self.names({"sourceId": self.source, "pairView": "both",
                                 "sort": {"field": "name"}})
        self.assertEqual(page["total"], 4)
        self.assertIn(self.qualified("c.dng"), both)
        self.assertIn(self.qualified("c.jpg"), both)
        for view, hidden, shown in (("raw", "c.jpg", "c.dng"), ("jpeg", "c.dng", "c.jpg")):
            names, page = self.names({"sourceId": self.source, "pairView": view,
                                      "sort": {"field": "name"}})
            self.assertEqual(page["total"], 3, view)
            self.assertNotIn(self.qualified(hidden), names)
            self.assertIn(self.qualified(shown), names)
            self.assertEqual([item["pair"] for item in page["items"]
                              if item["name"] == self.qualified(shown)],
                             [self.qualified(hidden)])
        # An explicit switch keeps the requested member on screen.
        names, page = self.names({"sourceId": self.source, "pairView": "raw",
                                  "pairOverrides": [self.qualified("c.jpg")],
                                  "sort": {"field": "name"}})
        self.assertIn(self.qualified("c.jpg"), names)
        self.assertNotIn(self.qualified("c.dng"), names)
        self.assertEqual(page["total"], 3)
        # A member filtered out of the view does not hide its companion.
        names, page = self.names({"sourceId": self.source, "pairView": "raw",
                                  "filter": {"fileTypes": ["jpeg"]}})
        self.assertIn(self.qualified("c.jpg"), names)

    def test_collapsed_stacks_show_the_first_member_still_in_the_view(self):
        make_photo(self.root / "c.jpg")
        catalog_scan.scan_source(self.catalog, self.source, read_metadata_for_new=False)
        a, b, c = (self.qualified(n) for n in ("a.jpg", "sub/b.jpg", "c.jpg"))
        ids = [server.catalog_image_id(n) for n in (b, a)]
        stack = self.catalog.add_stack("burst", ids)
        names, page = self.names({"collapseStacks": True, "sort": {"field": "name"}})
        self.assertEqual((names, page["total"]), ([b, c], 2))
        names, _ = self.names({"sort": {"field": "name"}})
        self.assertEqual(names, [a, b, c])
        server.save_image_state(a, {"rating": 3})
        names, page = self.names({"collapseStacks": True, "filter": {"ratingMin": 3}})
        self.assertEqual((names, page["total"]), ([a], 1))
        self.catalog.toggle_stack(stack)
        names, _ = self.names({"collapseStacks": True, "sort": {"field": "name"}})
        self.assertEqual(names, [a, b, c])

    def test_counts_only_tallies_a_scope_and_collections_carry_counts(self):
        server.save_image_state(self.qualified("a.jpg"), {"rating": 2, "status": "approved"})
        server.save_image_state(self.qualified("sub/b.jpg"), {"status": "skipped"})
        page = server.browser_catalog_query({"countsOnly": True, "sourceId": self.source})
        self.assertEqual(page["counts"], {"all": 2, "pending": 0, "approved": 1,
                                          "skipped": 1, "rated": 1})
        self.assertEqual(page["items"], [])
        smart = self.catalog.add_collection("Picked", kind="smart", rules={"status": "approved"})
        regular = self.catalog.add_collection("Both")
        self.catalog.set_collection_members(regular, [
            server.catalog_image_id(self.qualified("a.jpg")),
            server.catalog_image_id(self.qualified("sub/b.jpg"))])
        counts = {item["name"]: item["count"] for item in server.current_library_state()["collections"]}
        self.assertEqual(counts, {"Picked": 1, "Both": 2})
        ids = {item["name"]: item["id"] for item in server.current_library_state()["collections"]}
        self.assertEqual(ids["Picked"], str(smart))
