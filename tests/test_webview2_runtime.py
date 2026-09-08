"""Run prerequisite behavior checks without installing anything on the test host."""

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


class WebView2RuntimeTests(unittest.TestCase):
    @unittest.skipUnless(POWERSHELL, "PowerShell is required")
    def test_installed_missing_untrusted_offline_and_failed_runtime_setup(self):
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(ROOT / "tests/windows_webview2.ps1")],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WebView2 runtime behavior checks passed", result.stdout)

    def test_prerequisite_failure_precedes_changes_to_existing_install(self):
        installer = (ROOT / "scripts/windows/installer.nsi").read_text()
        installation = installer.split('Section "Install"', 1)[1].split("SectionEnd", 1)[0]
        prerequisite, payload = installation.split('SetOutPath "$INSTDIR"', 1)
        self.assertIn('"$PLUGINSDIR\\ensure-webview2.ps1"', prerequisite)
        self.assertIn("SetErrorLevel 1", prerequisite)
        self.assertIn("Abort", prerequisite)
        self.assertIn('File /r "${PAYLOAD}\\*"', payload)


if __name__ == "__main__":
    unittest.main()
