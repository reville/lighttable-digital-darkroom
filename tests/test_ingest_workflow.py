# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import ast
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import ingest_workflow as ingest


def _photo(path: Path, colour, *, capture: str = "", make: str = "",
           model: str = "", mtime: float | None = None) -> Path:
    """Write one small real JPEG, optionally with EXIF capture time and camera."""
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (24, 16), colour)
    exif = image.getexif()
    if make:
        exif[271] = make
    if model:
        exif[272] = model
    if capture:
        exif.get_ifd(0x8769)[36867] = capture
    image.save(path, "JPEG", exif=exif.tobytes())
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _card(root: Path) -> Path:
    """A card holding three photos plus files ingest has to ignore."""
    folder = root / "DCIM" / "100TEST"
    _photo(folder / "IMG_0001.JPG", (200, 60, 40), capture="2026-07-04 08:09:10",
           make="Canon", model="Canon EOS R5")
    _photo(folder / "IMG_0002.JPG", (40, 160, 90), mtime=1_600_000_000)
    (folder / "IMG_0003.CR2").write_bytes(b"RAW\x00" + b"\x11" * 4096)
    os.utime(folder / "IMG_0003.CR2", (1_700_000_000, 1_700_000_000))
    _photo(folder / "._IMG_0001.JPG", (1, 2, 3))  # AppleDouble dotfile
    (folder / "NOTES.TXT").write_text("card notes")
    _photo(root / "MISC" / "outside.JPG", (10, 10, 10))  # not under DCIM
    return root


def _request(destination: Path, **overrides) -> dict:
    request = {"destination": str(destination), "verify": "hash"}
    request.update(overrides)
    return request


class ScanTests(unittest.TestCase):
    def test_scan_prefers_dcim_and_lists_only_photos(self):
        with tempfile.TemporaryDirectory() as directory:
            items = ingest.scan_source(_card(Path(directory)))

            self.assertEqual([item["name"] for item in items],
                             ["IMG_0001.JPG", "IMG_0002.JPG", "IMG_0003.CR2"])
            first, second, third = items
            self.assertEqual(first["captureTime"], "2026-07-04T08:09:10")
            self.assertEqual(first["camera"], "Canon EOS R5")
            self.assertEqual(second["camera"], "")
            self.assertEqual(second["captureTime"][:4], "2020")  # mtime fallback
            self.assertEqual(third["ext"], ".cr2")
            self.assertEqual(third["size"], 4100)
            self.assertEqual(len(third["hash"]), 32)

    def test_scan_walks_the_whole_tree_without_dcim_and_honours_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _photo(root / "trip" / "a.JPG", (5, 5, 5))
            _photo(root / "trip" / "deep" / "b.jpeg", (6, 6, 6))
            (root / ".hidden").mkdir()
            _photo(root / ".hidden" / "c.jpg", (7, 7, 7))

            names = [item["name"] for item in ingest.scan_source(root)]
            self.assertEqual(names, ["a.JPG", "b.jpeg"])
            self.assertEqual(len(ingest.scan_source(root, limit=1)), 1)


class ExtensionTests(unittest.TestCase):
    def test_photo_extensions_match_the_set_the_library_browses(self):
        """A card must offer exactly what the library can open afterwards.

        This imports the module rather than text-parsing it, so composing the
        set from parts does not turn the check into a silent skip. The wider
        five-language comparison lives in
        `test_performance_contracts.ExtensionListContractTests`.
        """
        import server

        self.assertEqual(set(ingest.PHOTO_EXTENSIONS), set(server.EXTS))


