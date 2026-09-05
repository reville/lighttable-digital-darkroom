from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "update-personal-app.sh"


class PersonalBuildScriptTests(unittest.TestCase):
    def test_quick_build_reuses_but_does_not_mutate_the_base_runtime(self):
        source = SCRIPT.read_text()
        self.assertIn('BASE_NATIVE="$BASE_CONTENTS/MacOS/LightTable"', source)
        self.assertIn('BASE_PYTHON="$BASE_CONTENTS/Resources/Python/bin/python3.13"', source)
        self.assertIn('/bin/cp -cRp "$BASE_APP" "$STAGE_APP"', source)
        self.assertIn("is not a native Mach-O binary", source)
        self.assertIn(
            'codesign --force --sign - "$STAGE_CONTENTS/MacOS/lighttable-cli"',
            source,
        )
        self.assertNotIn("uv pip sync", source)
        self.assertNotIn('rm -rf "$BASE_APP"', source)

    def test_dependency_changes_require_a_full_release_build(self):
        source = SCRIPT.read_text()
        self.assertIn("actual-packages.txt", source)
        self.assertIn('BASE_PYTHON_REV', source)
        self.assertIn('BASE_RUST_REV', source)
        self.assertIn('BASE_SPARKLE_VERSION', source)
        self.assertGreaterEqual(source.count("run scripts/build-release.sh"), 4)

    def test_install_is_staged_verified_and_recoverable(self):
        source = SCRIPT.read_text()
        running_check = source.index(
            '/usr/bin/pgrep -f "$INSTALL_APP/Contents/MacOS/LightTable"'
        )
        clone = source.index('/bin/cp -cRp "$BASE_APP" "$STAGE_APP"')
        first_verify = source.index(
            '/usr/bin/codesign --verify --deep --strict --verbose=2 "$STAGE_APP"'
        )
        native_smoke = source.index(
            '"$ROOT/scripts/native-app-smoke.py" --app "$STAGE_APP" --layer package'
        )
        move_old = source.index('/bin/mv "$INSTALL_APP" "$BACKUP_APP"')
        move_new = source.index('/bin/mv "$STAGE_APP" "$INSTALL_APP"')
        restore = source.index('/bin/mv "$BACKUP_APP" "$INSTALL_APP"')
        self.assertLess(running_check, clone)
        self.assertLess(first_verify, move_old)
        self.assertLess(first_verify, native_smoke)
        self.assertLess(native_smoke, move_old)
        self.assertLess(move_old, move_new)
        self.assertGreater(restore, move_new)

    def test_personal_data_paths_remain_isolated(self):
        source = SCRIPT.read_text()
        self.assertIn('BUNDLE_IDENTIFIER="com.reville.filmlab.nprinstalled"', source)
        self.assertIn('DATA_NAME="Film Lab - NPR Installed"', source)
        self.assertIn("NUMBA_CACHE_DIR", source)
        self.assertIn("SUEnableAutomaticChecks false", source)

    def test_personal_update_refreshes_the_current_app_icon(self):
        source = SCRIPT.read_text()
        self.assertIn('APP_ICON="$ROOT/build/LightTable.icns"', source)
        self.assertIn('require_file "$APP_ICON"', source)
        self.assertIn(
            '/usr/bin/ditto "$APP_ICON" "$STAGE_CONTENTS/Resources/LightTable.icns"',
            source,
        )


if __name__ == "__main__":
    unittest.main()
