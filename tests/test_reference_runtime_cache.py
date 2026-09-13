# SPDX-License-Identifier: GPL-3.0-only
"""Exercise the workflow's cache preparation against real temporary Git repos."""
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / '.github/workflows/python-tests.yml').read_text()
BEGIN = WORKFLOW.index('          prepare_reference() {')
END = WORKFLOW.index('          mkdir -p .build/reference-runtimes', BEGIN)
FUNCTION = textwrap.dedent(WORKFLOW[BEGIN:END])


class ReferenceRuntimeCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.cache = self.root / 'cache'
        self.env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull}
        self.git('init', '-q')
        (self.source / 'data.txt').write_text('pinned reference')
        (self.source / '.gitignore').write_text('ignored.py\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
        self.sha = self.git('rev-parse', 'HEAD').stdout.strip()

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.source, env=self.env,
                              text=True, capture_output=True, check=True, timeout=20)

    def prepare(self, revision=None):
        return subprocess.run(['bash', '-e', '-c', FUNCTION + '\nprepare_reference "$1" "$2" "$3"',
                               'test', self.source.as_uri(), revision or self.sha, str(self.cache)],
                              env=self.env, capture_output=True, text=True, timeout=30)

    def test_fresh_then_cached_without_remote_access(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.source.rename(self.root / 'offline-source')
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual((self.cache / 'data.txt').read_text(), 'pinned reference')

    def test_dirty_and_ignored_cached_files_are_rejected(self):
        self.assertEqual(self.prepare().returncode, 0)
        (self.cache / 'ignored.py').write_text('unexpected module')
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('cache is dirty', result.stdout)
        (self.cache / 'ignored.py').unlink()
        (self.cache / 'data.txt').write_text('modified')
        self.assertNotEqual(self.prepare().returncode, 0)

    def test_wrong_revision_cannot_reuse_cached_bytes(self):
        self.assertEqual(self.prepare().returncode, 0)
        result = self.prepare('0' * 40)
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
