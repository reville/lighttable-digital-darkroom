# SPDX-License-Identifier: GPL-3.0-only
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import app_version
import lighttable_cli


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "set_version", ROOT / "scripts/release/set-version.py")
set_version = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(set_version)

# Hard-coded fallbacks that once made unversioned builds report 0.1.0 or 1.0.
STALE_DEFAULTS = (
    ':-0.1.0}',
    '"LIGHTTABLE_VERSION", "0.1.0"',
    '$Version = "0.1.0"',
    "0.1.0-ci",
    "<key>CFBundleShortVersionString</key><string>1.0</string>",
)


class AppVersionTests(unittest.TestCase):
    def test_every_copy_agrees_and_is_not_behind_the_published_release(self):
        self.assertEqual(set_version.problems(), [])
        self.assertEqual(set_version.source_version(), app_version.VERSION)

    def test_about_api_and_cli_report_the_release_version(self):
        self.assertEqual(lighttable_cli.__version__, app_version.VERSION)
        self.assertIn('"version": app_version.VERSION,', (ROOT / "server.py").read_text())
        self.assertIn('"serverInfo": {"name": "lighttable", "version": __version__}',
                      (ROOT / "lighttable_cli/__main__.py").read_text())
        self.assertIn("<string>$(MARKETING_VERSION)</string>", (ROOT / "app/Info.plist").read_text())

    def test_builds_without_an_explicit_version_use_app_version(self):
        for relative in (
            "scripts/update-personal-app.sh",
            "scripts/build-release.sh",
            "build-app.sh",
            "scripts/linux/build-release.py",
            "scripts/windows/build-release.ps1",
            ".github/workflows/linux-build.yml",
            ".github/workflows/windows-build.yml",
        ):
            source = (ROOT / relative).read_text()
            self.assertIn("app_version.py", source, relative)
            for stale in STALE_DEFAULTS:
                self.assertNotIn(stale, source, relative)

    def test_release_tag_must_carry_the_bump(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn('git show "refs/tags/v$version:app_version.py"', workflow)

    def test_set_version_updates_sources_and_refuses_to_go_backwards(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for relative in ("app_version.py", set_version.XCODE_PROJECT,
                             *set_version.PUBLISHED_RECORDS):
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / relative, root / relative)
            major, minor, patch = set_version.version_key(app_version.VERSION)
            newer = f"{major}.{minor}.{patch + 1}"

            set_version.set_version(newer, root)
            self.assertEqual(set_version.source_version(root), newer)
            self.assertEqual(set_version.problems(root), [])
            with self.assertRaises(ValueError):
                set_version.set_version(app_version.VERSION, root)

            npm = root / "packaging/npm/package.json"
            record = json.loads(npm.read_text())
            record["version"] = f"{major}.{minor + 1}.0"
            npm.write_text(json.dumps(record))
            self.assertTrue(any("packaging/npm/package.json" in problem
                                for problem in set_version.problems(root)))


if __name__ == "__main__":
    unittest.main()
