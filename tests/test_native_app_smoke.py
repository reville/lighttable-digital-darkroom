# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "native-app-smoke.py"
SPEC = importlib.util.spec_from_file_location("native_app_smoke", SCRIPT)
assert SPEC and SPEC.loader
SMOKE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SMOKE)


class NativeAppSmokeTests(unittest.TestCase):
    def pr_payload(self):
        steps = [{"name": name, "durationMs": index + 1}
                 for index, name in enumerate(SMOKE.REQUIRED_PR_STEPS)]
        steps[0]["render"] = {
            "presentation": "native-metal", "totalMs": 12.0, "nativeGpuMs": 2.0,
        }
        return {
            "image": "second.jpg",
            "requestedWidth": 1100,
            "windowConnected": True,
            "screenshot": {"path": "/tmp/journey.png"},
            "journey": {
                "startImage": "first.jpg",
                "navigatedImage": "second.jpg",
                "correctedPresentation": "native-metal",
                "returnPresentation": "native-metal",
                "samplingPickers": {
                    "pointColor": True,
                    "maskColor": True,
                },
                "visibilityControls": {
                    "clipping": True,
                    "whiteBalance": True,
                    "webglFallback": False,
                },
                "steps": steps,
                "export": {"total": 1, "done": 1},
                "finalState": {
                    "current": "second.jpg",
                    "render": {
                        "state": "ready",
                        "name": "second.jpg",
                        "backend": "native-metal",
                    },
                },
            },
            "renders": [
                {"presentation": "native-metal", "nativeGpuMs": 3.2,
                 "totalMs": 14.0}
            ],
        }

    def test_real_photo_fixtures_are_available_without_git_lfs(self):
        for fixture in SMOKE.DEFAULT_FIXTURES:
            self.assertTrue(fixture.is_file(), fixture)
            self.assertGreater(fixture.stat().st_size, 100_000, fixture)

    def test_curated_raw_layer_covers_every_supported_format(self):
        fixtures = SMOKE.fixtures_for_layer("raw-curated")
        extensions = {path.suffix.lower().lstrip(".") for path in fixtures}
        self.assertEqual(extensions, SMOKE.RAW_EXTENSIONS)
        self.assertEqual(len(fixtures), 8)

    def test_success_requires_a_real_native_metal_presentation(self):
        summary = SMOKE.validate_benchmark(
            self.pr_payload(),
            require_journey=True,
        )
        self.assertEqual(summary["presentation"], "native-metal")
        self.assertEqual(summary["renders"], 1)
        self.assertTrue(summary["windowConnected"])

    def test_performance_summary_preserves_step_and_render_distributions(self):
        summary = SMOKE.performance_summary(self.pr_payload(), 1234.5678)
        self.assertEqual(summary["processWallMs"], 1234.568)
        self.assertEqual(len(summary["steps"]), len(SMOKE.REQUIRED_PR_STEPS))
        self.assertEqual(summary["render"]["totalMs"]["count"], 2)
        self.assertEqual(summary["render"]["nativeGpuMs"]["median"], 2.6)

    def test_missing_or_non_native_presentations_fail(self):
        with self.assertRaisesRegex(RuntimeError, "connected a visible app window"):
            SMOKE.validate_benchmark({"renders": []})
        with self.assertRaisesRegex(RuntimeError, "produced no render"):
            SMOKE.validate_benchmark({"windowConnected": True, "renders": []})
        with self.assertRaisesRegex(RuntimeError, "expected only native-metal"):
            SMOKE.validate_benchmark(
                {"windowConnected": True,
                 "renders": [{"presentation": "webgl"}]}
            )
        with self.assertRaisesRegex(RuntimeError, "reported errors"):
            SMOKE.validate_benchmark(
                {
                    "windowConnected": True,
                    "renders": [
                        {"presentation": "native-metal", "error": "no drawable"}
                    ]
                }
            )
        with self.assertRaisesRegex(RuntimeError, "did not report its user steps"):
            SMOKE.validate_benchmark(
                {"windowConnected": True,
                 "renders": [{"presentation": "native-metal"}]},
                require_journey=True,
            )

    def test_raw_layer_requires_format_coverage_and_corrupt_recovery(self):
        payload = self.pr_payload()
        payload["journey"] = {
            "images": list(SMOKE.CURATED_RAW_NAMES),
            "steps": [],
            "recovery": {
                "corrupt": "zz-corrupt-recovery.DNG",
                "recoveredImage": SMOKE.CURATED_RAW_NAMES[0],
            },
            "finalState": {
                "current": SMOKE.CURATED_RAW_NAMES[0],
                "render": {
                    "state": "ready",
                    "name": SMOKE.CURATED_RAW_NAMES[0],
                    "backend": "native-metal",
                },
            },
        }
        summary = SMOKE.validate_benchmark(
            payload, require_journey=True, layer="raw-curated")
        self.assertEqual(summary["journey"]["recovery"]["corrupt"],
                         "zz-corrupt-recovery.DNG")

    def test_native_shell_enables_the_scripted_photo_journey(self):
        shell = (ROOT / "app" / "main.swift").read_text()
        native = (ROOT / "app" / "NativePreview.swift").read_text()
        browser = (ROOT / "web" / "app.js").read_text()
        self.assertIn("LIGHTTABLE_NATIVE_JOURNEY_LAYER", shell)
        self.assertIn("LIGHTTABLE_NATIVE_JOURNEY_SCREENSHOT", shell)
        self.assertIn("if !benchmarkRequested", shell)
        self.assertIn('window.setFrameAutosaveName("LightTableWindow")', shell)
        self.assertIn("runNativeProductJourney(width, journeyLayer)", browser)
        self.assertIn("executeUICommand('filmToggle')", browser)
        self.assertIn("executeUICommand('slider'", browser)
        self.assertIn("executeUICommand('compare')", browser)
        self.assertIn("executeUICommand('zoomActual')", browser)
        self.assertIn("waitForJourneyExport()", browser)
        self.assertIn("runNativeRawJourney(width, layer)", browser)
        self.assertIn("func snapshot() -> NSImage?", native)
        self.assertIn("captureBenchmarkScreenshot", shell)


if __name__ == "__main__":
    unittest.main()
