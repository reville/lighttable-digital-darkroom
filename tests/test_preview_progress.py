"""Run the browser progress and RAW refinement orchestration regressions."""
import shutil
import subprocess
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which('node'), 'Node.js required')
class PreviewProgressTests(unittest.TestCase):
    def test_preview_progress(self):
        result = subprocess.run(
            ['node', '--test', str(Path(__file__).with_name('preview-progress.test.mjs'))],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class StageReportingTests(unittest.TestCase):
    def test_reports_actual_stages_once_and_restores_context(self):
        import preview_progress
        events, nested = [], []
        preview_progress.advance(1)  # Non-preview work is silent.
        with preview_progress.reporting(events.append):
            preview_progress.advance(1)
            preview_progress.advance(2)
            preview_progress.advance(1)
            with preview_progress.reporting(nested.append):
                preview_progress.advance(3)
            preview_progress.advance(3)
        preview_progress.advance(4)
        self.assertEqual(events, [1, 2, 3])
        self.assertEqual(nested, [3])

    def test_failed_render_does_not_leak_into_next_request(self):
        import preview_progress
        events = []
        with self.assertRaises(RuntimeError):
            with preview_progress.reporting(events.append):
                preview_progress.advance(1)
                raise RuntimeError('render failed')
        preview_progress.advance(3)
        self.assertEqual(events, [1])
