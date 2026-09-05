"""Integration tests against real files rather than fabricated ones.

Two things can only be checked with genuine data: that the scanner reads real
camera metadata out of every RAW format the repo ships, and that the catalog
importer refuses an Adobe database whose schema it does not know instead of
crashing on it.

The RAW tests skip when `demo-assets/` has not been fetched, since those files
are Git LFS-backed and a fresh clone does not have them.
"""
from __future__ import annotations

import gzip
import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import catalog as catalog_module
import catalog_import
import catalog_scan
import server

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo-assets" / "cc0-raw" / "files"
LIGHTROOM_FIXTURE = (ROOT / "tests" / "fixtures" /
                     "lightroom-classic-15-migration.lrcat.gz")
LIGHTROOM_ASSETS = (
    ROOT / "demo-assets" / "cc0-raw" / "portrait-contact-sheet.jpg",
    ROOT / "demo-assets" / "cc0-raw" / "contact-sheet.jpg",
)


def demo_files() -> list[Path]:
    if not DEMO.is_dir():
        return []
    # An unfetched LFS pointer is a ~130 byte text file, not a photograph.
    return [path for path in sorted(DEMO.iterdir())
            if path.is_file() and not path.name.startswith(".")
            and path.stat().st_size > 100_000]


class RealRawScanTests(unittest.TestCase):
    """The scanner against real camera files of every shipped format."""

    @classmethod
    def setUpClass(cls):
        cls.files = demo_files()
        if not cls.files:
            raise unittest.SkipTest("demo-assets not fetched (Git LFS)")

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.catalog = catalog_module.Catalog(
            Path(self._dir.name) / "library.sqlite3")
        self.source = self.catalog.add_source(DEMO)

    def tearDown(self):
        self.catalog.close()
        self._dir.cleanup()

    def test_every_shipped_raw_file_is_catalogued_as_raw(self):
        result = catalog_scan.scan_source(self.catalog, self.source)
        self.assertEqual(result["added"], len(self.files))
        items = self.catalog.query({"limit": 200})["items"]
        self.assertEqual(len(items), len(self.files))
        kinds = {item["kind"] for item in items}
        self.assertEqual(kinds, {"raw"},
                         "every demo file should classify as RAW")

    def test_real_metadata_is_read_at_scan_time(self):
        catalog_scan.scan_source(self.catalog, self.source)
        items = self.catalog.query({"limit": 200})["items"]
        with_camera = [i for i in items if (i["camera"] or "").strip()]
        with_capture = [i for i in items if i["captureTime"]]
        with_size = [i for i in items if i["width"] and i["height"]]
        # Not every camera writes every field, but the great majority do; a
        # regression that broke EXIF reading entirely would show here.
        self.assertGreater(len(with_camera), len(items) * 0.8,
                           "camera make/model should be read from most files")
        self.assertGreater(len(with_capture), len(items) * 0.8,
                           "capture time should be read from most files")
        self.assertGreater(len(with_size), len(items) * 0.8,
                           "pixel dimensions should be read from most files")

    def test_camera_make_reaches_the_per_camera_default_key(self):
        """The Make field was previously never read, which broke this key."""
        from unittest import mock

        catalog_scan.scan_source(self.catalog, self.source)
        with mock.patch.object(server, "FOLDER", DEMO), \
                mock.patch.object(server, "CATALOG", None):
            identities = [server.raw_camera_identity(path.name)
                          for path in self.files[:6]]
        makes = [i["key"].split("|")[0] for i in identities]
        self.assertTrue(any(make for make in makes),
                        f"no camera make resolved; got {identities}")
        for identity in identities:
            self.assertNotEqual(identity["label"], "Unknown camera")

    def test_content_hashes_are_distinct_across_real_files(self):
        digests = {catalog_scan.header_hash(path) for path in self.files}
        self.assertEqual(len(digests), len(self.files),
                         "distinct photographs must not share a content hash")
        self.assertNotIn("", digests)

    def test_rescan_of_real_files_finds_no_changes(self):
        catalog_scan.scan_source(self.catalog, self.source)
        again = catalog_scan.scan_source(self.catalog, self.source)
        self.assertEqual((again["added"], again["updated"], again["missing"]),
                         (0, 0, 0))

    def test_every_shipped_extension_is_accepted_by_the_server(self):
        suffixes = {path.suffix.lower() for path in self.files}
        self.assertTrue(suffixes <= set(server.EXTS),
                        f"unaccepted extensions: {suffixes - set(server.EXTS)}")
        for suffix in suffixes:
            self.assertTrue(server.is_raw(f"frame{suffix}"), suffix)


