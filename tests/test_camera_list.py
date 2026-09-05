import contextlib
import datetime
import importlib.util
import io
import json
import random
import re
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = APP_ROOT / "scripts" / "camera-list.py"


def load_script():
    """The generator is a hyphenated script, so it cannot simply be imported."""
    spec = importlib.util.spec_from_file_location("camera_list", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves a class's annotations through sys.modules, so the
    # module has to be registered before its body runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


camera_list = load_script()

try:
    LIBRAW = camera_list.load_libraw()
except camera_list.LibRawUnavailable:
    LIBRAW = None  # a bare environment: the rest of the suite still runs

HAVE_LIBRAW = LIBRAW is not None
NEEDS_LIBRAW = "no LibRaw is loadable in this environment"

# Camera strings LibRaw spells awkwardly, kept apart from the real list so this
# test says what the grouping should do rather than what it currently does.
SYNTHETIC = (
    "FujiFilm X-T5",
    "FujiFilm GFX 100S",
    "PhaseOne IQ180",
    "OM Digital Solutions OM-1",
    "BlackMagic URSA Mini Pro 4.6k",
    "Digital Bolex D16",
    "RaspberryPi HQ Camera",
    "AutelRobotics XB015",
    "PARROT Anafi",
    "DXO One",
    "GITUP GIT2",
    "JaiPulnix BB-500CL",
    "PtGrey GRAS-50S5C",
    "Canon EOS 50D",
    "Canon EOS 5D",
    "Canon EOS R5",
)

# Models that are in LibRaw 0.22 and are not going to quietly vanish. They
# cover the awkward manufacturers as well, so the page is checked end to end.
KNOWN_MODELS = (
    ("Canon", "EOS R5"),
    ("Fujifilm", "X-T5"),
    ("Hasselblad", "X2D 100C"),
    ("Leica", "M11 Monochrom"),
    ("OM System", "OM-1"),
    ("Phase One", "IQ180"),
    ("Blackmagic", "URSA Mini Pro 4.6k"),
    ("Raspberry Pi", "HQ Camera"),
    ("Sony", "ILCE-7RM5 (A7R V)"),
    ("Adobe", "Digital Negative (DNG)"),
)

VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
                 "link", "meta", "param", "source", "track", "wbr"}


class TagBalance(HTMLParser):
    """Every non-void element opened is closed, in order, exactly once."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.open_tags = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID_ELEMENTS:
            self.open_tags.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass  # <meta ... /> opens and closes in one go

    def handle_endtag(self, tag):
        if tag in VOID_ELEMENTS:
            self.errors.append(f"</{tag}> closes a void element")
        elif not self.open_tags:
            self.errors.append(f"</{tag}> with nothing open")
        elif self.open_tags[-1] != tag:
            self.errors.append(f"</{tag}> closes <{self.open_tags.pop()}>")
        else:
            self.open_tags.pop()


class AssetCollector(HTMLParser):
    """Everything the browser would have to fetch to render the page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.assets = []
        self.linked_scripts = 0
        self.linked_stylesheets = 0

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("src"):
            self.assets.append(values["src"])
            if tag == "script":
                self.linked_scripts += 1
        if tag == "link":
            if values.get("href"):
                self.assets.append(values["href"])
            if values.get("rel") == "stylesheet":
                self.linked_stylesheets += 1


class LibRawDiscoveryTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_LIBRAW, NEEDS_LIBRAW)
    def test_library_reports_a_version_and_a_plausible_camera_count(self):
        self.assertRegex(LIBRAW.version, r"^\d+\.\d+\.\d+")
        self.assertGreater(LIBRAW.version_number, 0)
        self.assertGreater(LIBRAW.count, 1000)
        self.assertEqual(LIBRAW.count, len(set(LIBRAW.cameras)))
        for name in LIBRAW.cameras:
            self.assertTrue(name.strip())
            self.assertEqual(name, name.strip())

    @unittest.skipUnless(HAVE_LIBRAW, NEEDS_LIBRAW)
    def test_the_library_that_was_read_is_recorded_and_bundled(self):
        self.assertTrue(LIBRAW.library.exists())
        bundled = camera_list.candidate_libraries()
        if bundled:
            # A system LibRaw is a different build with a different list, so
            # the bundled copy has to win whenever there is one.
            self.assertIn(LIBRAW.library, bundled)

    def test_a_library_that_is_not_libraw_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            impostor = Path(directory) / "libraw-not-really.dylib"
            impostor.write_bytes(b"not a shared library")
            self.assertIsNone(camera_list.read_library(impostor))
            with self.assertRaises(camera_list.LibRawUnavailable):
                camera_list.load_libraw(impostor)


