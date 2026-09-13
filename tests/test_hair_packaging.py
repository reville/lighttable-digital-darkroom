"""Keep model assets and real inference in each relocated package gate."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ASSETS = (
    "selfie_multiclass_256x256.tflite",
    "HairSegmentation-APACHE-2.0.txt",
    "hair-model.json",
)


class HairPackagingTests(unittest.TestCase):
    def test_runtime_locks_use_the_same_python_compatible_inference_set(self):
        expected = {
            "ai-edge-litert": "2.2.0",
            "backports.strenum": "1.2.8",
            "flatbuffers": "25.12.19",
            "ml-dtypes": "0.6.0",
            "protobuf": "7.36.1",
            "tqdm": "4.70.0",
            "typing-extensions": "4.16.0",
        }
        for filename in ("requirements-runtime.lock", "packaging/runtime-windows.lock"):
            with self.subTest(filename=filename):
                lines = (ROOT / filename).read_text().splitlines()
                pins = dict(line.split("==") for line in lines if line and not line.startswith("#"))
                self.assertEqual({key: pins.get(key) for key in expected}, expected)
                self.assertTrue(any("1.3.1 declares Python <3.11" in line for line in lines))
                if filename.startswith("packaging/"):
                    self.assertEqual(pins.get("colorama"), "0.4.6")
                else:
                    self.assertNotIn("colorama", pins)

    def test_macos_release_copies_all_verified_assets_before_inference(self):
        source = (ROOT / "scripts/build-release.sh").read_text()
        fetch = source.index('"$ROOT/scripts/fetch-hair-model.py" --into "$ROOT/scripts/models"')
        copy = source.index('for MODEL_ASSET in ')
        smoke = source.index('"$ROOT/scripts/smoke-hair-mask.py"')
        sign = source.index('"$ROOT/scripts/sign-app.sh"')
        self.assertLess(fetch, copy)
        self.assertLess(copy, smoke)
        self.assertLess(smoke, sign)
        for asset in ASSETS:
            self.assertIn(asset, source[copy:smoke])
        self.assertIn('"$APP/Contents/Resources/models/$MODEL_ASSET"', source[copy:smoke])

    def test_macos_relocation_smoke_restores_bundle_on_success_and_failure(self):
        source = (ROOT / "scripts/build-release.sh").read_text()
        start = source.index('(\n  HAIR_SMOKE_ROOT=')
        stop = source.index('\n)\n', start) + len('\n)\n')
        block = source[start:stop]
        for failure in (0, 17):
            with self.subTest(exit_status=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "source checkout"
                output = Path(directory) / "release output"
                app = output / "LightTable.app"
                python = app / "Contents/Resources/Python/bin/python3.13"
                python.parent.mkdir(parents=True)
                root.mkdir()
                log = Path(directory) / "inference.json"
                # This stub records which interpreter/path the *real shell
                # block* uses; it never downloads assets or launches an app.
                stub = '''import json, os, pathlib, sys
pathlib.Path(os.environ["SMOKE_LOG"]).write_text(json.dumps({
    "executable": sys.argv[1], "arguments": sys.argv[2:],
    "pythonpath": os.environ.get("PYTHONPATH"),
    "models": os.environ.get("LIGHTTABLE_MODEL_DIR"),
}))
sys.exit(int(os.environ["SMOKE_EXIT"]))
'''
                # Kernel shebang parsing cannot quote a Python path containing
                # spaces. The shell can, and forwards the stub path and argv.
                python.write_text(
                    "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " -c "
                    + shlex.quote(stub) + ' "$0" "$@"\n'
                )
                python.chmod(0o755)
                environment = dict(os.environ, ROOT=str(root), APP=str(app),
                                   OUTPUT_DIR=str(output), SMOKE_LOG=str(log),
                                   SMOKE_EXIT=str(failure))
                result = subprocess.run(["bash", "-euc", block], env=environment,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, failure, result.stderr)
                record = json.loads(log.read_text())
                moved = Path(record["executable"]).parents[4]
                self.assertNotEqual(moved, app)
                self.assertEqual(moved.name, "LightTable moved.app")
                self.assertEqual(record["pythonpath"], str(moved / "Contents/Resources/LightTable"))
                self.assertEqual(record["models"], str(moved / "Contents/Resources/models"))
                self.assertEqual(record["arguments"], [
                    "-B", str(root / "scripts/smoke-hair-mask.py"),
                    "--model-dir", record["models"],
                    "--image", str(root / "tests/fixtures/photos/portrait.jpg"),
                ])
                self.assertTrue(python.is_file(), "original output bundle was not restored")
                self.assertFalse(moved.exists())
                self.assertEqual(list(output.iterdir()), [app])

    def test_personal_update_requires_hashes_and_copies_assets_without_fetching(self):
        source = (ROOT / "scripts/update-personal-app.sh").read_text()
        assets = source.split("HAIR_MODEL_ASSETS=(", 1)[1].split("\n)", 1)[0]
        for asset in ASSETS:
            self.assertIn('"$ROOT/scripts/models/' + asset + '"', assets)
        self.assertIn('for MODEL_ASSET in "${HAIR_MODEL_ASSETS[@]}"; do\n  require_file "$MODEL_ASSET"', source)
        model_hash = source.split('MODEL_HASH="$(hash_sources ', 1)[1].split('\nSOURCE_TREE_HASH=', 1)[0]
        self.assertIn('"${HAIR_MODEL_ASSETS[@]}"', model_hash)
        refresh = source.split('echo "Refreshing the bundled models..."', 1)[1].split('\nelse\n', 1)[0]
        self.assertIn('for MODEL_ASSET in "${HAIR_MODEL_ASSETS[@]}"', refresh)
        self.assertIn('"$STAGE_CONTENTS/Resources/models/$(basename "$MODEL_ASSET")"', refresh)
        self.assertNotIn("fetch-hair-model.py", source)
        self.assertNotIn("uv pip", source)
        self.assertLess(source.index('"$ROOT/scripts/smoke-hair-mask.py"'),
                        source.index('/bin/mv "$INSTALL_APP" "$BACKUP_APP"'))
        self.assertIn('PYTHONPATH="$STAGE_PAYLOAD"', source)
        self.assertIn('--model-dir "$STAGE_CONTENTS/Resources/models"', source)
        self.assertIn('--image "$ROOT/tests/fixtures/photos/portrait.jpg"', source)

    def test_windows_inference_runs_after_relocation_and_before_quick_exit(self):
        source = (ROOT / "scripts/windows/build-release.ps1").read_text()
        move = source.index('Move-Item -Path $Payload -Destination $MovedParent')
        fetch = source.index('"scripts\\fetch-hair-model.py"')
        smoke = source.index('"scripts\\smoke-hair-mask.py"')
        quick = source.index('    if ($RuntimeSmokeOnly) {\n'
                             '        Start-BuildStage $Timing "runtime-smoke"\n'
                             '        & $PythonExe')
        self.assertLess(move, fetch)
        self.assertLess(fetch, smoke)
        self.assertLess(smoke, quick)
        self.assertIn('$Payload = Join-Path $MovedParent "LightTable"', source[move:fetch])
        self.assertIn('$PythonExe = Join-Path $Python "python.exe"', source[move:fetch])
        self.assertIn('$Models = Join-Path $Payload "Resources\\models"', source[move:fetch])
        self.assertIn('$env:PYTHONPATH = $Resources', source[fetch:smoke])
        self.assertIn('--image (Join-Path $Project "tests\\fixtures\\photos\\portrait.jpg")', source[smoke:quick])
        self.assertIn('SetEnvironmentVariable("PYTHONPATH", $PreviousPythonPath, "Process")',
                      source.split('} finally {', 1)[1])

    def test_linux_fetch_is_outside_pure_staging_and_inference_is_relocated(self):
        source = (ROOT / "scripts/linux/build-release.py").read_text()
        tree = ast.parse(source)
        staging = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name == "stage_resources")
        self.assertNotIn("fetch-hair-model.py", ast.get_source_segment(source, staging))
        self.assertLess(source.index('ROOT / "scripts/fetch-hair-model.py"'),
                        source.index('bundle.rename(relocated)'))
        smoke = source.index('ROOT / "scripts/smoke-hair-mask.py"')
        self.assertLess(source.index('bundle.rename(relocated)'), smoke)
        self.assertLess(smoke, source.index('relocated / "runtime-smoke.py"'))
        self.assertIn('run(relocated / "Python/bin/python3", "-B", ROOT / "scripts/smoke-hair-mask.py"', source)
        self.assertIn('hair_environment["PYTHONPATH"] = str(relocated / "Resources/LightTable")', source)
        self.assertIn('"--model-dir", relocated / "Resources/models"', source)
        self.assertIn('"--image", ROOT / "tests/fixtures/photos/portrait.jpg", env=hair_environment', source)


if __name__ == "__main__":
    unittest.main()
