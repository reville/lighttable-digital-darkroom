from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "windows_uninstall_manifest", ROOT / "scripts/windows/make-uninstall-manifest.py"
)
manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest)


class WindowsUninstallManifestTests(unittest.TestCase):
    def test_only_shipped_files_are_removed_and_directories_are_removed_inside_out(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / "payload"
            (payload / "Resources/LightTable").mkdir(parents=True)
            (payload / "Resources/LightTable/server.py").write_text("pass\n")
            (payload / "LightTable.exe").write_bytes(b"application")
            commands = manifest.make_manifest(payload)

            # User files created after packaging must survive uninstall, including
            # ones inside a directory that also contains application resources.
            user_file = payload / "Resources/LightTable/my-notes.txt"
            user_file.write_text("keep me")
            outside = root / "Catalog/library.sqlite3"
            outside.parent.mkdir()
            outside.write_bytes(b"keep catalog")
            for command, relative in re.findall(r'^  (Delete|RMDir) "\$INSTDIR\\(.*)"$', commands, re.M):
                path = payload.joinpath(*relative.split("\\"))
                if command == "Delete":
                    path.unlink()
                else:
                    try:
                        path.rmdir()
                    except OSError:
                        pass  # NSIS RMDir preserves nonempty directories.
            self.assertEqual(user_file.read_text(), "keep me")
            self.assertEqual(outside.read_bytes(), b"keep catalog")
            self.assertFalse((payload / "LightTable.exe").exists())
            self.assertFalse((payload / "Resources/LightTable/server.py").exists())
            self.assertLess(commands.index('RMDir "$INSTDIR\\Resources\\LightTable"'),
                            commands.index('RMDir "$INSTDIR\\Resources"'))
            self.assertNotIn("/r", commands)

    def test_paths_escape_nsis_variables_and_reject_traversal_or_injected_commands(self):
        self.assertEqual(manifest.nsis_path(Path("profiles/$example.json")),
                         "profiles\\$$example.json")
        for unsafe in (Path("../outside"), Path('/absolute'), Path('quote".txt'), Path("line\nbreak"),
                       Path("C:\\outside"), Path("..\\outside"), Path("*.txt")):
            with self.subTest(path=unsafe), self.assertRaises(ValueError):
                manifest.nsis_path(unsafe)

    def test_payload_symlink_cannot_add_files_outside_the_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / "payload"
            payload.mkdir()
            outside = root / "outside.txt"
            outside.write_text("private")
            try:
                (payload / "link").symlink_to(outside)
            except OSError:
                self.skipTest("Creating symlinks requires Windows developer mode")
            with self.assertRaises(ValueError):
                manifest.make_manifest(payload)


if __name__ == "__main__":
    unittest.main()
