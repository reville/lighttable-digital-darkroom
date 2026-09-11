# SPDX-License-Identifier: GPL-3.0-only
from unittest import mock

import server
import xmp_sidecar
from test_server_catalog import CatalogServerTestCase


class DAMServerTests(CatalogServerTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(server, "_queue_mirror").start()
        mock.patch.object(server, "load_preferences", return_value={"writeSidecars": True}).start()

    def test_metadata_on_lean_payload_matches_scanned_exif(self):
        path = self.root / "a.jpg"
        from PIL import Image
        with Image.open(path) as image:
            exif = image.getexif()
            exif[271] = "Nikon"
            exif[272] = "Z6"
            image.save(path, exif=exif)
        import catalog_scan
        catalog_scan.scan_source(self.catalog, self.source)
        rows, _ = server.library_payload()
        row = next(row for row in rows if row["name"] == self.qualified("a.jpg"))
        self.assertEqual(row["camera"], "Nikon Z6")

    def test_additive_keyword_batch_uses_latest_catalog_tags_and_can_undo(self):
        names = [self.qualified("a.jpg"), self.qualified("sub/b.jpg")]
        for name, keyword in zip(names, ("Original A", "Original B")):
            self.catalog.save_state(server.catalog_image_id(name), {"keywords": [keyword]})
        result = server.keyword_batch_action({"action": "add", "names": names, "keywords": ["Places > Boston"]})
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["changes"][0]["keywords"], ["Original A", "Places > Boston"])
        self.assertEqual(server.sidecar_sync_status()["pending"], 2)
        self.assertEqual(server.write_pending_sidecars(), 2)
        self.assertIn("Places > Boston", xmp_sidecar.read_for(self.root / "a.jpg")["metadataKeywords"])
        server.keyword_batch_action({"action": "undo", "undoId": result["undoId"]})
        self.assertEqual(self.catalog.keywords_for(server.catalog_image_id(names[0])), ["Original A"])

    def test_keyword_rename_propagates_descendants_copies_and_sidecars(self):
        parent_name = self.qualified("a.jpg")
        child_name = self.qualified("sub/b.jpg")
        parent_id = server.catalog_image_id(parent_name)
        child_id = server.catalog_image_id(child_name)
        self.catalog.save_state(parent_id, {"keywords": ["Trip"]})
        self.catalog.save_state(child_id, {"keywords": ["Trip > Rome", "Tripod"]})
        copy_id = self.catalog.add_virtual_copy(child_id, "alternate", "Alternate")
        keyword = next(item for item in self.catalog.keyword_tree() if item["path"] == "Trip")
        with mock.patch.object(server.EVENTS, "publish") as publish:
            result = server.keyword_rename_action({"id": keyword["id"], "name": "Travel"})
        self.assertEqual(result["count"], 3)
        changes = {item["id"]: item for item in result["changes"]}
        self.assertEqual(changes[parent_id]["keywords"], ["Travel"])
        self.assertEqual(changes[child_id]["keywords"], ["Travel > Rome", "Tripod"])
        self.assertEqual(changes[copy_id]["keywords"], ["Travel > Rome", "Tripod"])
        event_type, event = publish.call_args.args
        self.assertEqual(event_type, "state")
        self.assertEqual(event["fields"], ["keywords"])
        self.assertEqual(event["patches"], {item["name"]: {"keywords": item["keywords"]}
                                            for item in result["changes"]})
        server._queue_mirror.assert_called_once()
        self.assertEqual(server.sidecar_sync_status()["pending"], 2)
        self.assertEqual(server.write_pending_sidecars(), 2)
        self.assertEqual(xmp_sidecar.read_for(self.root / "a.jpg")["metadataKeywords"], ["Travel"])
        self.assertEqual(set(xmp_sidecar.read_for(self.root / "sub/b.jpg")["metadataKeywords"]),
                         {"Travel > Rome", "Tripod"})
        self.assertEqual(self.catalog.query({"filter": {"query": "Travel"}})["total"], 3)
        # A reconciled browser save keeps the renamed tags alongside the next edit.
        server.save_image_state(child_name, {"keywords": changes[child_id]["keywords"],
                                            "grade": {"exposure": 1}})
        saved = self.catalog.state_for(child_id)
        self.assertEqual(saved["keywords"], ["Travel > Rome", "Tripod"])
        self.assertEqual(saved["grade"]["exposure"], 1)

    def test_rating_only_sync_preserves_unimported_foreign_caption_and_label(self):
        path = self.root / "a.jpg"
        path.with_suffix('.xmp').write_text(xmp_sidecar.build_sidecar({
            "rating": 1, "label": "Client Approved", "iptc": {"caption": "Other editor caption"},
            "params": {"film_stock": "Test stock"}}))
        name = self.qualified("a.jpg")
        server.save_image_state(name, {"rating": 4})
        self.assertEqual(server.write_pending_sidecars(), 1)
        parsed = xmp_sidecar.read_for(path)
        self.assertEqual(parsed["rating"], 4)
        self.assertEqual(parsed["label"], "Client Approved")
        self.assertEqual(parsed["caption"], "Other editor caption")

    def test_external_rating_conflict_persists_until_explicit_metadata_import(self):
        path = self.root / "a.jpg"
        sidecar = path.with_suffix('.xmp')
        sidecar.write_text(xmp_sidecar.build_sidecar({"rating": 1}))
        name = self.qualified("a.jpg")
        server.import_sidecars({"names": [name]})
        sidecar.write_text(xmp_sidecar.build_sidecar({"rating": 5}))
        server.save_image_state(name, {"rating": 4})
        self.assertEqual(server.write_pending_sidecars(), 0)
        self.assertEqual(server.sidecar_sync_status()["failed"], 1)
        self.assertEqual(xmp_sidecar.read_for(path)["rating"], 5)
        server.save_image_state(name, {"rating": 3})
        self.assertEqual(server.write_pending_sidecars(), 0)
        server.import_sidecars({"names": [name]})
        self.assertEqual(self.catalog.state_for(server.catalog_image_id(name))["rating"], 5)
        self.assertEqual(server.sidecar_sync_status()["pending"], 0)

    def test_caption_sync_preserves_foreign_rights_and_new_local_edits_during_write(self):
        path = self.root / 'a.jpg'; name = self.qualified('a.jpg')
        ident = server.catalog_image_id(name)
        path.with_suffix('.xmp').write_text(xmp_sidecar.build_sidecar({
            'iptc': {'caption': 'Old caption', 'copyright': 'Foreign rights'}}))
        server.save_catalog_metadata(name, ident, {'caption': 'First caption', 'copyright': None})
        write = xmp_sidecar.write_sidecar
        def arriving_edit(*args, **kwargs):
            result = write(*args, **kwargs)
            server.save_catalog_metadata(name, ident, {'caption': 'Second caption'})
            return result
        with mock.patch.object(xmp_sidecar, 'write_sidecar', side_effect=arriving_edit):
            self.assertEqual(server.write_pending_sidecars(), 1)
        self.assertEqual(server.sidecar_sync_status()['pending'], 1)
        self.assertEqual(server.write_pending_sidecars(), 1)
        parsed = xmp_sidecar.read_for(path)
        self.assertEqual(parsed['caption'], 'Second caption')
        self.assertEqual(parsed['copyright'], 'Foreign rights')

    def test_pending_sidecar_follows_catalog_rename_without_losing_conflict_baseline(self):
        path = self.root / 'a.jpg'; name = self.qualified('a.jpg')
        path.with_suffix('.xmp').write_text(xmp_sidecar.build_sidecar({'rating': 1}))
        server.import_sidecars({'names': [name]})
        server.save_image_state(name, {'rating': 4})
        moved = path.with_name('renamed.jpg')
        path.rename(moved)
        path.with_suffix('.xmp').rename(moved.with_suffix('.xmp'))
        self.catalog.relocate_files([(self.source, 'a.jpg', self.source, 'renamed.jpg')])
        self.assertEqual(server.write_pending_sidecars(), 1)
        self.assertEqual(xmp_sidecar.read_for(moved)['rating'], 4)

    def test_import_snapshots_all_query_pages_and_preserves_long_keyword_paths(self):
        name = self.qualified('a.jpg'); other = self.qualified('sub/b.jpg')
        keyword = 'Places > United States of America > Massachusetts > Boston > Waterfront'
        (self.root / 'sub/b.xmp').write_text(xmp_sidecar.build_sidecar({'keywords': [keyword]}))
        with mock.patch.object(self.catalog, 'query', side_effect=[
                {'total': 2, 'items': [{'name': name}]},
                {'total': 2, 'items': [{'name': other}]}]) as query:
            result = server.import_sidecars({})
        self.assertEqual(query.call_count, 2)
        self.assertEqual(result['applied'], 1)
        self.assertEqual(self.catalog.keywords_for(server.catalog_image_id(other)), [keyword])

    def test_import_retains_flat_and_hierarchical_keywords_and_reads_rejection(self):
        path = self.root / "a.jpg"
        fixture = __import__('pathlib').Path(__file__).parent / 'fixtures/xmp/digikam-representative.xmp'
        path.with_suffix('.xmp').write_text(fixture.read_text())
        name = self.qualified('a.jpg')
        parsed = xmp_sidecar.read_for(path)
        server.import_sidecars({"names": [name]})
        state = self.catalog.state_for(server.catalog_image_id(name))
        self.assertEqual(set(state["keywords"]), set(parsed["metadataKeywords"]))
        self.assertEqual(state["status"], parsed["status"])

    def test_explicit_metadata_clears_are_imported_but_absence_preserves(self):
        path = self.root / 'a.jpg'; name = self.qualified('a.jpg')
        ident = server.catalog_image_id(name)
        self.catalog.save_state(ident, {"rating": 5, "label": "red", "keywords": ["Keep"]})
        path.with_suffix('.xmp').write_text(xmp_sidecar.build_sidecar({"rating": 0}))
        server.import_sidecars({"names": [name]})
        self.assertEqual(self.catalog.state_for(ident)["keywords"], ["Keep"])
        path.with_suffix('.xmp').write_text(xmp_sidecar.build_sidecar({"rating": 0, "label": "none", "keywords": []}))
        server.import_sidecars({"names": [name]})
        state = self.catalog.state_for(ident)
        self.assertEqual((state['rating'],state['label'],state['keywords']), (0,'none',[]))

    def test_ambiguous_sidecar_names_are_reported_without_import(self):
        name = self.qualified('a.jpg'); path = self.root / 'a.jpg'
        path.with_suffix('.xmp').write_text(xmp_sidecar.build_sidecar({'rating': 5}))
        path.with_name('a.jpg.xmp').write_text(xmp_sidecar.build_sidecar({'rating': 1}))
        result = server.import_sidecars({'names':[name]})
        self.assertTrue(result['errors'])
        self.assertEqual(result['applied'],0)
