# SPDX-License-Identifier: GPL-3.0-only
"""Keep the cross-platform denoise model wired into each release gate.

Mirrors tests/test_hair_packaging.py's approach for the other model already
packaged across all three platforms: check the runtime locks pin the right
inference backend, and check each build script fetches/copies the model and
runs a real-inference smoke test in the right order relative to relocation.
"""
from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
ONNX_ASSETS = (
    "denoise.onnx",
    "models.json",
    "SCUNet-CODE-LICENSE.txt",
    "SCUNet-WEIGHTS-LICENSE.txt",
)


class DenoisePackagingTests(unittest.TestCase):
    def test_macos_lock_has_no_onnx_runtime(self):
        # Core ML is the macOS backend; onnxruntime belongs only to the
        # platforms that have no Core ML.
        lock = (ROOT / "requirements-runtime.lock").read_text()
        self.assertNotIn("onnxruntime", lock)

    def test_windows_lock_pins_the_directml_provider(self):
        lock = (ROOT / "packaging/runtime-windows.lock").read_text()
        self.assertIn("onnxruntime-directml==", lock)
        self.assertNotIn("onnxruntime==", lock)

    def test_linux_lock_pins_the_cpu_provider(self):
        lock = (ROOT / "packaging/runtime-linux.lock").read_text()
        self.assertIn("onnxruntime==", lock)
        self.assertNotIn("onnxruntime-directml", lock)

    def test_convert_models_supports_the_onnx_export_format(self):
        source = (ROOT / "scripts/convert-models.py").read_text()
        self.assertIn('"--format", choices=("coreml", "onnx")', source)
        self.assertIn("def convert_onnx(", source)
        # The bit-identity gate must run for both formats, not just Core ML.
        self.assertIn("_traced_wrapped_network", source)

    def test_enhance_workflow_supports_all_three_platforms(self):
        source = (ROOT / "enhance_workflow.py").read_text()
        self.assertIn('SUPPORTED_PLATFORMS = ("darwin", "win32", "linux")', source)
        self.assertIn("def onnx_runner(", source)
        self.assertIn("def onnx_batch_runner(", source)

    def test_macos_build_requires_and_copies_the_coreml_package(self):
        source = (ROOT / "scripts/build-release.sh").read_text()
        self.assertIn("scripts/models/denoise.mlpackage", source)
        self.assertIn("scripts/smoke-denoise.py", source)

    def test_linux_build_copies_onnx_assets_before_relocated_smoke(self):
        source = (ROOT / "scripts/linux/build-release.py").read_text()
        fetch = source.index('onnx_root = ROOT / "scripts/models/onnx"')
        relocate = source.index("bundle.rename(relocated)")
        smoke = source.index('ROOT / "scripts/smoke-denoise.py"')
        self.assertLess(fetch, relocate,
                        "denoise assets must be staged before the bundle moves")
        self.assertLess(relocate, smoke,
                        "denoise smoke must run against the relocated bundle")
        for asset in ONNX_ASSETS:
            self.assertIn(asset, source[fetch:relocate])
        self.assertIn('bundle / "Resources/models" / asset', source[fetch:relocate])

    def test_windows_build_copies_onnx_assets_before_smoke(self):
        source = (ROOT / "scripts/windows/build-release.ps1").read_text()
        hair_smoke = source.index('scripts\\smoke-hair-mask.py')
        onnx_copy = source.index('$OnnxModelRoot = Join-Path $Project "scripts\\models\\onnx"')
        denoise_smoke = source.index('scripts\\smoke-denoise.py')
        self.assertLess(hair_smoke, onnx_copy)
        self.assertLess(onnx_copy, denoise_smoke)
        for asset in ONNX_ASSETS:
            self.assertIn(asset, source[onnx_copy:denoise_smoke])

    def test_smoke_denoise_script_exists_and_needs_no_required_arguments(self):
        source = (ROOT / "scripts/smoke-denoise.py").read_text()
        self.assertIn("import enhance_workflow", source)
        self.assertNotIn('required=True', source)


if __name__ == "__main__":
    unittest.main()
