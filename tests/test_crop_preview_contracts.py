# SPDX-License-Identifier: GPL-3.0-only
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CropPreviewContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.javascript = (ROOT / "web" / "app.js").read_text()
        cls.css = (ROOT / "web" / "style.css").read_text()

    def test_crop_mode_has_an_immediate_full_frame_selection(self):
        visual = self.javascript[
            self.javascript.index("function applyCropVisualNow()"):
            self.javascript.index("const cropFrameScheduler")
        ]
        self.assertIn(
            "S.cropping ? { x: 0, y: 0, w: 1, h: 1 } : null",
            visual,
        )
        self.assertIn("if (next) requestAnimationFrame", self.javascript)

    def test_committed_crop_reframes_source_surfaces(self):
        self.assertIn("cmp.classList.toggle('crop-committed', !!crop)",
                      self.javascript)
        for selector in ("#cv", "#orig", "#referenceImg"):
            self.assertIn(f".cmp.preview-framed > {selector}", self.css)
        self.assertIn(
            "wrap.width, wrap.height, source.width, source.height, frameCrop",
            self.javascript,
        )
        self.assertIn("const frame = $('cmp').getBoundingClientRect()",
                      self.javascript)

    def test_rotation_presentation(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for rotation presentation checks")
        result = subprocess.run(
            [node, "--test", "tests/rotation-presentation.test.mjs"],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_crop_viewport_and_compare_mapping_math(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the crop geometry contract")
        functions = []
        for name in ("cropViewportSize", "previewSourceX"):
            match = re.search(
                rf"function {name}\(.*?\n\}}",
                self.javascript,
                flags=re.DOTALL,
            )
            self.assertIsNotNone(match)
            functions.append(match.group(0))
        script = "\n".join(functions) + """
const crop = {x: 0.2, y: 0.1, w: 0.5, h: 0.5};
process.stdout.write(JSON.stringify({
  landscape: cropViewportSize(1000, 800, 1200, 800, crop),
  portrait: cropViewportSize(1000, 800, 800, 1200, crop),
  middle: previewSourceX(0.5, crop),
  disabled: previewSourceX(0, crop),
}));
"""
        completed = subprocess.run(
            [node, "-e", script], check=True, capture_output=True, text=True)
        result = json.loads(completed.stdout)
        self.assertEqual(
            result["landscape"],
            {"width": 1000, "height": 666.6666666666666},
        )
        self.assertAlmostEqual(
            result["portrait"]["width"], 533.3333333333333)
        self.assertAlmostEqual(result["portrait"]["height"], 800)
        self.assertEqual(result["middle"], 0.45)
        self.assertEqual(result["disabled"], 0)


if __name__ == "__main__":
    unittest.main()
