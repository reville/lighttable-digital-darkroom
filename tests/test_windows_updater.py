"""Update-feed contracts: exact signed payload identity and release-only URLs."""
import base64
import importlib.util
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("windows_appcast", ROOT / "scripts/windows/write-appcast.py")
feed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feed)


class WindowsAppcastTests(unittest.TestCase):
    def test_feed_identifies_the_exact_verified_exe_and_immutable_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            installer = Path(temporary) / "LightTable-1.2.3-windows-x64-setup.exe"
            installer.write_bytes(b"signed executable fixture")
            signature = base64.b64encode(bytes(range(64))).decode()
            root = ET.fromstring(feed.appcast("1.2.3", installer, signature))
            enclosure = root.find("channel/item/enclosure")
            self.assertEqual(enclosure.get("length"), str(installer.stat().st_size))
            self.assertEqual(enclosure.get(f"{{{feed.SPARKLE}}}edSignature"), signature)
            self.assertEqual(enclosure.get(f"{{{feed.SPARKLE}}}os"), "windows")
            self.assertEqual(enclosure.get(f"{{{feed.SPARKLE}}}version"), "1.2.3")
            self.assertEqual(enclosure.get("url"), f"{feed.REPOSITORY}/releases/download/v1.2.3/{installer.name}")

    def test_feed_rejects_mismatched_empty_and_unsigned_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            installer = Path(temporary) / "LightTable-1.2.3-windows-x64-setup.exe"
            installer.write_bytes(b"fixture")
            signature = base64.b64encode(bytes(range(64))).decode()
            for version, sig in [("1.2.4", signature), ("1.2.3-beta", signature), ("1.2.3", "invalid"), ("1.2.3", base64.b64encode(b"short").decode())]:
                with self.subTest(version=version, signature=sig), self.assertRaises(ValueError):
                    feed.appcast(version, installer, sig)
            installer.write_bytes(b"")
            with self.assertRaises(ValueError):
                feed.appcast("1.2.3", installer, signature)

    def test_windows_and_sparkle_publishers_pin_the_same_public_key(self):
        key = plistlib.loads((ROOT / "app/Info.plist").read_bytes())["SUPublicEDKey"]
        native = (ROOT / "windows-shell/src/windows_update.rs").read_text()
        build = (ROOT / "scripts/windows/build-release.ps1").read_text()
        self.assertEqual(re.search(r'PUBLIC_KEY: &str = "([^"]+)"', native)[1], key)
        self.assertEqual(re.search(r'\$PublicKey = "([^"]+)"', build)[1], key)


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell is required")
class WinSparkleStagingTests(unittest.TestCase):
    def test_stages_the_x64_release_dll_and_notices_without_development_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sdk, payload = root / "SDK with spaces", root / "payload"
            for name, data in {"x64/Release/WinSparkle.dll": b"x64 updater", "Win32/Release/WinSparkle.dll": b"wrong arch",
                               "bin/winsparkle-tool.exe": b"signing tool", "COPYING": b"license",
                               "COPYING.expat": b"dependency license"}.items():
                path = sdk / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            result = self.stage(sdk, payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((payload / "WinSparkle.dll").read_bytes(), b"x64 updater")
            self.assertEqual({p.relative_to(payload).as_posix() for p in payload.rglob("*") if p.is_file()},
                             {"WinSparkle.dll", "Resources/LightTable/licenses/winsparkle/COPYING",
                              "Resources/LightTable/licenses/winsparkle/COPYING.expat"})
            # An incomplete SDK fails before touching an existing payload.
            (sdk / "bin/winsparkle-tool.exe").unlink()
            (sdk / "x64/Release/WinSparkle.dll").write_bytes(b"replacement")
            result = self.stage(sdk, payload)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((payload / "WinSparkle.dll").read_bytes(), b"x64 updater")

    @staticmethod
    def stage(sdk, payload):
        return subprocess.run([shutil.which("pwsh") or shutil.which("powershell"), "-NoProfile", "-File",
                               str(ROOT / "scripts/windows/stage-winsparkle.ps1"), "-Sdk", str(sdk),
                               "-Payload", str(payload)], capture_output=True, text=True, timeout=30)


if __name__ == "__main__":
    unittest.main()
