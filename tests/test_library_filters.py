# SPDX-License-Identifier: GPL-3.0-only
"""Toolbar filters retain their semantics in the browser and saved collections."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import catalog as catalog_module
import catalog_scan
import library_workflow

ROOT = Path(__file__).resolve().parents[1]


class LibraryFilterTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is unavailable')
    def test_browser_filter_behaviors(self):
        result = subprocess.run(['node', '--test', str(ROOT / 'tests/library-filters.test.mjs')],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_saved_filters_match_the_catalog_after_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photos = root / 'photos'
            photos.mkdir()
            for name in ('a.JPG', 'b.PNG', 'c.TIFF', 'd.HEIC', 'e.ARW'):
                (photos / name).write_bytes(name.encode() * 64)
            cat = catalog_module.Catalog(root / 'library.sqlite3')
            source_id = cat.add_source(photos)
            catalog_scan.scan_source(cat, source_id, read_metadata_for_new=False)
            rows = {item['filename']: item for item in cat.query()['items']}
            cat.save_state(rows['a.JPG']['id'], {'rating': 4, 'status': 'approved', 'label': 'red'})
            cat.save_state(rows['b.PNG']['id'], {'rating': 5, 'status': 'approved', 'label': 'red', 'grade': {'exposure': 1}})
            rules = {'fileTypes': ['jpeg', 'png'], 'ratingMin': 4, 'status': 'approved', 'label': 'red', 'editState': 'unedited'}
            ident = cat.add_collection('Filtered', kind='smart', rules=rules)
            cat.close()
            cat = catalog_module.Catalog(root / 'library.sqlite3')
            try:
                result = cat.query({'scope': 'collection', 'collectionId': ident})
                self.assertEqual([item['filename'] for item in result['items']], ['a.JPG'])
                for kind, expected in [('raw','e.ARW'),('heic','d.HEIC'),('tiff','c.TIFF')]:
                    result = cat.query({'filter': {'fileTypes': [kind]}})
                    self.assertEqual([item['filename'] for item in result['items']], [expected])
                self.assertEqual(cat.query({'filter': {'fileTypes': ['jpeg'], 'unrated': True}})['total'], 0)
            finally:
                cat.close()

    def test_folder_mode_preserves_saved_rules(self):
        rules = {'fileTypes': ['jpeg','raw','wrong'], 'editState':'unedited', 'unrated':True, 'label':'red'}
        cleaned = library_workflow.clean_collections([{'name':'Filtered','type':'smart','rules':rules}])[0]['rules']
        self.assertEqual(cleaned['fileTypes'], ['raw','jpeg'])
        for key in ('editState','unrated','label'):
            self.assertEqual(cleaned[key], rules[key])
