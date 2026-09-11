# SPDX-License-Identifier: GPL-3.0-only
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import linux_theme


class LinuxThemeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.v4 = self.home / ".local/state/omarchy/current/theme"
        self.v3 = self.home / ".config/omarchy/current/theme"
        for context in (patch.object(linux_theme.sys, "platform", "linux"),
                        patch.object(Path, "home", return_value=self.home),
                        patch.dict(os.environ, {}, clear=True)):
            context.start()
            self.addCleanup(context.stop)

    def write_palette(self, directory, text='background = "#1a1b26"\nforeground = "#a9b1d6"\naccent = "#7aa2f7"\n'):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "colors.toml").write_text(text)

    def test_v4_precedes_legacy_and_reopens_after_directory_swap(self):
        self.write_palette(self.v3, 'background="#ffffff"\nforeground="#000000"\n')
        self.write_palette(self.v4)
        self.assertEqual(linux_theme.desktop_theme()["colors"]["background"], "#1a1b26")
        replacement = self.v4.with_name("next-theme")
        self.write_palette(replacement, 'mode="light"\nbackground="#f2e9e1"\nforeground="#575279"\n')
        self.v4.rename(self.v4.with_name("old-theme"))
        replacement.rename(self.v4)
        current = linux_theme.desktop_theme()
        self.assertEqual(current["mode"], "light")
        self.assertEqual(current["colors"]["background"], "#f2e9e1")

    def test_legacy_symlink_palette_aliases_and_light_marker(self):
        actual = self.home / "themes/legacy"
        self.write_palette(actual, 'bg="#FEFEFE"\nfg="#212121"\ncolor4="#123456"\nbackground="#ffffff"\n')
        (actual / "light.mode").touch()
        self.v3.parent.mkdir(parents=True)
        self.v3.symlink_to(actual, target_is_directory=True)
        result = linux_theme.desktop_theme()
        self.assertEqual(result, {"source": "omarchy", "mode": "light", "colors": {
            "background": "#ffffff", "foreground": "#212121", "accent": "#123456"}})

    def test_absolute_xdg_relocation_precedes_default_but_relative_is_ignored(self):
        self.write_palette(self.v4)
        relocated = self.home / "state/omarchy/current/theme"
        self.write_palette(relocated, 'background="#fffafa"\nforeground="#202020"\n')
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.home / "state")}):
            self.assertEqual(linux_theme.desktop_theme()["colors"]["background"], "#fffafa")
        with patch.dict(os.environ, {"XDG_STATE_HOME": "state"}):
            self.assertEqual(linux_theme.desktop_theme()["colors"]["background"], "#1a1b26")

    def test_filters_unsupported_values_without_executing_theme_content(self):
        sentinel = self.home / "should-not-exist"
        self.write_palette(self.v4, 'background="#222222"\nforeground="#ffffff"\n'
                           'accent="red; background:url(https://example.invalid)"\n'
                           'selection=123\nmuted=["#123456"]\n'
                           f'command="touch {sentinel}"\n')
        result = linux_theme.desktop_theme()
        self.assertEqual(set(result["colors"]), {"background", "foreground"})
        self.assertFalse(sentinel.exists())

    def test_missing_malformed_large_and_unreadable_palettes_fall_back(self):
        fallback = {"source": "system", "mode": None, "colors": {}}
        self.assertEqual(linux_theme.desktop_theme(), fallback)
        for content in ('not TOML', 'background="#222222"', 'x="' + 'a' * 65536 + '"'):
            self.write_palette(self.v4, content)
            self.assertEqual(linux_theme.desktop_theme(), fallback)
        with patch.object(linux_theme, "_palette_file", side_effect=PermissionError):
            self.assertEqual(linux_theme.desktop_theme(), fallback)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "POSIX file types")
    def test_fifo_is_rejected_without_waiting_for_a_writer(self):
        self.v4.mkdir(parents=True)
        os.mkfifo(self.v4 / "colors.toml")
        self.assertEqual(linux_theme.desktop_theme()["source"], "system")

    def test_other_platforms_never_read_omarchy_files(self):
        for platform in ("darwin", "win32"):
            with patch.object(linux_theme.sys, "platform", platform), patch.object(linux_theme, "_theme_directories") as directories:
                self.assertEqual(linux_theme.desktop_theme()["source"], "system")
                directories.assert_not_called()

    @unittest.skipUnless(shutil.which("node"), "Node.js is needed for browser behavior tests")
    def test_browser_theme_lifecycle(self):
        # setUp isolates environment values for path tests; preserve the
        # absolute Node executable resolved before that isolation below.
        result = subprocess.run([NODE, "--test", str(Path(__file__).with_name("desktop-theme.test.mjs"))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


NODE = shutil.which("node")

if __name__ == "__main__":
    unittest.main()