class HeaderHashTests(unittest.TestCase):
    def test_header_hash_covers_the_size_and_the_first_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "a.bin"
            base.write_bytes(b"HEADER12" + b"a" * 100)
            same_header = root / "b.bin"
            same_header.write_bytes(b"HEADER12" + b"b" * 100)
            longer = root / "c.bin"
            longer.write_bytes(b"HEADER12" + b"a" * 101)
            other_header = root / "d.bin"
            other_header.write_bytes(b"DIFFERNT" + b"a" * 100)

            digest = ingest.header_hash(base, chunk=8)
            self.assertEqual(len(digest), 32)
            self.assertEqual(digest, ingest.header_hash(same_header, chunk=8))
            self.assertNotEqual(digest, ingest.header_hash(longer, chunk=8))
            self.assertNotEqual(digest, ingest.header_hash(other_header, chunk=8))
            # The whole file is read at the default chunk, so those two differ.
            self.assertNotEqual(ingest.header_hash(base),
                                ingest.header_hash(same_header))


class PlanRequestTests(unittest.TestCase):
    def test_plan_request_is_bounded_and_validated(self):
        request = ingest.clean_plan_request({
            "destination": "  /photos/library  ", "folderTemplate": "   ",
            "filenameTemplate": "", "custom": "  summer   trip  ",
            "startNumber": 0, "verify": "trust me", "onDuplicate": "destroy",
            "backupDestination": "   ", "selected": ["a.JPG", "a.JPG", "b.JPG"],
            "preset": "", "metadataPreset": "not a dict",
        })
        self.assertEqual(request["destination"], "/photos/library")
        self.assertEqual(request["folderTemplate"], ingest.FOLDER_TEMPLATE)
        self.assertEqual(request["filenameTemplate"], ingest.FILENAME_TEMPLATE)
        self.assertEqual(request["custom"], "summer trip")
        self.assertEqual(request["startNumber"], 1)
        self.assertEqual(request["verify"], "hash")
        self.assertEqual(request["onDuplicate"], "skip")
        self.assertIsNone(request["backupDestination"])
        self.assertEqual(request["selected"], ["a.JPG", "b.JPG"])
        self.assertIsNone(request["preset"])
        self.assertIsNone(request["metadataPreset"])
        self.assertFalse(request["eject"])
        self.assertIsNone(ingest.clean_plan_request(None)["selected"])

    def test_explicit_nulls_fall_back_to_the_defaults(self):
        # str(None) is "none", which is a real verify mode: it must not win.
        request = ingest.clean_plan_request({
            "verify": None, "onDuplicate": None, "folderTemplate": None,
            "filenameTemplate": None, "custom": None, "startNumber": None,
        })
        self.assertEqual(request["verify"], "hash")
        self.assertEqual(request["onDuplicate"], "skip")
        self.assertEqual(request["folderTemplate"], ingest.FOLDER_TEMPLATE)
        self.assertEqual(request["filenameTemplate"], ingest.FILENAME_TEMPLATE)
        self.assertEqual(request["custom"], "")
        self.assertEqual(request["startNumber"], 1)


class TemplateTests(unittest.TestCase):
    def _context(self, **overrides):
        item = {"name": "IMG_0007.CR2", "camera": "Canon EOS R5",
                "captureTime": "2026-07-04T08:09:10"}
        item.update(overrides)
        return ingest.template_context(item, 7, "beach")

    def test_folder_and_filename_tokens_render(self):
        context = self._context()
        self.assertEqual(ingest.render_path("{yyyy}/{yyyy}-{mm}-{dd}", context),
                         "2026/2026-07-04")
        self.assertEqual(
            ingest.render_path("{yy}{mm}{dd}_{hh}{min}{ss}_{custom}_{sequence}",
                               context),
            "260704_080910_beach_0007")
        self.assertEqual(ingest.render_path("{camera}/{filename}", context),
                         "Canon EOS R5/IMG_0007")

    def test_unknown_tokens_keep_their_name_and_empty_ones_drop_out(self):
        context = self._context(camera="")
        self.assertEqual(ingest.render_path("{filename}_{bogus}", context),
                         "IMG_0007_bogus")
        self.assertEqual(ingest.render_path("{filename}_{camera}", context),
                         "IMG_0007")
        self.assertEqual(ingest.render_path("", context), "")

    def test_a_token_cannot_introduce_a_separator_or_escape_upwards(self):
        context = self._context(camera="a/b\\c", name="IMG_0007.CR2")
        context["custom"] = "../.."
        rendered = ingest.render_path("{custom}/{camera}/{filename}", context)

        self.assertNotIn("..", rendered)
        # The escaping value collapses away and the separators become one name.
        self.assertEqual(rendered, "a_b_c/IMG_0007")
        self.assertEqual(ingest.render_path("{camera}", self._context(
            camera="../../etc")), "etc")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual((root / rendered).resolve().parts[:len(root.parts)],
                             root.parts)


