"""Source drift must require article review, even when the bundle still builds."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('help_content', ROOT / 'scripts/help_content.py')
help_content = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(help_content)


class HelpContentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'docs/help').mkdir(parents=True)
        (self.root / 'web').mkdir()
        (self.root / 'web/tool.js').write_text('function crop() { return "Crop"; }\n')
        self.article = {
            'id': 'crop', 'title': 'Crop a photo', 'category': 'Editing',
            'summary': 'Change the framing.', 'keywords': ['crop', 'straighten'],
            'sections': [{'title': 'Crop', 'paragraphs': ['Open Crop.'],
                          'steps': ['Drag the corners.', 'Choose Done.']}],
            'related': [], 'sources': [{'path': 'web/tool.js', 'anchor': 'function crop()'}],
        }
        self.write_articles([self.article])

    def write_articles(self, articles):
        (self.root / 'docs/help/editing.json').write_text(json.dumps(articles))

    def run_command(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return help_content.run(list(args), self.root)

    def test_shipped_content_is_reviewed_and_bundle_current(self):
        self.assertEqual(help_content.run(['check'], ROOT), 0)

    def test_build_cannot_acknowledge_review(self):
        self.assertEqual(self.run_command('build'), 1)
        self.assertFalse((self.root / help_content.LOCK).exists())
        self.assertEqual(self.run_command('review', 'crop'), 0)
        self.assertEqual(self.run_command('check'), 1, 'missing bundle must fail')
        self.assertEqual(self.run_command('build'), 0)
        self.assertEqual(self.run_command('check'), 0)

    def test_source_change_requires_review_even_when_anchor_survives(self):
        self.run_command('review', '--all')
        self.run_command('build')
        (self.root / 'web/tool.js').write_text('function crop() { return "New crop behavior"; }\n')
        self.assertEqual(self.run_command('check'), 1)
        self.assertEqual(self.run_command('build'), 1)
        self.run_command('review', 'crop')
        self.assertEqual(self.run_command('build'), 0)

    def test_content_changes_require_review_and_regeneration(self):
        self.run_command('review', '--all')
        self.run_command('build')
        self.article['summary'] = 'Use the new framing control.'
        self.write_articles([self.article])
        self.assertEqual(self.run_command('check'), 1)
        self.run_command('review', 'crop')
        self.assertEqual(self.run_command('check'), 1)
        self.assertEqual(self.run_command('build'), 0)

    def test_unrelated_changes_do_not_invalidate_help(self):
        self.run_command('review', '--all')
        self.run_command('build')
        (self.root / 'web/unrelated.js').write_text('new unrelated feature')
        self.assertEqual(self.run_command('check'), 0)

    def test_review_is_scoped_per_article(self):
        second = copy.deepcopy(self.article)
        second['id'] = 'second'
        self.write_articles([self.article, second])
        self.run_command('review', '--all')
        (self.root / 'web/tool.js').write_text('function crop() { return "changed"; }')
        self.run_command('review', 'crop')
        issues = help_content.drift(help_content.current_records(
            help_content.load_articles(self.root), self.root), help_content.read_lock(self.root))
        self.assertEqual(set(issues), {'second'})
        self.assertEqual(self.run_command('build'), 1)

    def test_removed_anchor_cannot_be_reviewed_away(self):
        (self.root / 'web/tool.js').write_text('function removedCrop() {}')
        self.assertEqual(self.run_command('review', '--all'), 1)

    def test_duplicate_ids_missing_links_and_path_escape_fail(self):
        self.write_articles([self.article, self.article])
        self.assertEqual(self.run_command('review', '--all'), 1)
        self.article['related'] = ['missing']
        self.write_articles([self.article])
        self.assertEqual(self.run_command('review', '--all'), 1)
        self.article['related'] = []
        self.article['sources'][0]['path'] = '../outside'
        self.write_articles([self.article])
        self.assertEqual(self.run_command('review', '--all'), 1)

    def test_bundle_strips_source_evidence(self):
        self.run_command('review', '--all')
        self.run_command('build')
        bundled = json.loads((self.root / help_content.BUNDLE).read_text())
        self.assertNotIn('sources', bundled['articles'][0])
        self.assertIn('steps', bundled['articles'][0]['sections'][0])

    @unittest.skipUnless(shutil.which('node'), 'Node.js is unavailable')
    def test_search_behaviors(self):
        result = subprocess.run(['node', '--test', 'tests/help-search.test.mjs'],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
