"""Real catalog and API-route coverage for explicit RAW+JPEG metadata linking."""
import unittest
from pathlib import Path
from unittest import mock

import catalog as catalog_module
import catalog_scan
import server
from tests.test_server_catalog import CatalogServerTestCase, make_photo


class PairMetadataTests(CatalogServerTestCase):
    def setUp(self):
        super().setUp()
        for rel in ('capture.CR3', 'sub/capture.CR3'):
            (self.root / rel).write_bytes(b'raw fixture')
        make_photo(self.root / 'capture.JPG')
        make_photo(self.root / 'sub/capture.JPG')
        catalog_scan.scan_source(self.catalog, self.source, read_metadata_for_new=False)
        self.raw = self.qualified('capture.CR3')
        self.jpeg = self.qualified('capture.JPG')
        self.prefs = {'pairRawJPEG': True, 'linkPairedMetadata': True}
        self.extra_patches = [mock.patch.object(server, 'load_preferences', lambda: self.prefs),
                              mock.patch.object(server, 'queue_sidecar'),
                              mock.patch.object(server.EVENTS, 'publish')]
        for patch in self.extra_patches:
            patch.start()

    def tearDown(self):
        for patch in self.extra_patches:
            patch.stop()
        super().tearDown()

    def post(self, path, body):
        handler = server.Handler.__new__(server.Handler)
        handler.path = path
        handler.headers = {}
        handler._body = lambda: body
        response = []
        handler._json = lambda payload, status=200: response.append((status, payload))
        handler._log_request = lambda started: None
        handler.do_POST()
        self.assertEqual(len(response), 1)
        return response[0]

    def test_single_state_route_links_all_four_mark_fields_but_not_pixels(self):
        server.save_image_state(self.jpeg, {'grade': {'exposure': -1}, 'crop': {'x': .2}})
        status, result = self.post('/api/state', {'name': self.raw, 'rating': 5,
            'status': 'approved', 'label': 'red', 'keywords': ['Travel'], 'grade': {'exposure': .4}})
        self.assertEqual(status, 200)
        self.assertEqual(set(result['names']), {self.raw, self.jpeg})
        paired = server.catalog_entry_for(self.jpeg)
        self.assertEqual((paired['rating'], paired['status'], paired['label'], paired['keywords']),
                         (5, 'approved', 'red', ['Travel']))
        self.assertEqual(paired['grade'], {'exposure': -1})
        self.assertEqual(paired['crop'], {'x': .2})
        self.assertEqual(server.catalog_entry_for(self.qualified('sub/capture.JPG'))['rating'], 0)
        server.EVENTS.publish.assert_called_once()
        self.assertEqual(set(server.EVENTS.publish.call_args.args[1]['names']), {self.raw, self.jpeg})

    def test_bulk_route_finds_companion_without_any_loaded_ui_rows(self):
        status, result = self.post('/api/state/bulk', {'names': [self.raw], 'entry': {'rating': 4}})
        self.assertEqual(status, 200)
        self.assertEqual(server.catalog_entry_for(self.jpeg)['rating'], 4)
        self.assertEqual(server.catalog_entry_for(self.raw)['rating'], 4)

    def test_opt_in_and_master_pair_preferences_are_respected(self):
        for prefs in ({}, {'linkPairedMetadata': False}, {'linkPairedMetadata': True, 'pairRawJPEG': False}):
            self.prefs = prefs
            self.post('/api/state', {'name': self.raw, 'rating': 3})
            self.assertEqual(server.catalog_entry_for(self.jpeg)['rating'], 0)

    def test_same_basename_other_source_and_virtual_copies_remain_independent(self):
        root = Path(self._dir.name) / 'other'
        make_photo(root / 'capture.JPG')
        (root / 'capture.CR3').write_bytes(b'other RAW')
        other_id = self.catalog.add_source(root)
        catalog_scan.scan_source(self.catalog, other_id, read_metadata_for_new=False)
        raw_id = server.catalog_image_id(self.raw)
        copy_id = self.catalog.add_virtual_copy(raw_id, 'alt', 'Alternate')
        virtual = catalog_module.qualified_name(self.source, 'capture.CR3', 'alt')
        self.post('/api/state', {'name': self.raw, 'rating': 4})
        self.assertEqual(self.catalog.state_for(copy_id)['rating'], 0)
        self.assertEqual(server.catalog_entry_for(catalog_module.qualified_name(other_id, 'capture.JPG'))['rating'], 0)
        self.post('/api/state', {'name': virtual, 'rating': 1})
        self.assertEqual(server.catalog_entry_for(self.jpeg)['rating'], 4)

    def test_ambiguous_multiple_raw_variants_are_not_linked(self):
        (self.root / 'capture.DNG').write_bytes(b'another RAW')
        catalog_scan.scan_source(self.catalog, self.source, read_metadata_for_new=False)
        self.post('/api/state', {'name': self.raw, 'rating': 5})
        self.assertEqual(server.catalog_entry_for(self.jpeg)['rating'], 0)

    def test_pixel_save_does_not_synchronize_unchanged_legacy_metadata(self):
        server.save_image_state(self.raw, {'rating': 4, 'label': 'red'})
        server.save_image_state(self.jpeg, {'rating': 1, 'label': 'blue'})
        self.post('/api/state', {'name': self.raw, 'rating': 4, 'label': 'red', 'grade': {'exposure': .5}})
        paired = server.catalog_entry_for(self.jpeg)
        self.assertEqual((paired['rating'], paired['label']), (1, 'blue'))
        # An explicit CLI-style mark action sets even an unchanged source value.
        self.post('/api/state', {'name': self.raw, 'rating': 4})
        self.assertEqual(server.catalog_entry_for(self.jpeg)['rating'], 4)
        self.assertEqual(server.catalog_entry_for(self.jpeg)['label'], 'blue')

    def test_conflicting_pair_marks_are_rejected_before_any_write(self):
        with self.assertRaisesRegex(ValueError, 'conflicting metadata'):
            server.save_image_states(server.expand_paired_metadata({
                self.raw: {'rating': 5}, self.jpeg: {'rating': 1}}))
        self.assertEqual(server.catalog_entry_for(self.raw)['rating'], 0)
        self.assertEqual(server.catalog_entry_for(self.jpeg)['rating'], 0)

    def test_pair_batch_rolls_back_both_members_if_second_save_fails(self):
        save = self.catalog._save_state
        count = 0
        def fail_second(conn, image_id, entry):
            nonlocal count
            count += 1
            save(conn, image_id, entry)
            if count == 2:
                raise OSError('simulated disk failure')
        with mock.patch.object(self.catalog, '_save_state', fail_second):
            with self.assertRaisesRegex(OSError, 'simulated disk failure'):
                server.save_image_states(server.expand_paired_metadata({self.raw: {'rating': 5}}))
        self.assertEqual(server.catalog_entry_for(self.raw)['rating'], 0)
        self.assertEqual(server.catalog_entry_for(self.jpeg)['rating'], 0)
        server.queue_sidecar.assert_not_called()

    def test_folder_mode_links_only_physical_siblings(self):
        with mock.patch.object(server, 'CATALOG', None):
            updates = server.expand_paired_metadata({'capture.CR3': {'keywords': ['Rome']}})
        self.assertEqual(set(updates), {'capture.CR3', 'capture.JPG'})
        self.assertEqual(updates['capture.JPG'], {'keywords': ['Rome']})


if __name__ == '__main__':
    unittest.main()