class PlanTests(unittest.TestCase):
    def test_plan_covers_only_photos_in_capture_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = ingest.scan_source(_card(root / "card"))
            destination = (root / "library").resolve()

            plan = ingest.build_plan(items, _request(destination))

            self.assertEqual(plan["total"], 3)
            self.assertEqual(plan["duplicates"], 0)
            self.assertEqual(plan["skipped"], [])
            self.assertEqual(plan["bytes"], sum(item["size"] for item in items))
            # 2020 mtime, then 2023 mtime, then the 2026 EXIF capture.
            self.assertEqual([entry["name"] for entry in plan["items"]],
                             ["IMG_0002.JPG", "IMG_0003.CR2", "IMG_0001.JPG"])
            self.assertEqual([entry["sequence"] for entry in plan["items"]],
                             [1, 2, 3])
            self.assertEqual(Path(plan["items"][-1]["destination"]),
                             destination / "2026" / "2026-07-04" / "IMG_0001.JPG")
            for entry in plan["items"]:
                self.assertIsNone(entry["backup"])
                self.assertEqual(len(entry["hash"]), 32)

    def test_selection_templates_and_backup_shape_the_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = ingest.scan_source(_card(root / "card"))
            chosen = [item for item in items if item["name"] == "IMG_0001.JPG"]

            plan = ingest.build_plan(items, _request(
                root / "library", selected=[chosen[0]["path"]],
                folderTemplate="{yyyy}/{custom}",
                filenameTemplate="{custom}_{yyyy}{mm}{dd}_{sequence}",
                custom="Iceland", startNumber=41,
                backupDestination=str(root / "backup")))

            self.assertEqual(plan["total"], 1)
            entry = plan["items"][0]
            self.assertEqual(Path(entry["destination"]),
                             (root / "library").resolve() / "2026" / "Iceland" /
                             "Iceland_20260704_0041.JPG")
            self.assertEqual(Path(entry["backup"]),
                             (root / "backup").resolve() / "2026" / "Iceland" /
                             "Iceland_20260704_0041.JPG")

    def test_templates_cannot_write_outside_the_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = ingest.scan_source(_card(root / "card"))
            library = (root / "library").resolve()

            plan = ingest.build_plan(items, _request(
                library, folderTemplate="{custom}/{yyyy}",
                filenameTemplate="{custom}", custom="../../escape"))

            self.assertEqual(plan["total"], 3)
            for entry in plan["items"]:
                path = Path(entry["destination"]).resolve()
                self.assertNotIn("..", entry["destination"])
                self.assertEqual(path.parts[:len(library.parts)], library.parts)

    def test_duplicates_are_skipped_or_copied_by_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = ingest.scan_source(_card(root / "card"))
            known = {items[0]["hash"]}

            skipping = ingest.build_plan(items, _request(root / "library"),
                                         existing_hashes=known)
            self.assertEqual(skipping["total"], 2)
            self.assertEqual(skipping["duplicates"], 1)
            self.assertEqual(len(skipping["skipped"]), 1)
            self.assertEqual(skipping["skipped"][0]["name"], items[0]["name"])
            self.assertEqual(skipping["skipped"][0]["reason"], "duplicate")
            self.assertNotIn(items[0]["name"],
                             [entry["name"] for entry in skipping["items"]])

            copying = ingest.build_plan(
                items, _request(root / "library", onDuplicate="copy"),
                existing_hashes=known)
            self.assertEqual(copying["total"], 3)
            self.assertEqual(copying["duplicates"], 1)
            self.assertEqual(copying["skipped"], [])
            flagged = [entry for entry in copying["items"] if entry["duplicate"]]
            self.assertEqual([entry["name"] for entry in flagged],
                             [items[0]["name"]])

    def test_two_sources_that_render_alike_get_distinct_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            card = root / "card" / "DCIM"
            first = _photo(card / "100TEST" / "IMG_0001.JPG", (200, 10, 10),
                           capture="2026-07-04 08:09:10")
            second = _photo(card / "101TEST" / "IMG_0001.JPG", (10, 200, 10),
                            capture="2026-07-04 09:10:11")
            items = ingest.scan_source(root / "card")
            self.assertEqual(len(items), 2)

            plan = ingest.build_plan(items, _request(root / "library"))
            destinations = [entry["destination"] for entry in plan["items"]]
            self.assertEqual(len(set(destinations)), 2)
            self.assertTrue(destinations[1].endswith("IMG_0001-2.JPG"))

            for entry in plan["items"]:
                self.assertTrue(ingest.copy_item(entry)["ok"])
            written = {Path(path).read_bytes() for path in destinations}
            self.assertEqual(written, {first.read_bytes(), second.read_bytes()})

    def test_replanning_after_an_ingest_reuses_the_same_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = ingest.scan_source(_card(root / "card"))
            request = _request(root / "library")

            first = ingest.build_plan(items, request)
            for entry in first["items"]:
                self.assertTrue(ingest.copy_item(entry)["ok"])
            second = ingest.build_plan(ingest.scan_source(root / "card"), request)

            self.assertEqual([entry["destination"] for entry in second["items"]],
                             [entry["destination"] for entry in first["items"]])

    def test_a_blank_destination_is_refused(self):
        with self.assertRaises(ValueError):
            ingest.build_plan([], {"destination": "  "})


