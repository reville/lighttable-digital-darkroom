# SPDX-License-Identifier: GPL-3.0-only
"""Pull-request check tiers follow the touched paths; everything else runs all checks."""
import importlib.util
import io
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('change_scope', ROOT / 'scripts/ci/change-scope.py')
change_scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(change_scope)


class ChangeScopeTests(unittest.TestCase):
    def test_documentation_only_changes_need_no_test_job(self):
        result = change_scope.scope(['docs/cli.md\n', 'README.md\n', 'web/locales/de.json\n'])
        self.assertTrue(result['docs_only'])
        self.assertFalse(any(result[key] for key in ('full', 'pixel', 'windows', 'linux', 'installers')))

    def test_ordinary_feature_changes_run_only_the_light_tier(self):
        result = change_scope.scope(['server.py', 'web/app.js', 'tests/test_server_http.py'])
        self.assertEqual(result, {'full': False, 'docs_only': False, 'pixel': False,
                                  'windows': False, 'linux': False, 'installers': False})

    def test_pixel_paths_add_the_engine_gates(self):
        for path in ('grade.py', 'rust-engine/src/main.rs', 'web/gl.js', 'app/NativePreview.metal',
                     'tests/goldens/film/standard.png', 'web/mask-atlas.js'):
            self.assertTrue(change_scope.scope([path])['pixel'], path)

    def test_host_and_packaging_paths_add_their_jobs(self):
        self.assertTrue(change_scope.scope(['windows-shell/src/linux.rs'])['windows'])
        self.assertTrue(change_scope.scope(['windows-shell/src/linux.rs'])['linux'])
        self.assertTrue(change_scope.scope(['scripts/windows/build-release.ps1'])['windows'])
        self.assertFalse(change_scope.scope(['scripts/windows/build-release.ps1'])['linux'])
        self.assertTrue(change_scope.scope(['scripts/linux/build-release.py'])['linux'])
        self.assertTrue(change_scope.scope(['packaging/npm/package.json'])['installers'])
        self.assertTrue(change_scope.scope(['desktop_updater.py'])['installers'])

    def test_pushes_dispatches_labels_and_ci_changes_run_everything(self):
        everything = {'full': True, 'docs_only': False, 'pixel': True, 'windows': True,
                      'linux': True, 'installers': True}
        self.assertEqual(change_scope.scope(['docs/cli.md'], event='push'), everything)
        self.assertEqual(change_scope.scope(['docs/cli.md'], event='workflow_dispatch'), everything)
        self.assertEqual(change_scope.scope(['docs/cli.md'], labels={'ci:full'}), everything)
        self.assertEqual(change_scope.scope(['.github/workflows/python-tests.yml']), everything)
        self.assertEqual(change_scope.scope(['scripts/ci/change-scope.py']), everything)
        self.assertEqual(change_scope.scope(['requirements-runtime.lock']), everything)
        self.assertEqual(change_scope.scope([]), everything, 'an unknown file list must fail safe')

    def test_command_line_writes_github_outputs(self):
        with mock.patch('sys.stdin', io.StringIO('docs/cli.md\n')), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as out, \
                mock.patch('sys.stderr', new_callable=io.StringIO) as err:
            self.assertEqual(change_scope.main(['--event', 'pull_request', '--labels', 'bug, docs']), 0)
        self.assertEqual(out.getvalue(),
                         'full=false\ndocs_only=true\npixel=false\nwindows=false\nlinux=false\ninstallers=false\n')
        self.assertIn('Check scope: none', err.getvalue())


if __name__ == '__main__':
    unittest.main()