class ForeignCatalogTests(unittest.TestCase):
    """An Adobe database this build does not read must refuse, not crash.

    Lightroom's cloud library keeps an opaque document store (`docs`, `revs`,
    `labels`) rather than the relational schema Lightroom Classic uses. Pointing
    the importer at one is an easy mistake to make, and the refusal has to be
    clean and leave the file alone.
    """

    def _cloud_catalog(self, directory: Path) -> Path:
        path = directory / "Managed Catalog.mcat"
        conn = sqlite3.connect(path)
        conn.executescript(
            "CREATE TABLE docs (id TEXT PRIMARY KEY, payload BLOB);"
            "CREATE TABLE revs (id INTEGER PRIMARY KEY, doc TEXT);"
            "CREATE TABLE labels (id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE TABLE schemaVersion (version INTEGER);")
        conn.execute("INSERT INTO schemaVersion VALUES (7)")
        conn.execute("INSERT INTO docs VALUES ('a', x'00')")
        conn.commit()
        conn.close()
        return path

    def test_cloud_catalog_is_refused_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._cloud_catalog(Path(directory))
            with self.assertRaises(catalog_import.UnsupportedCatalog) as caught:
                catalog_import.inspect(path)
            self.assertIn("not a Lightroom catalog", str(caught.exception))

    def test_refusal_leaves_the_source_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._cloud_catalog(Path(directory))
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaises(catalog_import.UnsupportedCatalog):
                catalog_import.inspect(path)
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),
                             before)
            self.assertEqual(
                sorted(p.name for p in Path(directory).iterdir()),
                ["Managed Catalog.mcat"],
                "no journal or copy may be left beside the source")

    def test_a_plain_non_database_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notes.lrcat"
            path.write_text("this is not a database")
            with self.assertRaises(catalog_import.UnsupportedCatalog):
                catalog_import.inspect(path)

    def test_an_empty_sqlite_database_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.lrcat"
            sqlite3.connect(path).close()
            with self.assertRaises(catalog_import.UnsupportedCatalog):
                catalog_import.inspect(path)


class PopulatedLightroomCatalogTests(unittest.TestCase):
    """End-to-end mapping from a catalog authored by Lightroom Classic 15."""

    def setUp(self):
        if not LIGHTROOM_FIXTURE.is_file() \
                or any(not path.is_file() for path in LIGHTROOM_ASSETS):
            self.skipTest("checked-in Lightroom migration fixture is missing")
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        directory = Path(self._dir.name)
        self.lrcat = directory / "fixture.lrcat"
        with gzip.open(LIGHTROOM_FIXTURE, "rb") as source, \
                self.lrcat.open("wb") as target:
            shutil.copyfileobj(source, target)

        self.photos = directory / "photos"
        self.photos.mkdir()
        for source in LIGHTROOM_ASSETS:
            shutil.copy2(source, self.photos / source.name)
        self.catalog = catalog_module.Catalog(directory / "library.sqlite3")
        self.addCleanup(self.catalog.close)
        self.source_id = self.catalog.add_source(self.photos)
        scan = catalog_scan.scan_source(self.catalog, self.source_id)
        self.assertEqual(scan["added"], 2)

    def image(self, name: str, copy_ident: str | None = None) -> int:
        image_id = self.catalog.image_id_for(
            self.source_id, name, copy_ident)
        self.assertIsNotNone(image_id, (name, copy_ident))
        return image_id

    def collection_named(self, name: str) -> dict:
        for item in self.catalog.collections():
            if item["name"] == name:
                return item
        self.fail(f"no collection named {name}")

    def import_fixture(self) -> dict:
        summary = catalog_import.inspect(self.lrcat)
        original_root = summary["roots"][0]["originalPath"]
        return catalog_import.import_catalog(
            self.catalog, self.lrcat,
            options={"history": True, "conflict": "overwrite"},
            root_map={original_root: str(self.photos.resolve())})

    def test_fixture_is_populated_and_current(self):
        summary = catalog_import.inspect(self.lrcat)
        self.assertEqual(summary["version"], "1504001")
        self.assertEqual(summary["images"], 3)
        self.assertEqual(summary["keywords"], 2)
        self.assertGreaterEqual(summary["collections"], 2)
        self.assertEqual(summary["stacks"], 1)
        self.assertTrue(summary["hasDevelopSettings"])
        self.assertTrue(summary["hasHistory"])

    def test_real_catalog_imports_every_fixture_feature(self):
        result = self.import_fixture()
        self.assertEqual(result["matched"], 3)
        self.assertEqual(result["unmatched"], 0)
        self.assertEqual(result["keywords"], 2)
        self.assertEqual(result["stacks"], 1)
        self.assertEqual(result["history"], 6)

        portrait = self.image("portrait-contact-sheet.jpg")
        state = self.catalog.state_for(portrait)
        self.assertEqual((state["rating"], state["status"], state["label"]),
                         (4, "approved", "blue"))
        self.assertAlmostEqual(state["grade"]["exposure"], 0.75, places=5)
        self.assertAlmostEqual(state["grade"]["contrast"], 0.25, places=5)
        self.assertEqual(state["crop"],
                         {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8})
        self.assertAlmostEqual(state["optics"]["rotate"], 2.5, places=5)
        self.assertEqual(self.catalog.keywords_for(portrait),
                         ["Migration Fixture > Portrait"])

        iptc = self.catalog.iptc_for(portrait)
        self.assertEqual(iptc["title"],
                         "Migration fixture portrait sheet")
        self.assertEqual(iptc["caption"],
                         "A genuine catalog migration fixture")
        self.assertEqual(iptc["creator"], "LightTable Test Suite")
        self.assertEqual(iptc["copyright"], "CC0 source images")
        self.assertEqual(iptc["city"], "Boston")
        self.assertAlmostEqual(iptc["gps_lat"], 42.3601, places=4)
        self.assertAlmostEqual(iptc["gps_lon"], -71.0589, places=4)

        contact = self.image("contact-sheet.jpg")
        contact_state = self.catalog.state_for(contact)
        self.assertEqual((contact_state["rating"], contact_state["status"],
                          contact_state["label"]),
                         (2, "skipped", "purple"))
        self.assertEqual(self.catalog.keywords_for(contact),
                         ["Migration Fixture"])

        copy_row = self.catalog.connection.execute(
            "SELECT copy_ident, display_name FROM images WHERE virtual=1"
        ).fetchone()
        self.assertIsNotNone(copy_row)
        self.assertEqual(copy_row["display_name"], "Migration Fixture Copy")
        copy_id = self.image("portrait-contact-sheet.jpg",
                             copy_row["copy_ident"])
        copy_state = self.catalog.state_for(copy_id)
        self.assertEqual((copy_state["rating"], copy_state["status"],
                          copy_state["label"]),
                         (5, "approved", "green"))
        self.assertAlmostEqual(copy_state["grade"]["exposure"],
                               -0.5, places=5)

        picks = self.collection_named("Migration Fixture Picks")
        self.assertEqual((picks["type"], picks["count"]), ("regular", 3))
        rated = self.collection_named("Migration Fixture Rated")
        self.assertEqual(rated["rules"], {"ratingMin": 4})
        members = self.catalog.connection.execute(
            "SELECT COUNT(*) AS n FROM stack_images").fetchone()
        self.assertEqual(members["n"], 3)


