# SPDX-License-Identifier: GPL-3.0-only
"""Every first-party source file states the license it is under."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "add_license_headers", ROOT / "scripts/add-license-headers.py")
headers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(headers)


class LicenseHeaderTests(unittest.TestCase):
    def test_every_first_party_source_file_has_a_header(self):
        if not headers.tracked_sources():
            self.skipTest("git is unavailable; cannot enumerate tracked sources")
        absent = headers.missing()
        self.assertEqual(absent, [],
                         "Missing a license header; run scripts/add-license-headers.py:\n  "
                         + "\n  ".join(absent))

    def test_vendored_upstream_code_is_left_alone(self):
        sources = headers.tracked_sources()
        if not sources:
            self.skipTest("git is unavailable; cannot enumerate tracked sources")
        vendored = [str(path.relative_to(ROOT)) for path in sources
                    if str(path.relative_to(ROOT)).startswith(headers.EXCLUDED_PREFIXES)]
        self.assertEqual(vendored, [],
                         "Vendored upstream code must stay outside the tagged set.")
        engine = ROOT / "rust-engine/vendor/spektrafilm/crates/spektrafilm-core/src"
        if engine.is_dir():
            self.assertTrue(any(engine.rglob("*.rs")),
                            "expected vendored sources the exclusion applies to")

    def test_a_shebang_stays_on_the_first_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "tool.py"
            script.write_bytes(b"#!/usr/bin/env python3\nvalue = 1\n")
            self.assertTrue(headers.add_header(script))
            lines = script.read_text().splitlines()
            self.assertEqual(lines[0], "#!/usr/bin/env python3")
            self.assertIn(headers.SPDX, lines[1])
            self.assertEqual(lines[2], "value = 1")

    def test_an_encoding_declaration_stays_within_the_first_two_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = Path(temporary) / "legacy.py"
            module.write_bytes(b"# -*- coding: utf-8 -*-\nvalue = 1\n")
            self.assertTrue(headers.add_header(module))
            lines = module.read_text().splitlines()
            self.assertEqual(lines[0], "# -*- coding: utf-8 -*-")
            self.assertIn(headers.SPDX, lines[1])

    def test_windows_line_endings_survive(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "build.ps1"
            script.write_bytes(b"Write-Host 'hello'\r\n")
            self.assertTrue(headers.add_header(script))
            raw = script.read_bytes()
            self.assertNotIn(b"\r\n\n", raw)
            self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"))
            self.assertTrue(raw.startswith(b"# " + headers.SPDX.encode() + b"\r\n"))
            self.assertNotIn(b"Copyright", raw)

    def test_adding_a_header_twice_changes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = Path(temporary) / "once.rs"
            module.write_bytes(b"fn main() {}\n")
            self.assertTrue(headers.add_header(module))
            first = module.read_bytes()
            self.assertFalse(headers.add_header(module))
            self.assertEqual(module.read_bytes(), first)

    def test_an_empty_file_is_left_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "__init__.py"
            marker.write_bytes(b"")
            self.assertFalse(headers.add_header(marker))
            self.assertEqual(marker.read_bytes(), b"")


if __name__ == "__main__":
    unittest.main()
