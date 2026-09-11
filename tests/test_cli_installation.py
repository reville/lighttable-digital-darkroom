# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == 'nt', 'POSIX app launcher')
class CliInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.app = self.base / 'App with spaces.app'
        macos = self.app / 'Contents/MacOS'
        macos.mkdir(parents=True)
        self.command = macos / 'lighttable-cli'
        shutil.copy2(ROOT / 'lighttable', self.command)
        self.command.chmod(0o755)
        python = self.app / 'Contents/Resources/Python/bin/python3'
        python.parent.mkdir(parents=True)
        python.write_text('#!/usr/bin/env python3\nimport os, sys, json\nprint(json.dumps({"args":sys.argv[1:],"path":os.getenv("PYTHONPATH"),"bytecode":os.getenv("PYTHONDONTWRITEBYTECODE") }))\n')
        python.chmod(0o755)
        self.cwd = self.base / 'unrelated'
        self.cwd.mkdir()

    def invoke(self, command):
        result = subprocess.run([str(command), 'photos', 'list', 'literal ; $(value)'], cwd=self.cwd, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_relative_and_absolute_symlink_chain_finds_bundled_runtime(self):
        folder = self.base / 'bin'
        folder.mkdir()
        absolute = folder / 'absolute'
        absolute.symlink_to(self.command)
        relative = folder / 'lighttable'
        relative.symlink_to('absolute')
        output = self.invoke(relative)
        self.assertEqual(output['args'], ['-m', 'lighttable_cli', 'photos', 'list', 'literal ; $(value)'])
        self.assertEqual(Path(output['path'].split(os.pathsep)[0]).resolve(), (self.app / 'Contents/Resources/LightTable').resolve())
        self.assertEqual(output['bytecode'], '1')

    def test_installer_links_application_and_runs_from_any_directory(self):
        destination = self.base / 'custom bin'
        subprocess.run([str(ROOT/'scripts/install-cli.sh'), '--app', str(self.app), str(destination)], check=True, capture_output=True)
        self.invoke(destination / 'lighttable')

    def test_installer_preserves_unrelated_existing_command(self):
        destination = self.base / 'bin'
        destination.mkdir()
        command = destination / 'lighttable'
        command.write_text('user command')
        result = subprocess.run([str(ROOT/'scripts/install-cli.sh'), '--app', str(self.app), str(destination)], text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(command.read_text(), 'user command')

    def test_checkout_command_uses_its_source_from_another_directory(self):
        result = subprocess.run([str(ROOT/'lighttable'), '--help'], cwd=self.cwd, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('usage: lighttable', result.stdout)
