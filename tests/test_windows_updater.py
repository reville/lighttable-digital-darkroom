"""Update-feed contracts: exact signed payload identity and release-only URLs."""
import base64
import importlib.util
from pathlib import Path
import plistlib
import re
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


if __name__ == "__main__":
    unittest.main()
