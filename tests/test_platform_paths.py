"""Platform storage contracts without reading or writing a user's catalog."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import catalog
import platform_paths as paths
from lighttable_cli.__main__ import build_parser, discovery_directory, profile_environment
from lighttable_cli.instances import default_instance_directory


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == "nt", "POSIX XDG path semantics")
class PlatformPathsTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.home = Path("/example/home")
        self.home_patch = mock.patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.platform_patch = mock.patch.object(sys, "platform", "linux")
        self.platform_patch.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.home_patch.stop)
        self.addCleanup(self.platform_patch.stop)

    def test_linux_defaults_and_client_catalog_agree(self):
        data = self.home / ".local/share/lighttable"
        self.assertEqual(catalog.default_catalog_path(), data / "Catalog/library.sqlite3")
        self.assertEqual(paths.preferences_file(ROOT), self.home / ".config/lighttable/prefs.json")
        self.assertEqual(paths.presets_file(ROOT), data / "presets.json")
        self.assertEqual(paths.cache_directory(ROOT), self.home / ".cache/lighttable")
        self.assertEqual(paths.model_directory(), data / "Models")
        self.assertEqual(paths.ai_directory(paths.preferences_file(ROOT)), data / "AI Index")
        self.assertEqual(paths.generated_data_directory(paths.preferences_file(ROOT)), data)
        self.assertEqual(paths.server_log_file(), self.home / ".local/state/lighttable/logs/server.log")
        self.assertEqual(default_instance_directory(), self.home / ".local/state/lighttable/instances")

    def test_all_absolute_xdg_bases_and_runtime_directory(self):
        os.environ.update({"XDG_DATA_HOME": "/custom/data", "XDG_CONFIG_HOME": "/custom/config",
                           "XDG_CACHE_HOME": "/custom/cache", "XDG_STATE_HOME": "/custom/state",
                           "XDG_RUNTIME_DIR": "/run/user/1234"})
        self.assertEqual(paths.catalog_file(), Path("/custom/data/lighttable/Catalog/library.sqlite3"))
        self.assertEqual(paths.preferences_file(ROOT), Path("/custom/config/lighttable/prefs.json"))
        self.assertEqual(paths.presets_file(ROOT), Path("/custom/data/lighttable/presets.json"))
        self.assertEqual(paths.cache_directory(ROOT), Path("/custom/cache/lighttable"))
        self.assertEqual(paths.server_log_file(), Path("/custom/state/lighttable/logs/server.log"))
        self.assertEqual(default_instance_directory(), Path("/run/user/1234/lighttable/instances"))

    def test_relative_empty_and_tilde_xdg_values_are_ignored(self):
        for value in ("", "relative/path", "~/not-absolute"):
            with self.subTest(value=value):
                os.environ.update(dict.fromkeys(("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                                                 "XDG_STATE_HOME", "XDG_RUNTIME_DIR"), value))
                self.assertEqual(paths.catalog_file(), self.home / ".local/share/lighttable/Catalog/library.sqlite3")
                self.assertEqual(paths.preferences_file(ROOT), self.home / ".config/lighttable/prefs.json")
                self.assertEqual(paths.cache_directory(ROOT), self.home / ".cache/lighttable")
                self.assertEqual(default_instance_directory(), self.home / ".local/state/lighttable/instances")

    def test_explicit_lighttable_overrides_win(self):
        os.environ.update({"XDG_DATA_HOME": "/xdg/data", "XDG_RUNTIME_DIR": "/run/user/1234",
                           "LIGHTTABLE_CATALOG_FILE": "/isolated/catalog.sqlite3",
                           "LIGHTTABLE_PREFS_FILE": "/isolated/prefs.json",
                           "LIGHTTABLE_PRESETS_FILE": "/isolated/presets.json",
                           "LIGHTTABLE_CACHE_DIR": "/isolated/cache",
                           "LIGHTTABLE_INSTANCE_DIR": "/isolated/instances",
                           "LIGHTTABLE_AI_DIR": "/isolated/ai",
                           "LIGHTTABLE_MODEL_DIR": "/isolated/models",
                           "LIGHTTABLE_SERVER_LOG": "/isolated/server.log",
                           "LIGHTTABLE_PROFILE_ROOT": "/isolated/profiles"})
        self.assertEqual(catalog.default_catalog_path(), Path("/isolated/catalog.sqlite3"))
        self.assertEqual(paths.preferences_file(ROOT), Path("/isolated/prefs.json"))
        self.assertEqual(paths.presets_file(ROOT), Path("/isolated/presets.json"))
        self.assertEqual(paths.cache_directory(ROOT), Path("/isolated/cache"))
        self.assertEqual(default_instance_directory(), Path("/isolated/instances"))
        self.assertEqual(paths.ai_directory(paths.preferences_file(ROOT)), Path("/isolated/ai"))
        self.assertEqual(paths.model_directory(), Path("/isolated/models"))
        self.assertEqual(paths.server_log_file(), Path("/isolated/server.log"))
        self.assertEqual(paths.profile_root(), Path("/isolated/profiles"))
        del os.environ["LIGHTTABLE_AI_DIR"]
        self.assertEqual(paths.ai_directory(paths.preferences_file(ROOT)), Path("/isolated/AI Index"))
        self.assertEqual(paths.generated_data_directory(paths.preferences_file(ROOT)), Path("/isolated"))

    def test_review_profile_isolated_even_with_primary_overrides(self):
        os.environ.update({"XDG_DATA_HOME": "/data", "LIGHTTABLE_PREFS_FILE": "/primary/prefs.json",
                           "LIGHTTABLE_AI_DIR": "/primary/AI Index", "LIGHTTABLE_MODEL_DIR": "/primary/Models",
                           "LIGHTTABLE_LOG_FILE": "/primary/server.log"})
        args = build_parser().parse_args(["--profile", "review", "status"])
        env, _ = profile_environment(args, parent=False)
        review = Path("/data/lighttable/Profiles/review")
        for key in ("LIGHTTABLE_CATALOG_FILE", "LIGHTTABLE_PREFS_FILE", "LIGHTTABLE_PRESETS_FILE",
                    "LIGHTTABLE_CACHE_DIR", "LIGHTTABLE_INSTANCE_DIR", "LIGHTTABLE_AI_DIR",
                    "LIGHTTABLE_MODEL_DIR", "LIGHTTABLE_LOG_FILE", "LIGHTTABLE_SERVER_LOG"):
            self.assertTrue(Path(env[key]).is_relative_to(review), key)
        self.assertEqual(discovery_directory(args), review / "instances")

    def test_existing_macos_defaults_are_unchanged(self):
        with mock.patch.object(sys, "platform", "darwin"):
            support = self.home / "Library/Application Support/LightTable"
            self.assertEqual(paths.catalog_file(), support / "Catalog/library.sqlite3")
            self.assertEqual(paths.model_directory(), support / "Models")
            self.assertEqual(paths.instance_directory(), support / "instances")
            self.assertEqual(paths.preferences_file(ROOT), ROOT / "prefs.json")
            self.assertEqual(paths.presets_file(ROOT), ROOT / "presets.json")
            self.assertEqual(paths.cache_directory(ROOT), ROOT / "cache")
            self.assertEqual(paths.ai_directory(ROOT / "prefs.json"), support / "AI Index")

    def test_existing_windows_defaults_are_unchanged(self):
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(paths.catalog_file(), self.home / "AppData/Local/LightTable/Catalog/library.sqlite3")
            self.assertEqual(paths.instance_directory(), self.home / "LightTable/instances")
            os.environ["LOCALAPPDATA"] = "/windows/local"
            self.assertEqual(paths.catalog_file(), Path("/windows/local/LightTable/Catalog/library.sqlite3"))
            self.assertEqual(paths.model_directory(), Path("/windows/local/LightTable/Models"))
            self.assertEqual(paths.instance_directory(), Path("/windows/local/LightTable/instances"))
            self.assertEqual(paths.preferences_file(ROOT), ROOT / "prefs.json")
            self.assertEqual(paths.presets_file(ROOT), ROOT / "presets.json")
            self.assertEqual(paths.cache_directory(ROOT), ROOT / "cache")


@unittest.skipIf(os.name == "nt", "POSIX XDG path semantics")
class LinuxServerPathIntegrationTests(unittest.TestCase):
    def test_server_initializes_only_isolated_xdg_storage_and_cli_discovers_same_root(self):
        # Patch the path policy alone so the same integration runs on macOS CI
        # without pretending macOS native Python dependencies are Linux builds.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("LIGHTTABLE_", "XDG_"))}
            env.update({"XDG_DATA_HOME": str(root / "data"), "XDG_CONFIG_HOME": str(root / "config"),
                        "XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state"),
                        "XDG_RUNTIME_DIR": str(root / "runtime"), "LIGHTTABLE_CATALOG": "0",
                        "LIGHTTABLE_SAFE_MODE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
            program = '''import json
import platform_paths
platform_paths.is_linux = lambda: True
import server
from lighttable_cli.instances import default_instance_directory
print(json.dumps({"catalog": str(server.catalog_module.default_catalog_path()),
 "cache": str(server.CACHE), "prefs": str(server.PREFS_FILE), "presets": str(server.PRESETS_FILE),
 "ai": str(server.AI_DATA_ROOT), "server_instances": str(server.instance_directory()),
 "cli_instances": str(default_instance_directory())}))
'''
            result = subprocess.run([sys.executable, "-c", program], cwd=ROOT, env=env,
                                    capture_output=True, text=True, timeout=60, check=True)
            actual = json.loads(result.stdout.splitlines()[-1])
            self.assertEqual(actual, {
                "catalog": str(root / "data/lighttable/Catalog/library.sqlite3"),
                "cache": str(root / "cache/lighttable"),
                "prefs": str(root / "config/lighttable/prefs.json"),
                "presets": str(root / "data/lighttable/presets.json"),
                "ai": str(root / "data/lighttable/AI Index"),
                "server_instances": str(root / "runtime/lighttable/instances"),
                "cli_instances": str(root / "runtime/lighttable/instances"),
            })
            self.assertTrue((root / "cache/lighttable/render").is_dir())
            self.assertFalse((root / "data/lighttable/Catalog/library.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
