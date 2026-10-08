# SPDX-License-Identifier: GPL-3.0-only
"""Exercise Store saved-folder acceptance against real filesystem symlinks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc'), 'requires Swift on macOS')
class StoreFolderAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='lighttable-store-folders-')
        cls.addClassCleanup(cls.build.cleanup)
        directory = Path(cls.build.name)
        source = (ROOT / 'app/main.swift').read_text()
        policy = source.split('// MARK: - Store folder access policy', 1)[1].split('// MARK: - Store bookmark bridge', 1)[0]
        main = directory / 'main.swift'
        main.write_text('import Foundation\n' + policy + '''
let roots = CommandLine.arguments.dropFirst(3).map { URL(fileURLWithPath: $0) }
let allowed = storeFolderIsAccessible(CommandLine.arguments[1], selectedRoots: roots,
                                      supportDirectory: URL(fileURLWithPath: CommandLine.arguments[2]))
print(allowed ? "true" : "false")
''')
        cls.binary = directory / 'access'
        env = dict(os.environ, SWIFT_MODULECACHE_PATH=str(directory / 'swift-cache'),
                   CLANG_MODULE_CACHE_PATH=str(directory / 'clang-cache'))
        subprocess.run(['swiftc', str(main), '-o', str(cls.binary)], env=env,
                       check=True, capture_output=True, timeout=90)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.support = self.root / 'Application Support' / 'LightTable'
        self.support.mkdir(parents=True)
        self.external = self.root / 'outside'
        self.external.mkdir()

    def allowed(self, path, selected=()):
        result = subprocess.run([str(self.binary), str(path), str(self.support),
                                 *map(str, selected)], check=True, capture_output=True, text=True, timeout=10)
        return json.loads(result.stdout)

    def test_app_owned_photos_and_demo_survive_fresh_processes_without_bookmarks(self):
        for name in ('LightTable Imports/Apple Photos', 'Getting Started'):
            folder = self.support / name
            folder.mkdir(parents=True)
            self.assertTrue(self.allowed(folder))
            self.assertTrue(self.allowed(folder))

    def test_outside_and_sibling_prefix_require_explicit_selection(self):
        sibling = self.support.parent / 'LightTable-other'
        sibling.mkdir()
        self.assertFalse(self.allowed(sibling))
        self.assertFalse(self.allowed(self.external))
        self.assertTrue(self.allowed(self.external, selected=[self.external]))

    def test_descendant_symlink_cannot_escape_app_owned_root(self):
        link = self.support / 'outside'
        link.symlink_to(self.external, target_is_directory=True)
        self.assertFalse(self.allowed(link))
        self.assertTrue(self.allowed(link, selected=[self.external]))

    def test_support_root_symlink_cannot_make_external_folder_app_owned(self):
        self.support.rmdir()
        self.support.symlink_to(self.external, target_is_directory=True)
        self.assertFalse(self.allowed(self.support))
        self.assertFalse(self.allowed(self.external))
