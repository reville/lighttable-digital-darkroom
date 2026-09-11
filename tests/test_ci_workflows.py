# SPDX-License-Identifier: GPL-3.0-only
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_WORKFLOW = ROOT / ".github" / "workflows" / "windows-build.yml"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


class WindowsWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WINDOWS_WORKFLOW.read_text()
        cls.quick_check = cls.workflow.split("\n  quick-check:\n", 1)[1].split(
            "\n  package:\n", 1
        )[0]
        cls.package = cls.workflow.split("\n  package:\n", 1)[1]

    def test_pull_requests_use_the_quick_check_only(self):
        self.assertIn("pull_request:", self.workflow)
        self.assertIn("if: github.event_name == 'pull_request'", self.quick_check)
        self.assertIn("cargo check --locked", self.quick_check)
        self.assertNotIn("build-release.ps1\n          -Version", self.quick_check)
        self.assertNotIn("actions/upload-artifact", self.quick_check)

    def test_full_package_runs_after_merge_or_by_request(self):
        self.assertIn("push:\n    branches: [main]", self.workflow)
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn("if: github.event_name != 'pull_request'", self.package)
        self.assertIn("./scripts/windows/build-release.ps1", self.package)
        self.assertIn("actions/upload-artifact@v4", self.package)

    def test_superseded_runs_are_cancelled_and_builds_are_cached(self):
        self.assertIn("cancel-in-progress: true", self.workflow)
        self.assertIn("github.event.pull_request.number || github.ref", self.workflow)
        self.assertGreaterEqual(self.workflow.count("uses: actions/cache@v4"), 2)
        self.assertIn("rust-engine/target", self.quick_check)
        self.assertIn("rust-engine/target", self.package)


class MacReleaseWorkflowContractTests(unittest.TestCase):
    def test_real_photo_native_smoke_runs_before_notarization(self):
        workflow = RELEASE_WORKFLOW.read_text()
        self.assertNotIn("lfs: true", workflow)
        self.assertIn("ref: ${{ needs.prepare.outputs.tag }}", workflow)
        smoke = workflow.index(
            "python3 scripts/native-app-smoke.py --app dist/LightTable.app"
        )
        self.assertIn("--layer package", workflow)
        notarize = workflow.index("scripts/notarize-app.sh dist/LightTable.app")
        self.assertNotIn("gh release create", workflow)
        self.assertNotIn("    tags:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertLess(smoke, notarize)
        self.assertIn("macos-release-proof.json", workflow)


if __name__ == "__main__":
    unittest.main()
