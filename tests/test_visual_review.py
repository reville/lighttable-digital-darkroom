"""Evidence-integrity tests for the opt-in review runner."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('visual_review', ROOT / 'scripts/visual-review/run.py')
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)


class VisualReviewTests(unittest.TestCase):
    def test_capture_must_cover_first_and_last_action(self):
        steps = [{'startedEpochMs': 1000, 'endedEpochMs': 2000}]
        metadata = {'firstFrameEpochMs': 900, 'frames': 60, 'durationSeconds': 1.2}
        self.assertTrue(review.capture_coverage(metadata, steps))
        for bad in [dict(metadata, firstFrameEpochMs=1100),
                    dict(metadata, durationSeconds=.9), dict(metadata, frames=0),
                    dict(metadata, error='encoder failed')]:
            self.assertFalse(review.capture_coverage(bad, steps))
        self.assertFalse(review.capture_coverage(metadata, []))

    def test_single_frame_flash_is_a_candidate_not_a_bug(self):
        frames = np.full((8, 8, 8), 80, dtype=np.uint8)
        frames[3] = 200
        found = review.transient_candidates(frames)
        self.assertEqual(len(found), 1)
        self.assertAlmostEqual(found[0]['seconds'], 3 / 60)
        self.assertEqual(found[0]['classification'], 'unreviewed-transient-candidate')

    def test_steady_frames_and_persistent_edits_are_not_flash_candidates(self):
        frames = np.full((8, 8, 8), 80, dtype=np.uint8)
        self.assertEqual(review.transient_candidates(frames), [])
        frames[3:] = 200
        self.assertEqual(review.transient_candidates(frames), [])

    def test_partial_log_preserves_failed_action(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            (folder / 'native-perf.jsonl').write_text('\n'.join([
                json.dumps({'stage': 'visual-step-start', 'name': 'pan'}),
                json.dumps({'stage': 'visual-step-end', 'name': 'pan', 'status': 'failed'}),
                '{truncated']))
            self.assertEqual(review.completed_steps(folder)[0]['status'], 'failed')

    def test_report_never_claims_missing_video_or_unreviewed_success(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            review.write_json(folder / 'result.json', {'status': 'NOT DONE', 'error': '<script>alert(1)</script>'})
            output = review.report(folder).read_text()
            self.assertIn('Video: NOT DONE.', output)
            self.assertIn('not-reviewed', output)
            self.assertIn('&lt;script&gt;', output)
            self.assertNotIn('<script>alert', output)
            self.assertNotIn('<video', output)

    def test_real_video_pipeline_produces_clips_and_reviewable_frames(self):
        # Synthetic codec fixture only; never used as product screenshot evidence.
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            review.checked(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                'testsrc2=size=160x100:rate=60:duration=3', '-c:v', 'libx264',
                '-pix_fmt', 'yuv420p', folder / 'window.mov'])
            review.write_json(folder / 'capture.json', {'firstFrameEpochMs': 1000,
                'frames': 180, 'durationSeconds': 3, 'error': ''})
            steps = [{'journey': journey, 'name': journey, 'status': 'passed',
                'startedEpochMs': 1100 + i * 600, 'endedEpochMs': 1500 + i * 600,
                'durationMs': 400} for i, journey in enumerate(['browse', 'zoom', 'edit', 'explore'])]
            result = review.analyze(folder, steps)
            self.assertTrue(result['coverageComplete'])
            self.assertEqual(result['reviewStatus'], 'not-reviewed')
            for journey in ['browse', 'zoom', 'edit', 'explore']:
                self.assertGreater((folder / 'evidence' / f'{journey}.mp4').stat().st_size, 100)
            self.assertEqual(len(list((folder / 'evidence').glob('*.png'))), 12)
            self.assertIn('three frame samples', review.report(folder).read_text())

    def test_cleanup_terminates_owned_process(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        review.stop(child)
        self.assertIsNotNone(child.poll())


if __name__ == '__main__': unittest.main()