if __name__ == "__main__":
    unittest.main()


class RealLightroomCatalogTests(unittest.TestCase):
    """Schema conformance against a genuine Lightroom Classic catalog.

    Synthetic fixtures prove the importer reads what it was told to expect.
    They cannot prove those expectations match a catalog Lightroom actually
    writes. When one is present on this machine these run; otherwise they skip,
    so the suite stays green on a machine without Lightroom.

    Read-only throughout: the catalog is copied before it is opened, and the
    test asserts the original is untouched.
    """

    CANDIDATES = (
        Path.home() / "Pictures/Lightroom/Lightroom Catalog.lrcat",
    )

    # Every table the importer reads, with the columns it depends on.
    EXPECTED = {
        "AgLibraryRootFolder": ("absolutePath",),
        "AgLibraryFolder": ("pathFromRoot", "rootFolder"),
        "AgLibraryFile": ("baseName", "extension", "folder"),
        "Adobe_images": ("rating", "pick", "colorLabels", "captureTime",
                         "masterImage", "copyName", "fileFormat"),
        "AgLibraryKeyword": ("name", "parent"),
        "AgLibraryKeywordImage": ("image", "tag"),
        "AgLibraryCollection": ("name", "creationId"),
        "AgLibraryCollectionImage": ("collection", "image"),
        "AgLibraryIPTC": ("caption",),
        "AgHarvestedExifMetadata": ("gpsLatitude", "gpsLongitude"),
        "Adobe_imageDevelopSettings": ("text",),
        "Adobe_AdditionalMetadata": ("xmp",),
    }

    @classmethod
    def setUpClass(cls):
        cls.catalog = next((p for p in cls.CANDIDATES if p.is_file()), None)
        if cls.catalog is None:
            raise unittest.SkipTest("no Lightroom Classic catalog on this Mac")

    def test_inspect_reads_a_real_catalog(self):
        result = catalog_import.inspect(self.catalog)
        self.assertIsInstance(result.get("version"), (str, int))
        for key in ("images", "keywords", "collections", "stacks"):
            self.assertIsInstance(result.get(key), int, key)

    def test_inspection_does_not_touch_the_original(self):
        before = hashlib.sha256(self.catalog.read_bytes()).hexdigest()
        siblings = sorted(p.name for p in self.catalog.parent.iterdir())
        catalog_import.inspect(self.catalog)
        self.assertEqual(hashlib.sha256(self.catalog.read_bytes()).hexdigest(),
                         before)
        self.assertEqual(sorted(p.name for p in self.catalog.parent.iterdir()),
                         siblings, "no copy or journal may be left behind")

    def test_every_table_the_importer_reads_exists(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "catalog.lrcat"
            shutil.copy2(self.catalog, copy)
            conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
            try:
                tables = {row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                missing = sorted(set(self.EXPECTED) - tables)
                self.assertEqual(missing, [],
                                 f"Lightroom no longer has: {missing}")
                for table, columns in self.EXPECTED.items():
                    present = {row[1] for row in
                               conn.execute(f'PRAGMA table_info("{table}")')}
                    absent = [c for c in columns if c not in present]
                    self.assertEqual(absent, [], f"{table} lost {absent}")
            finally:
                conn.close()
