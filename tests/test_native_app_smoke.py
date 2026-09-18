# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import importlib.util
import json
import tempfile
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

    def test_clean_profile_has_no_unexpected_exit_evidence(self):
        with tempfile.TemporaryDirectory() as name:
            temporary = Path(name)
            (temporary / "catalog").mkdir()
            (temporary / "server.log").write_text(
                "=== LightTable launch 2026-09-17T00:00:00Z port 8321 ===\nready\n"
            )
            SMOKE.validate_no_unexpected_server_exit(temporary)

    def test_missing_profile_files_are_not_evidence_of_a_crash(self):
        with tempfile.TemporaryDirectory() as name:
            SMOKE.validate_no_unexpected_server_exit(Path(name))

    def test_crash_ledger_entry_fails_the_run_with_its_exit_status(self):
        with tempfile.TemporaryDirectory() as name:
            temporary = Path(name)
            catalog = temporary / "catalog"
            catalog.mkdir()
            (catalog / "crashes.jsonl").write_text(
                json.dumps({"startedAt": 1.0, "pid": 111, "exitStatus": -11,
                            "detectedAt": 2.0}) + "\n"
            )
            with self.assertRaisesRegex(RuntimeError, "crash ledger.*exit status -11"):
                SMOKE.validate_no_unexpected_server_exit(temporary)

    def test_engine_incident_fails_the_run_but_an_app_incident_does_not(self):
        with tempfile.TemporaryDirectory() as name:
            temporary = Path(name)
            diagnostics = temporary / "catalog" / "Diagnostics"
            diagnostics.mkdir(parents=True)
            (diagnostics / "incident-app-1.json").write_text(
                json.dumps({"id": "app-1", "kind": "app"})
            )
            SMOKE.validate_no_unexpected_server_exit(temporary)
            (diagnostics / "incident-engine-2.json").write_text(
                json.dumps({"id": "engine-2", "kind": "engine", "exitStatus": 139})
            )
            with self.assertRaisesRegex(RuntimeError, "engine incident engine-2.*exit status 139"):
                SMOKE.validate_no_unexpected_server_exit(temporary)

    def test_more_than_one_launch_header_is_treated_as_a_restart(self):
        with tempfile.TemporaryDirectory() as name:
            temporary = Path(name)
            (temporary / "catalog").mkdir()
            # A restart rotates the live log; the earlier launch survives in
            # the ".1" generation (app/main.swift's ServerController.rotateLog).
            (temporary / "server.log").write_text(
                "=== LightTable launch 2026-09-17T00:00:05Z port 8321 after exit -11 ===\nready\n"
            )
            (temporary / "server.log.1").write_text(
                "=== LightTable launch 2026-09-17T00:00:00Z port 8321 ===\ncrashed\n"
            )
            with self.assertRaisesRegex(RuntimeError, "2 launches instead of 1"):
                SMOKE.validate_no_unexpected_server_exit(temporary)

    def test_all_evidence_is_named_when_several_signals_fire_together(self):
        with tempfile.TemporaryDirectory() as name:
            temporary = Path(name)
            catalog = temporary / "catalog"
            (catalog / "Diagnostics").mkdir(parents=True)
            (catalog / "crashes.jsonl").write_text(
                json.dumps({"startedAt": 1.0, "exitStatus": None, "detectedAt": 2.0}) + "\n"
            )
            (catalog / "Diagnostics" / "incident-engine-1.json").write_text(
                json.dumps({"id": "engine-1", "kind": "engine"})
            )
            (temporary / "server.log").write_text("=== LightTable launch a ===\n")
            (temporary / "server.log.1").write_text("=== LightTable launch b ===\n")
            with self.assertRaises(RuntimeError) as raised:
                SMOKE.validate_no_unexpected_server_exit(temporary)
            message = str(raised.exception)
            self.assertIn("crash ledger recorded an unclean session", message)
            self.assertIn("engine incident engine-1", message)
            self.assertIn("2 launches instead of 1", message)


if __name__ == "__main__":
    unittest.main()
