# SPDX-License-Identifier: GPL-3.0-only
"""Exercise review/trial migration against real SQLite catalogs and source files."""
import copy
import sqlite3
from contextlib import closing
from pathlib import Path

import catalog_import
import catalog_scan
from test_catalog_import import ImportFixture


class MigrationReviewTests(ImportFixture):
    def test_preview_reports_every_photo_and_never_changes_the_library(self):
        image_id = self.image('a.jpg')
        self.catalog.save_state(image_id, {'grade': {'exposure': 1.2}, 'rating': 2})
        before = copy.deepcopy(self.catalog.state_for(image_id))
        count = self.catalog.query({'limit': 10})['total']
        original = (self.root / 'a.jpg').read_bytes()
        rows = []
        result = catalog_import.preview_import(self.catalog, self.lrcat,
            options={'conflict': 'overwrite', 'history': True}, report_photo=rows.append)
        self.assertTrue(result['previewOnly'])
        self.assertEqual(len(rows), 5)
        self.assertEqual(result['reportedPhotos'], 5)
        self.assertTrue(any(row['outcome'] == 'unmatched' for row in rows))
        self.assertTrue(any('grade' in row['mapped'] for row in rows))
        self.assertEqual(self.catalog.state_for(image_id), before)
        self.assertEqual(self.catalog.query({'limit': 10})['total'], count)
        self.assertEqual(self.catalog.collections(), [])
        self.assertEqual((self.root / 'a.jpg').read_bytes(), original)

    def test_trial_creates_independent_variants_and_preserves_original_state_dates_and_iptc(self):
        image_id = self.image('a.jpg')
        self.catalog.save_state(image_id, {'grade': {'exposure': 1.2}, 'rating': 2})
        self.catalog.save_iptc(image_id, {'title': 'Keep my original title'})
        before = copy.deepcopy(self.catalog.state_for(image_id))
        date = self.catalog.capture_details(image_id)
        result = catalog_import.import_catalog(self.catalog, self.lrcat,
            options={'trial': True, 'conflict': 'overwrite', 'history': True})
        self.assertTrue(result['trial'])
        self.assertEqual(self.catalog.state_for(image_id), before)
        self.assertEqual(self.catalog.capture_details(image_id), date)
        self.assertEqual(self.catalog.iptc_for(image_id)['title'], 'Keep my original title')
        collection = self.collection_named('Catalog import trial')
        self.assertEqual(self.catalog.connection.execute('SELECT COUNT(*) FROM collection_images WHERE collection_id=?', (collection['id'],)).fetchone()[0], 4)
        trial = next(row for row in result['photos'] if row['sourceId'] == 101)
        self.assertIn('::', trial['targetName'])
        self.assertIn('grade', trial['mapped'])

    def test_reference_grouping_requires_an_exact_relative_identity(self):
        references = self.directory / 'finished'
        (references / 'sub').mkdir(parents=True)
        ((references / 'a.jpg').resolve()).write_bytes(b'finished-a')
        # A matching basename in the wrong folder must never be paired.
        (references / 'c.jpg').write_bytes(b'wrong-folder')
        source_id = self.catalog.add_source(references)
        catalog_scan.scan_source(self.catalog, source_id)
        result = catalog_import.import_catalog(self.catalog, self.lrcat,
            options={'referenceRoot': str(references), 'stacks': False})
        a = next(row for row in result['photos'] if row['sourceId'] == 101)
        c = next(row for row in result['photos'] if row['sourceId'] == 103)
        self.assertTrue(a['reference']['grouped'])
        self.assertNotIn('reference', c)
        self.assertEqual(Path(a['reference']['path']), (references / 'a.jpg').resolve())

    def test_ambiguous_reference_outputs_are_reported_without_grouping(self):
        references = self.directory / 'finished'
        references.mkdir()
        for name in ('a.jpg', 'a.tif'):
            (references / name).write_bytes(b'finished')
        result = catalog_import.import_catalog(self.catalog, self.lrcat,
            options={'referenceRoot': str(references), 'stacks': False})
        a = next(row for row in result['photos'] if row['sourceId'] == 101)
        self.assertIn('Multiple', a['reference']['note'])
        self.assertFalse(a['reference'].get('grouped'))

    def test_local_source_alias_matches_the_same_physical_folder(self):
        alias = self.directory / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with closing(sqlite3.connect(self.lrcat)) as conn:
            conn.execute('UPDATE AgLibraryRootFolder SET absolutePath=? WHERE id_local=1', (str(alias) + '/',))
            conn.commit()
        result = catalog_import.preview_import(self.catalog, self.lrcat)
        self.assertEqual(result['matched'], 4)
        self.assertEqual(result['unmatched'], 1)
