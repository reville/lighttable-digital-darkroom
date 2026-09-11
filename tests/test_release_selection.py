# SPDX-License-Identifier: GPL-3.0-only
"""Run the real workflow's version/platform selection against an isolated Git repo."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReleaseSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        for arguments in (("init", "--quiet"), ("config", "user.email", "tests@example.invalid"),
                          ("config", "user.name", "Release tests"),
                          ("-c", "commit.gpgsign=false", "commit", "--allow-empty", "--quiet", "-m", "selected"),
                          ("tag", "v0.5.0")):
            subprocess.run(["git", *arguments], cwd=self.repo, check=True, capture_output=True)
        self.selected = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.repo, text=True).strip()
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        block = re.match(r"((?: {10}[^\n]*\n|\n)+)",
                         workflow.split("        run: |\n", 1)[1]).group(1)
        self.script = textwrap.dedent(block)

    def run_selection(self, platforms, version="0.5.0"):
        output = self.repo / "outputs"
        output.unlink(missing_ok=True)
        result = subprocess.run(["bash", "-euo", "pipefail", "-c", self.script], cwd=self.repo,
                                env=os.environ | {"REQUESTED_VERSION": version, "GITHUB_REF_NAME": "v0.5.0",
                                                  "PLATFORMS": platforms, "GITHUB_OUTPUT": str(output), "MACOS_CHANNEL": "stable",
                                                  "GITHUB_STEP_SUMMARY": str(self.repo/"summary")},
                                text=True, capture_output=True)
        fields = dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}
        return result, fields

    def test_all_and_independent_platforms_select_only_requested_builds(self):
        for selection, expected in (("all", {"linux", "macos", "windows"}),
                                    ("linux", {"linux"}),
                                    ("macos", {"macos"}), ("windows", {"windows"})):
            with self.subTest(selection=selection):
                result, fields = self.run_selection(selection)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual({name for name in ("linux", "macos", "windows") if fields[name] == "true"}, expected)
                self.assertEqual(fields["title"], "LightTable 0.5")
                self.assertEqual(fields["source_ref"], self.selected)

    def test_tag_keeps_original_source_when_main_moves(self):
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "--allow-empty", "--quiet", "-m", "later"],
                       cwd=self.repo, check=True, capture_output=True)
        result, fields = self.run_selection("linux")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(fields["source_ref"], self.selected)

    def test_missing_tag_and_invalid_selections_fail_without_creating_release_tags(self):
        for selection, version in (("unknown", "0.5.0"), ("linux", "0.5.1"), ("linux", "0.5"),
                                    ("all", "0.5.0;echo injected"), ("all", "00.5.0")):
            with self.subTest(selection=selection, version=version):
                result, _ = self.run_selection(selection, version)
                self.assertNotEqual(result.returncode, 0)
        tags = subprocess.check_output(["git", "tag"], cwd=self.repo, text=True).splitlines()
        self.assertEqual(tags, ["v0.5.0"])


if __name__ == "__main__":
    unittest.main()
