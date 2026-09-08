from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/linux" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load("linux_desktop_integration", "desktop-integration.py")
builder = load("linux_package_builder", "build-release.py")


class LinuxDesktopIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data = self.root / "data"
        self.state = self.root / "state"
        self.commands = self.root / "commands"
        self.patch = patch.dict(os.environ, {
            "XDG_DATA_HOME": str(self.data), "XDG_STATE_HOME": str(self.state),
        })
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.bundle = self.make_bundle("bundle with spaces")
        self.desktop = self.data / "applications/org.lighttable.LightTable.desktop"
        self.record = self.state / "lighttable/desktop-integration.json"

    def make_bundle(self, name):
        bundle = self.root / name
        for relative in ("bin/lighttable", "bin/lighttable-desktop", "bin/lighttable-desktop-shell",
                         "Python/bin/python3", "share/icons/lighttable.png"):
            path = bundle / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        return bundle

    def act(self, action="install", bundle=None):
        with contextlib.redirect_stdout(io.StringIO()):
            installer.integrate(action, bundle or self.bundle, self.commands)

    def test_install_registers_bundle_and_uninstall_preserves_catalog_and_bundle(self):
        catalog = self.data / "lighttable/Catalog/library.sqlite3"
        catalog.parent.mkdir(parents=True)
        catalog.write_bytes(b"existing catalog")
        self.act()
        self.assertEqual((self.commands / "lighttable").resolve(), self.bundle / "bin/lighttable")
        self.assertIn('Exec=/bin/sh "' + str(self.bundle) + '/bin/lighttable-desktop"', self.desktop.read_text())
        self.assertIn(' %u\n', self.desktop.read_text())
        self.assertIn('MimeType=x-scheme-handler/lighttable;\n', self.desktop.read_text())
        validate = shutil.which("desktop-file-validate")
        if validate:
            subprocess.run([validate, str(self.desktop)], check=True, capture_output=True)
        self.act("uninstall")
        self.assertFalse(os.path.lexists(self.commands / "lighttable"))
        self.assertFalse(self.desktop.exists())
        self.assertFalse(self.record.exists())
        self.assertEqual(catalog.read_bytes(), b"existing catalog")
        self.assertTrue((self.bundle / "bin/lighttable-desktop-shell").exists())

    def test_upgrade_retargets_owned_launchers_and_old_uninstall_cannot_remove_them(self):
        self.act()
        upgraded = self.make_bundle("new bundle")
        self.act(bundle=upgraded)
        self.assertEqual((self.commands / "lighttable").resolve(), upgraded / "bin/lighttable")
        with self.assertRaisesRegex(ValueError, "different LightTable bundle"):
            self.act("uninstall")
        self.act("uninstall", upgraded)
        self.assertFalse(self.desktop.exists())

    def test_moving_bundle_and_reinstalling_repairs_broken_owned_symlinks(self):
        self.act()
        moved = self.bundle.with_name("moved bundle")
        self.bundle.rename(moved)
        self.act(bundle=moved)
        self.assertEqual((self.commands / "lighttable").resolve(), moved / "bin/lighttable")
        self.assertIn(str(moved), self.desktop.read_text())

    def test_preflight_refuses_unowned_file_before_writing_any_integration(self):
        self.desktop.parent.mkdir(parents=True)
        self.desktop.write_text("unrelated desktop launcher")
        with self.assertRaisesRegex(ValueError, "unowned or modified"):
            self.act()
        self.assertFalse(os.path.lexists(self.commands / "lighttable"))
        self.assertFalse(self.record.exists())
        self.assertEqual(self.desktop.read_text(), "unrelated desktop launcher")

    def test_preflight_refuses_unowned_broken_symlink(self):
        self.commands.mkdir()
        path = self.commands / "lighttable"
        path.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "unowned or modified"):
            self.act()
        self.assertEqual(os.readlink(path), str(self.root / "missing"))

    def test_uninstall_preserves_modified_entries(self):
        self.act()
        self.desktop.write_text("user changed this")
        command = self.commands / "lighttable"
        command.unlink()
        command.symlink_to("/different/command")
        self.act("uninstall")
        self.assertEqual(self.desktop.read_text(), "user changed this")
        self.assertEqual(os.readlink(command), "/different/command")
        self.assertFalse(os.path.lexists(self.commands / "lighttable-desktop"))

    def test_new_install_does_not_follow_a_symlink_installer_record(self):
        self.record.parent.mkdir(parents=True)
        original = self.root / "private.txt"
        original.write_text("keep me")
        self.record.symlink_to(original)
        with self.assertRaisesRegex(ValueError, "symbolic link installer record"):
            self.act()
        self.assertEqual(original.read_text(), "keep me")

    def test_relative_xdg_variables_fall_back_to_user_directories(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": "relative", "XDG_STATE_HOME": ""}), \
                patch.object(Path, "home", return_value=self.root / "home"):
            self.act()
        expected = self.root / "home/.local/share/applications/org.lighttable.LightTable.desktop"
        self.assertTrue(expected.is_file())
        self.assertFalse(self.desktop.exists())

    def test_exec_escapes_shell_metacharacters_and_rejects_newlines(self):
        value = installer.desktop_exec(Path('/tmp/a $b`c"d\\e%f/run'))
        self.assertEqual(value, '"/tmp/a \\\\$b\\\\`c\\\\"d\\\\\\\\e%%f/run"')
        with self.assertRaisesRegex(ValueError, "control characters"):
            installer.desktop_exec(Path("/tmp/new\nline/bin/lighttable"))

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("gio"), "Linux GIO is required")
    def test_desktop_launch_delivers_preset_uri_without_expanding_path_metacharacters(self):
        bundle = self.make_bundle('bundle $cash `tick` "quote" \\ %percent')
        command = bundle / "bin/lighttable-desktop"
        command.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$LIGHTTABLE_INTEGRATION_TEST_RESULT"\n')
        command.chmod(0o755)
        self.act(bundle=bundle)
        validate = shutil.which("desktop-file-validate")
        if validate:
            subprocess.run([validate, str(self.desktop)], check=True, capture_output=True)
        result_path = self.root / "launched-arguments.txt"
        uri = "lighttable://preset/test/film"
        completed = subprocess.run(["gio", "launch", str(self.desktop), uri],
                                   env=dict(os.environ, LIGHTTABLE_INTEGRATION_TEST_RESULT=str(result_path)),
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        deadline = time.monotonic() + 5
        while not result_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(result_path.is_file(), "GIO did not invoke the desktop entry's command")
        self.assertEqual(result_path.read_text().splitlines(), [uri])


class LinuxPackageResourcesTests(unittest.TestCase):
    def test_staging_includes_new_shared_modules_and_corrected_film_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, python_source, rust_source, bundle = (root / name for name in
                                                          ("project", "python", "rust", "bundle"))
            for path in ("server.py", "platform_paths.py", "linux_theme.py", "new_shared_module.py", "media-formats.json",
                         "lighttable_cli/__init__.py", "film_lab_ai/__init__.py", "web/index.html",
                         "film_lab_ai/licenses/SFace.txt", "film_lab_ai/licenses/YuNet.txt",
                         "profiles/stock.json", "presets/default.json", "LICENSE", "THIRD_PARTY_NOTICES.md",
                         "LINUX.md", "CLI.md", "docs/help/editing.json", "packaging/linux/arch/README.md",
                         "requirements-runtime.lock", "packaging/linux/runtime.json", "build/icon-1024.png"):
                source = project / path
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("application content")
            for name in ("lighttable", "lighttable-desktop", "install.sh", "uninstall.sh",
                         "desktop-integration.py", "runtime-smoke.py"):
                destination = project / "scripts/linux" / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / "scripts/linux" / name, destination)
            for source in (python_source / "src/spektrafilm/__init__.py", python_source / "LICENSE",
                           rust_source / "data/profiles/stock.json", rust_source / "LICENSE"):
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("upstream content")
            builder.stage_resources(project, python_source, rust_source, bundle)
            resources = bundle / "Resources/LightTable"
            self.assertEqual((resources / "new_shared_module.py").read_text(), "application content")
            self.assertTrue((resources / "platform_paths.py").is_file())
            self.assertEqual((resources / "build/icon-1024.png").read_bytes(),
                             (bundle / "share/icons/lighttable.png").read_bytes())
            self.assertTrue((resources / "linux_theme.py").is_file())
            for license_name in ("SFace.txt", "YuNet.txt"):
                self.assertEqual((resources / "film_lab_ai/licenses" / license_name).read_text(),
                                 "application content")
            self.assertTrue((bundle / "CLI.md").is_file())
            self.assertTrue((bundle / "docs/help/editing.json").is_file())
            self.assertTrue((bundle / "packaging/linux/arch/README.md").is_file())
            self.assertEqual((resources / "engine/data/profiles/stock.json").read_text(), "application content")
            self.assertTrue((resources / "vendor/spektrafilm/src/spektrafilm/__init__.py").is_file())
            self.assertTrue(os.access(bundle / "bin/lighttable", os.X_OK))

    def test_relocated_cli_wrapper_uses_bundled_python_and_preserves_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle with spaces"
            (bundle / "bin").mkdir(parents=True)
            (bundle / "Python/bin").mkdir(parents=True)
            (bundle / "Python/bin/python3").symlink_to(sys.executable)
            package = bundle / "Resources/LightTable/lighttable_cli"
            package.mkdir(parents=True)
            (package / "__main__.py").write_text(
                "import json, os, sys\nprint(json.dumps({'arguments': sys.argv[1:], "
                "'cache': sys.pycache_prefix, 'numba': os.environ['NUMBA_CACHE_DIR']}))\n")
            shutil.copy2(ROOT / "scripts/linux/lighttable", bundle / "bin/lighttable")
            moved = bundle.with_name("relocated bundle")
            bundle.rename(moved)
            command = root / "linked-command"
            command.symlink_to(moved / "bin/lighttable")
            environment = dict(os.environ, XDG_CACHE_HOME=str(root / "cache"))
            environment.pop("NUMBA_CACHE_DIR", None)
            result = subprocess.run([str(command), "file with spaces", "literal $`%"],
                                    env=environment, text=True, capture_output=True, check=True, timeout=20)
            output = json.loads(result.stdout)
            self.assertEqual(output["arguments"], ["file with spaces", "literal $`%"])
            self.assertEqual(output["cache"], str(root / "cache/lighttable/python-bytecode"))
            self.assertEqual(output["numba"], str(root / "cache/lighttable/compiled-runtime"))
            self.assertFalse((package / "__pycache__").exists())


if __name__ == "__main__":
    unittest.main()
