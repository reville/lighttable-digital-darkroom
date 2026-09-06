"""Run setup lifecycle regression tests with the standard Python test suite."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class FirstRunTests(unittest.TestCase):
    def test_setup_lifecycle(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["node", "--test", "tests/first-run.test.mjs"], cwd=root,
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

class FirstRunLightroomTests(unittest.TestCase):
    def setUp(self):
        import gzip
        import tempfile
        import catalog
        import catalog_import
        from tests.test_real_assets import LIGHTROOM_FIXTURE, LIGHTROOM_ASSETS
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.photos = self.root / 'photos'
        self.photos.mkdir()
        for photo in LIGHTROOM_ASSETS:
            shutil.copy2(photo, self.photos / photo.name)
        self.lrcat = self.root / 'sample.lrcat'
        self.lrcat.write_bytes(gzip.decompress(LIGHTROOM_FIXTURE.read_bytes()))
        self.source_bytes = self.lrcat.read_bytes()
        self.catalog = catalog.Catalog(self.root / 'library.sqlite3')
        self.addCleanup(self.catalog.close)
        original = catalog_import.inspect(self.lrcat)['roots'][0]['originalPath']
        self.root_map = {original: str(self.photos)}

    def run_import(self, **kwargs):
        import catalog_import
        return catalog_import.import_catalog(
            self.catalog, self.lrcat, root_map=self.root_map, **kwargs)

    def test_opt_in_import_populates_empty_catalog_without_scanning_unrelated_files(self):
        extra = self.photos / 'unrelated.jpg'
        shutil.copy2(self.photos / 'contact-sheet.jpg', extra)
        result = self.run_import(add_sources=True)
        self.assertEqual(result['matched'], 3)
        self.assertEqual(result['unmatched'], 0)
        self.assertGreater(result['keywords'], 0)
        sources = self.catalog.sources()
        self.assertEqual(len(sources), 1)
        names = {row[0] for row in self.catalog.connection.execute('SELECT filename FROM files')}
        self.assertEqual(names, {'contact-sheet.jpg', 'portrait-contact-sheet.jpg'})
        self.assertEqual(self.lrcat.read_bytes(), self.source_bytes)
        self.assertEqual(self.run_import(add_sources=True)['matched'], 3)
        self.assertEqual(len(self.catalog.sources()), 1)
        self.assertEqual(self.catalog.connection.execute('SELECT COUNT(*) FROM files').fetchone()[0], 2)

    def test_default_import_still_requires_preexisting_sources(self):
        result = self.run_import()
        self.assertEqual(result['matched'], 0)
        self.assertEqual(self.catalog.sources(), [])

    def test_unavailable_original_is_reported_while_present_files_import(self):
        (self.photos / 'contact-sheet.jpg').unlink()
        result = self.run_import(add_sources=True)
        self.assertGreater(result['matched'], 0)
        self.assertGreater(result['unmatched'], 0)
        self.assertEqual(self.lrcat.read_bytes(), self.source_bytes)
