"""Actual delivery pixels, accepted assets, and complete rename preflight."""
import base64
import concurrent.futures
import copy
from pathlib import Path
import tempfile
from unittest import mock

import numpy as np
import tifffile
import edits
import server
from test_server_catalog import CatalogServerTestCase


class DeliveryIntegrityTests(CatalogServerTestCase):
    def test_single_and_parallel_workers_preserve_accepted_masks_heals_and_output_profile(self):
        y, x = np.mgrid[:48, :64]
        photo = np.stack((x / 63, y / 47, (x + y) / 110), axis=2)
        mask = np.zeros((12, 16), dtype=np.uint8)
        mask[:, :8] = 255
        masks = [{'id': 'accepted', 'type': 'subject', 'enabled': True,
                  'bitmap': {'width': 16, 'height': 12,
                             'data': base64.b64encode(mask.tobytes()).decode()},
                  'grade': {'exposure': .8}}]
        state = {'params': {'profile_enabled': False}, 'masks': masks,
                 'heals': [{'id': 'heal', 'mode': 'clone', 'target': [.5, .5],
                            'source': [.2, .2], 'radius': .15, 'feather': .2}]}
        name = self.qualified('a.jpg')
        self.catalog.save_state(server.catalog_image_id(name), state)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.tif'
            tifffile.imwrite(source, (photo * 65535).astype(np.uint16), photometric='rgb')
            cache = root / 'cache'; cache.mkdir()
            recipe = {'names': [name], 'format': 'tif', 'outputSpace': 'prophoto',
                      'metadata': 'none', 'sidecar': False, 'destination': str(root / 'single')}
            jobs = [server.prepare_export(recipe)[0][0][1]]
            recipe['destination'] = str(root / 'batch')
            jobs += [server.prepare_export(recipe)[0][0][1] for _ in range(2)]
            # Mutating the live edit after planning must not change delivery.
            self.catalog.save_state(server.catalog_image_id(name), {'masks': [], 'heals': []})
            with mock.patch.object(server, 'CACHE', cache), \
                 mock.patch.object(server, 'export_render_source', return_value=source):
                single = server._export_one(name, jobs[0])
                with concurrent.futures.ThreadPoolExecutor(2) as pool:
                    results = list(pool.map(lambda job: server._export_one(name, job), jobs[1:]))
                self.assertEqual(single.get('completed'), 1, single)
                for result in results:
                    self.assertEqual(result.get('completed'), 1, result)
                    np.testing.assert_array_equal(tifffile.imread(single['path']), tifffile.imread(result['path']))
                plain = root / 'plain.tif'
                server.finish_export(source, plain, dict(jobs[0], masks=[], heals=[]))
                self.assertGreater(np.abs(tifffile.imread(single['path']).astype(float) - tifffile.imread(plain)).max(), 500)
                with tifffile.TiffFile(single['path']) as output:
                    self.assertEqual(output.pages[0].tags[34675].value,
                        server.color_pipeline.required_icc_bytes('prophoto'))

    def test_damaged_required_ai_component_blocks_only_the_affected_export_selection(self):
        bad = {'id': 'accepted', 'name': 'Sky refinement', 'type': 'radial', 'components': [
            {'type': 'radial'}, {'type': 'sky', 'combine': 'subtract', 'bitmap': {'data': 'damaged'}}]}
        name = self.qualified('a.jpg')
        self.catalog.save_state(server.catalog_image_id(name), {'masks': [bad]})
        with self.assertRaisesRegex(ValueError, 'Sky refinement.*missing or damaged'):
            server.prepare_export({'names': [name]})
        self.assertEqual(len(server.prepare_export({'names': [self.qualified('sub/b.jpg')]})[0]), 1)
        edits.require_saved_mask_assets([dict(bad, enabled=False)])

    def test_rename_preview_lists_absolute_paths_uppercase_xmp_and_unknown_companions(self):
        (self.root / 'a.XMP').write_text('accepted metadata')
        (self.root / 'a.jpg.lighttable.json').write_text('accepted recipe')
        (self.root / 'a.foreign-data').write_text('unknown companion')
        request = {'names': [self.qualified('a.jpg')], 'template': 'Renamed'}
        preview = server.rename_photos(dict(request, preview=True))
        row = preview['preview'][0]
        self.assertTrue(Path(row['sourcePath']).is_absolute())
        self.assertEqual(len(row['companions']), 3)
        self.assertTrue(any(item['target'] is None for item in row['companions']))
        self.assertTrue((self.root / 'a.jpg').exists())
        result = server.rename_photos(request)
        self.assertEqual(result['renamed'], 1)
        self.assertEqual((self.root / 'Renamed.XMP').read_text(), 'accepted metadata')
        self.assertEqual((self.root / 'Renamed.jpg.lighttable.json').read_text(), 'accepted recipe')
        self.assertEqual((self.root / 'a.foreign-data').read_text(), 'unknown companion')

    def test_metadata_collision_is_visible_before_apply(self):
        (self.root / 'a.XMP').write_text('source')
        (self.root / 'Renamed.XMP').write_text('destination')
        preview = server.rename_photos({'names': [self.qualified('a.jpg')], 'template': 'Renamed', 'preview': True})
        self.assertIn('already exists', preview['error'])
        self.assertTrue((self.root / 'a.jpg').exists())

    def test_renaming_one_capture_keeps_shared_xmp_beside_its_companion(self):
        (self.root / 'a.dng').write_bytes(b'capture partner')
        (self.root / 'a.xmp').write_text('shared metadata')
        result = server.rename_photos({'names': [self.qualified('a.jpg')], 'template': 'Renamed'})
        self.assertEqual(result['renamed'], 1)
        self.assertEqual((self.root / 'a.xmp').read_text(), 'shared metadata')
        self.assertEqual((self.root / 'Renamed.xmp').read_text(), 'shared metadata')

    def test_batch_rename_reads_each_source_directory_once(self):
        with mock.patch.object(server, 'index_photo_companions', wraps=server.index_photo_companions) as index:
            server.rename_photos({'names': [self.qualified('a.jpg'), self.qualified('sub/b.jpg')],
                                  'template': 'Renamed', 'preview': True})
        self.assertEqual(index.call_count, 2)