class ManufacturerGroupingTests(unittest.TestCase):
    def test_awkward_manufacturer_spellings_are_fixed(self):
        expected = {
            "FujiFilm X-T5": ("Fujifilm", "X-T5"),
            "PhaseOne IQ180": ("Phase One", "IQ180"),
            "OM Digital Solutions OM-1": ("OM System", "OM-1"),
            "BlackMagic URSA Mini Pro 4.6k": ("Blackmagic",
                                              "URSA Mini Pro 4.6k"),
            "Digital Bolex D16": ("Digital Bolex", "D16"),
            "RaspberryPi HQ Camera": ("Raspberry Pi", "HQ Camera"),
            "AutelRobotics XB015": ("Autel Robotics", "XB015"),
            "PARROT Anafi": ("Parrot", "Anafi"),
            "DXO One": ("DxO", "One"),
            "GITUP GIT2": ("GitUp", "GIT2"),
            "JaiPulnix BB-500CL": ("JAI Pulnix", "BB-500CL"),
            "PtGrey GRAS-50S5C": ("Point Grey", "GRAS-50S5C"),
            "Canon EOS R5": ("Canon", "EOS R5"),
        }
        for name, split in expected.items():
            self.assertEqual(camera_list.split_camera(name), split, name)

    def test_a_synthetic_list_groups_and_sorts(self):
        groups = camera_list.group_cameras(SYNTHETIC)
        self.assertEqual(
            [group.name for group in groups],
            ["Autel Robotics", "Blackmagic", "Canon", "Digital Bolex", "DxO",
             "Fujifilm", "GitUp", "JAI Pulnix", "OM System", "Parrot",
             "Phase One", "Point Grey", "Raspberry Pi"])
        filed = {group.name: group.models for group in groups}
        self.assertEqual(filed["Fujifilm"], ("GFX 100S", "X-T5"))
        self.assertEqual(filed["OM System"], ("OM-1",))
        # Natural order, so the 5D comes before the 50D.
        self.assertEqual(filed["Canon"], ("EOS 5D", "EOS 50D", "EOS R5"))

    def test_every_camera_survives_grouping(self):
        groups = camera_list.group_cameras(SYNTHETIC)
        self.assertEqual(sum(len(group.models) for group in groups),
                         len(SYNTHETIC))
        self.assertEqual(len(camera_list.published_entries(
            camera_list.CameraList("0.0.0", 0, SYNTHETIC, Path("x"))),
            ), len(SYNTHETIC))

    def test_grouping_does_not_depend_on_the_order_it_is_given(self):
        """"K-1" and "K-01" reduce to the same natural key.

        Without a tiebreak the two would come out in whichever order the input
        happened to be in, and the published page would differ run to run.
        """
        forwards = camera_list.group_cameras(("Pentax K-1", "Pentax K-01"))
        backwards = camera_list.group_cameras(("Pentax K-01", "Pentax K-1"))
        self.assertEqual(forwards, backwards)
        self.assertEqual(forwards[0].models, ("K-01", "K-1"))
        shuffled = list(SYNTHETIC)
        random.Random(11).shuffle(shuffled)
        self.assertEqual(camera_list.group_cameras(shuffled),
                         camera_list.group_cameras(SYNTHETIC))

    def test_slugs_are_anchor_safe(self):
        for name in ("Phase One", "OM System", "Point Grey", "DxO"):
            self.assertRegex(camera_list.slug(name), r"^m-[a-z0-9-]+$")


class GeneratedPageTests(unittest.TestCase):
    """The page renders from the live library, not from a fixture."""

    @classmethod
    def setUpClass(cls):
        if not HAVE_LIBRAW:
            raise unittest.SkipTest(NEEDS_LIBRAW)
        cls.generated = datetime.date(2026, 9, 3)
        cls.page = camera_list.render_page(
            LIBRAW, cls.generated, camera_list.accepted_extensions())

    def test_the_page_states_the_library_version_and_the_totals(self):
        self.assertIn(LIBRAW.version, self.page)
        self.assertIn(f"{LIBRAW.count:,}", self.page)
        self.assertIn(f'content="{LIBRAW.count}"', self.page)
        self.assertIn("3 September 2026", self.page)
        self.assertIn('content="2026-09-03"', self.page)

    def test_the_page_carries_its_two_caveats(self):
        self.assertIn("necessary, not sufficient", self.page)
        self.assertIn("suffix", self.page)
        self.assertIn("Convert to DNG", self.page)

    def test_named_cameras_are_listed_under_their_manufacturer(self):
        for manufacturer, model in KNOWN_MODELS:
            with self.subTest(camera=f"{manufacturer} {model}"):
                self.assertIn(f"{manufacturer} {model}",
                              camera_list.published_entries(LIBRAW))
                self.assertIn(f'<li class="camera">{model}</li>', self.page)
                self.assertIn(f'data-manufacturer="{manufacturer}"', self.page)

    def test_the_page_is_reproducible(self):
        shuffled = list(LIBRAW.cameras)
        random.Random(7).shuffle(shuffled)
        reordered = camera_list.CameraList(
            LIBRAW.version, LIBRAW.version_number, tuple(shuffled),
            LIBRAW.library)
        self.assertEqual(
            camera_list.render_page(reordered, self.generated,
                                    camera_list.accepted_extensions()),
            self.page)

    def test_the_page_lists_every_camera_the_library_reports(self):
        reader = camera_list.PageReader()
        reader.feed(self.page)
        self.assertEqual(sorted(reader.entries),
                         sorted(camera_list.published_entries(LIBRAW)))
        self.assertEqual(reader.version, LIBRAW.version)
        self.assertEqual(reader.count, str(LIBRAW.count))

    def test_tags_are_balanced(self):
        parser = TagBalance()
        parser.feed(self.page)
        parser.close()
        self.assertEqual(parser.errors, [])
        self.assertEqual(parser.open_tags, [])

    def test_nothing_is_fetched_from_the_network(self):
        parser = AssetCollector()
        parser.feed(self.page)
        parser.close()
        for asset in parser.assets:
            with self.subTest(asset=asset):
                self.assertFalse(asset.startswith(("http://", "https://", "//")))
        self.assertEqual(parser.linked_scripts, 0)
        self.assertEqual(parser.linked_stylesheets, 0)
        self.assertNotIn("http://", self.page)
        self.assertNotIn("https://", self.page)
        self.assertNotIn("@import", self.page)


class CheckModeTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_LIBRAW, NEEDS_LIBRAW)
    def test_a_fresh_generated_page_is_up_to_date(self):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            page = Path(directory) / "cameras.html"
            camera_list.write_page(LIBRAW, page)
            with contextlib.redirect_stdout(output):
                status = camera_list.main(
                    ["--check", "--output", str(page)])
        self.assertEqual(status, 0, output.getvalue())
        self.assertIn("is current", output.getvalue())

    def test_site_output_is_explicit_without_an_environment_default(self):
        if camera_list.DEFAULT_OUTPUT is not None:
            self.skipTest("LIGHTTABLE_SITE_ROOT supplies the explicit target")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                camera_list.main([])
        self.assertEqual(raised.exception.code, 2)

    @unittest.skipUnless(HAVE_LIBRAW, NEEDS_LIBRAW)
    def test_a_stale_page_fails_with_a_diff_summary(self):
        stale = re.sub(r'<li class="camera">EOS R5</li>', "",
                       self.fresh_page(), count=1)
        stale = stale.replace(f'content="{LIBRAW.version}"',
                              'content="0.0.1-Release"')
        report = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cameras.html"
            path.write_text(stale, encoding="utf-8")
            status = camera_list.check_page(LIBRAW, path, stream=report)
        self.assertEqual(status, 1)
        summary = report.getvalue()
        self.assertIn("out of date", summary)
        self.assertIn("0.0.1-Release", summary)
        self.assertIn("+ Canon EOS R5", summary)

    @unittest.skipUnless(HAVE_LIBRAW, NEEDS_LIBRAW)
    def test_a_hand_edited_page_fails_even_with_the_right_cameras(self):
        edited = self.fresh_page().replace("Convert to DNG",
                                           "Convert to something else")
        report = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cameras.html"
            path.write_text(edited, encoding="utf-8")
            status = camera_list.check_page(LIBRAW, path, stream=report)
        self.assertEqual(status, 1)
        self.assertIn("the page text does not", report.getvalue())

    @unittest.skipUnless(HAVE_LIBRAW, NEEDS_LIBRAW)
    def test_a_missing_page_fails_rather_than_passing_quietly(self):
        report = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            status = camera_list.check_page(
                LIBRAW, Path(directory) / "absent.html", stream=report)
        self.assertEqual(status, 1)
        self.assertIn("does not exist", report.getvalue())

    @unittest.skipUnless(HAVE_LIBRAW, NEEDS_LIBRAW)
    def test_json_dumps_the_raw_list_and_writes_no_page(self):
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "cameras.json"
            page = Path(directory) / "cameras.html"
            with contextlib.redirect_stdout(io.StringIO()):
                status = camera_list.main(
                    ["--json", str(dump), "--output", str(page)])
            self.assertEqual(status, 0)
            self.assertFalse(page.exists())
            payload = json.loads(dump.read_text(encoding="utf-8"))
        self.assertEqual(payload["librawVersion"], LIBRAW.version)
        self.assertEqual(payload["cameraCount"], LIBRAW.count)
        self.assertEqual(payload["cameras"], list(LIBRAW.cameras))
        self.assertIn("Canon", payload["manufacturers"])

    def fresh_page(self):
        return camera_list.render_page(LIBRAW, datetime.date(2026, 9, 3),
                                       camera_list.accepted_extensions())


if __name__ == "__main__":
    unittest.main()