class CopyTests(unittest.TestCase):
    def _one_item(self, root: Path, **overrides):
        source = _photo(root / "card" / "DCIM" / "100TEST" / "IMG_0001.JPG",
                        (30, 90, 200), capture="2026-07-04 08:09:10")
        items = ingest.scan_source(root / "card")
        plan = ingest.build_plan(items, _request(root / "library", **overrides))
        return source, plan["items"][0]

    def test_copy_verifies_and_reports_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, entry = self._one_item(root)

            result = ingest.copy_item(entry)

            self.assertTrue(result["ok"])
            self.assertIsNone(result["error"])
            self.assertEqual(result["bytes"], source.stat().st_size)
            self.assertEqual(Path(result["destination"]).read_bytes(),
                             source.read_bytes())
            self.assertIsNone(result["backup"])

    def test_a_failed_verification_removes_the_copy_and_keeps_the_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, entry = self._one_item(root)
            original = source.read_bytes()

            def corrupt(_source, temporary):
                # Same length, different bytes: only a content check catches it.
                temporary.write_bytes(b"\x00" * len(original))
                return len(original)

            with mock.patch.object(ingest, "_copy_bytes", corrupt):
                result = ingest.copy_item(entry, verify="hash")

            destination = Path(entry["destination"])
            self.assertFalse(result["ok"])
            self.assertIn("do not match", result["error"])
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_name(
                destination.name + ".part").exists())
            self.assertTrue(source.is_file())
            self.assertEqual(source.read_bytes(), original)

    def test_a_truncated_copy_fails_size_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, entry = self._one_item(root)

            def truncate(source_path, temporary):
                temporary.write_bytes(source_path.read_bytes()[:-10])
                return 0

            with mock.patch.object(ingest, "_copy_bytes", truncate):
                result = ingest.copy_item(entry, verify="size")

            self.assertFalse(result["ok"])
            self.assertIn("bytes", result["error"])
            self.assertFalse(Path(entry["destination"]).exists())
            self.assertTrue(source.is_file())

    def test_copy_is_resumable_and_writes_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, entry = self._one_item(root)
            real = ingest._copy_bytes
            calls = []

            def counted(source_path, temporary):
                calls.append(str(source_path))
                return real(source_path, temporary)

            with mock.patch.object(ingest, "_copy_bytes", counted):
                first = ingest.copy_item(entry)
                second = ingest.copy_item(entry)

            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertEqual(calls, [str(source)])
            self.assertEqual(Path(entry["destination"]).read_bytes(),
                             source.read_bytes())

    def test_both_sidecar_naming_forms_travel_with_the_photo(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            card = root / "card" / "DCIM" / "100TEST"
            source = card / "IMG_0001.CR2"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"RAW\x00" + b"\x22" * 2048)
            (card / "IMG_0001.xmp").write_text("<x>plain form</x>")
            (card / "IMG_0001.CR2.xmp").write_text("<x>full form</x>")

            plan = ingest.build_plan(ingest.scan_source(root / "card"),
                                     _request(root / "library"))
            entry = plan["items"][0]
            result = ingest.copy_item(entry)

            destination = Path(entry["destination"])
            self.assertTrue(result["ok"], result["error"])
            self.assertEqual(destination.with_suffix(".xmp").read_text(),
                             "<x>plain form</x>")
            self.assertEqual(Path(f"{destination}.xmp").read_text(),
                             "<x>full form</x>")

    def test_a_backup_destination_receives_a_second_verified_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, entry = self._one_item(
                root, backupDestination=str(root / "backup"))

            result = ingest.copy_item(entry)

            self.assertTrue(result["ok"], result["error"])
            backup = Path(result["backup"])
            self.assertEqual(backup.read_bytes(), source.read_bytes())
            self.assertTrue(backup.is_relative_to((root / "backup").resolve()))

    def test_a_destination_equal_to_the_source_is_never_rewritten(self):
        # Ingesting a card onto itself must not open one file for read and
        # write and truncate the original.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _photo(root / "IMG_0001.JPG", (90, 30, 30))
            original = source.read_bytes()

            for mode in ingest.VERIFY_MODES:
                result = ingest.copy_item({"source": str(source),
                                           "destination": str(source)},
                                          verify=mode)
                self.assertTrue(result["ok"], result["error"])
                self.assertEqual(source.read_bytes(), original)

    def test_a_missing_source_reports_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = ingest.copy_item({"source": str(root / "gone.JPG"),
                                       "destination": str(root / "out.JPG")})
            self.assertFalse(result["ok"])
            self.assertIn("gone.JPG", result["error"])
            self.assertFalse((root / "out.JPG").exists())
            self.assertEqual(ingest.copy_item({})["error"],
                             "ingest item is missing a source or a destination")

    def test_an_unreadable_card_never_destroys_an_earlier_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, entry = self._one_item(root)
            self.assertTrue(ingest.copy_item(entry)["ok"])
            source.unlink()  # the card is pulled before a rerun finishes

            result = ingest.copy_item(entry)

            self.assertFalse(result["ok"])
            self.assertTrue(Path(entry["destination"]).is_file())

    def test_a_file_appearing_after_planning_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, entry = self._one_item(root)
            destination = Path(entry["destination"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"another process won this name")

            result = ingest.copy_item(entry)

            self.assertFalse(result["ok"])
            self.assertIn("destination appeared or changed", result["error"])
            self.assertEqual(destination.read_bytes(),
                             b"another process won this name")

    def test_a_publish_race_preserves_the_winner_and_cleans_the_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, entry = self._one_item(root)
            destination = Path(entry["destination"])
            real_copy = ingest._copy_bytes

            def raced(source, temporary):
                count = real_copy(source, temporary)
                destination.write_bytes(b"raced-in file")
                return count

            with mock.patch.object(ingest, "_copy_bytes", raced):
                result = ingest.copy_item(entry)

            self.assertFalse(result["ok"])
            self.assertEqual(destination.read_bytes(), b"raced-in file")
            self.assertFalse(destination.with_name(
                destination.name + ".part").exists())


if __name__ == "__main__":
    unittest.main()
