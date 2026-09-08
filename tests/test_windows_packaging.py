from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "windows_uninstall_manifest", ROOT / "scripts/windows/make-uninstall-manifest.py"
)
manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest)
STAGING_SPEC = importlib.util.spec_from_file_location(
    "windows_python_staging", ROOT / "scripts/windows/stage-python-modules.py"
)
staging = importlib.util.module_from_spec(STAGING_SPEC)
STAGING_SPEC.loader.exec_module(staging)
SMOKE_SPEC = importlib.util.spec_from_file_location(
    "windows_runtime_smoke", ROOT / "scripts/windows/runtime-smoke.py"
)
smoke = importlib.util.module_from_spec(SMOKE_SPEC)
SMOKE_SPEC.loader.exec_module(smoke)


class WindowsPythonStagingTests(unittest.TestCase):
    def test_transitive_from_and_lazy_optional_imports_work_outside_source_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "source"
            resources = Path(temporary) / "payload"
            project.mkdir()
            (project / "server.py").write_text(
                "from export_surface import result\n"
                "def optional():\n    import camera_profile\n    return camera_profile.VALUE\n")
            (project / "export_surface.py").write_text("from render_scheduling import result\n")
            (project / "render_scheduling.py").write_text("result = 42\n")
            (project / "camera_profile.py").write_text("VALUE = 16\n")
            (project / "private.env").write_text("not a runtime module")
            (project / "tests").mkdir()
            (project / "tests" / "test_development.py").write_text("raise RuntimeError()\n")
            staged = staging.stage_modules(project, resources)
            self.assertEqual({path.name for path in staged},
                             {"server.py", "export_surface.py", "render_scheduling.py", "camera_profile.py"})
            self.assertEqual(set(resources.iterdir()), set(staged))
            # Isolated Python cannot resolve a missing dependency from either
            # the test process's import path or the development checkout.
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "-c",
                 "import sys; sys.path.insert(0, sys.argv[1]); import server; "
                 "assert server.result == 42; assert server.optional() == 16", str(resources)],
                cwd=temporary, capture_output=True, text=True, timeout=10)
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_release_and_pull_request_checks_use_the_same_staging_path(self):
        build = (ROOT / "scripts/windows/build-release.ps1").read_text()
        workflow = (ROOT / ".github/workflows/windows-build.yml").read_text()
        quick_check = workflow.split("  quick-check:\n", 1)[1].split("\n  package:\n", 1)[0]
        self.assertIn("./scripts/windows/build-release.ps1 -RuntimeSmokeOnly", quick_check)
        self.assertLess(build.index('"stage-python-modules.py"'),
                        build.index('Write-Host "Staged Windows runtime smoke passed"'))
        self.assertLess(build.index('Write-Host "Staged Windows runtime smoke passed"'),
                        build.index("& cargo test"))


class WindowsRuntimeSmokeTests(unittest.TestCase):
    def test_http_smoke_checks_live_routes_and_stops_its_server(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "server.py").write_text("""
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        with Path('requests.txt').open('a') as log:
            log.write(self.path + '\\n')
        body = (b'<html>LightTable</html>' if self.path == '/' else
                json.dumps({'ok': True, 'pid': os.getpid(),
                            'catalog': os.environ['LIGHTTABLE_CATALOG_FILE']}).encode())
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)
server = HTTPServer(('127.0.0.1', 0), Handler)
Path(os.environ['LIGHTTABLE_STARTUP_FILE']).write_text(
    json.dumps({'phase': 'ready', 'port': server.server_port}))
server.serve_forever()
""")
            environment = smoke.smoke_environment(root)
            smoke.check_server_startup(root, root, environment, timeout=5)
            self.assertEqual((root / "requests.txt").read_text().splitlines(),
                             ["/api/health", "/", "/api/options"])

    def test_smoke_isolates_the_catalog_and_support_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.dict(os.environ, {"LIGHTTABLE_CATALOG_FILE": "/user/catalog.sqlite3",
                                               "LIGHTTABLE_WATCH_PARENT": "1"}):
                environment = smoke.smoke_environment(root)
            self.assertNotIn("LIGHTTABLE_WATCH_PARENT", environment)
            for key, value in environment.items():
                if key.endswith(("_FILE", "_DIR")) and key.startswith("LIGHTTABLE_"):
                    self.assertTrue(Path(value).is_relative_to(root), key)

    def test_startup_failure_reports_the_cause_and_reaps_the_child(self):
        self._run_failed_server("raise RuntimeError('missing runtime dependency')\n",
                                "missing runtime dependency", timeout=5)

    def test_startup_timeout_terminates_and_reaps_the_child(self):
        self._run_failed_server("import time\ntime.sleep(30)\n", "did not start", timeout=0.2)

    def _run_failed_server(self, source, expected_error, *, timeout):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "server.py").write_text(source)
            environment = smoke.smoke_environment(root)
            children = []
            real_popen = subprocess.Popen

            def start(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                children.append(process)
                return process

            with mock.patch.object(smoke.subprocess, "Popen", side_effect=start):
                with self.assertRaisesRegex(RuntimeError, expected_error):
                    smoke.check_server_startup(root, root, environment, timeout=timeout)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())


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


class WindowsSigningContractTests(unittest.TestCase):
    def test_reusable_workflow_preserves_source_identity_and_passes_signing_requirement(self):
        workflow = (ROOT / ".github/workflows/windows-build.yml").read_text()
        reusable = workflow.split("  workflow_call:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("      source_ref:", reusable)
        self.assertIn("      require_signing:", reusable)
        self.assertIn("        default: false\n        type: boolean", reusable)
        self.assertEqual(workflow.count("ref: ${{ inputs.source_ref || github.sha }}"), 2)
        self.assertIn("WINDOWS_CERTIFICATE_BASE64: ${{ secrets.WINDOWS_CERTIFICATE_BASE64 }}", workflow)
        self.assertIn("WINDOWS_CERTIFICATE_PASSWORD: ${{ secrets.WINDOWS_CERTIFICATE_PASSWORD }}", workflow)
        self.assertIn("-RequireSigning:($env:REQUIRE_SIGNING -eq 'true')", workflow)
        self.assertLess(workflow.index("name: Validate Windows signing configuration"),
                        workflow.index("name: Install pinned build tools"))

    def test_payload_and_installer_are_signed_before_archiving(self):
        build = (ROOT / "scripts/windows/build-release.ps1").read_text()
        self.assertLess(build.index("-CheckOnly -RequireSigning:$RequireSigning"),
                        build.index("foreach ($Tool"))
        payload_signing = build.index('-RequireSigning -Files @(')
        installer_signing = build.index('-RequireSigning -Files $Installer')
        self.assertLess(payload_signing, build.index('& $MakeNsis.Source'))
        self.assertLess(installer_signing, build.index('"scripts\\windows\\installer-smoke.ps1"'))
        self.assertLess(installer_signing, build.index("Compress-Archive"))
        self.assertIn('authenticode_signed = [bool]$SigningEnabled', build)


class WindowsRuntimeLaunchContractTests(unittest.TestCase):
    """The embedded runtime's ``python313._pth`` implies isolated mode, so the
    interpreter ignores every ``PYTHON*`` variable. Settings that matter must
    travel as interpreter options, and nothing may write into the install tree
    because the uninstaller removes only the files it shipped."""

    def test_desktop_shell_launches_python_with_explicit_options(self):
        shell = (ROOT / "windows-shell/src/main.rs").read_text()
        self.assertIn('.arg("-u")', shell)
        self.assertIn('.arg(format!("pycache_prefix={}", bytecode.display()))', shell)
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", shell)
        self.assertIn('command.env("NUMBA_CACHE_DIR", paths.cache.join("compiled-runtime"))', shell)
        self.assertIn('WebContext::new(Some(paths.support.join("WebView2")))', shell)
        self.assertIn(".with_theme(Some(Theme::Dark))", shell)
        self.assertIn(".with_background_color(BACKGROUND)", shell)

    def test_desktop_shell_starts_servers_off_the_ui_thread_after_stopping_the_last(self):
        shell = (ROOT / "windows-shell/src/main.rs").read_text()
        begin = shell.index("fn begin_server(")
        self.assertLess(shell.index("previous.stop();", begin),
                        shell.index("ServerController::start(&paths, &folder)", begin))
        self.assertIn("thread::spawn(move || {", shell[begin:])
        self.assertIn("load_html(LOADING_PAGE)", shell[begin:])
        self.assertIn("if generation != self.launch_generation {", shell)
        self.assertIn("self.queued = Some(folder);", shell)

    def test_explorer_and_default_application_launches_do_not_flash_consoles(self):
        shell = (ROOT / "windows-shell/src/main.rs").read_text()
        self.assertIn('.raw_arg(format!("/select,\\"{}\\"", path.display()))', shell)
        self.assertNotIn('Command::new("explorer.exe")\n        .arg(format!("/select,{}"', shell)
        self.assertIn('.args(["/C", "start", "", path])\n            .creation_flags(CREATE_NO_WINDOW)', shell)

    def test_cli_wrapper_caches_bytecode_outside_the_installation(self):
        wrapper = (ROOT / "scripts/windows/lighttable.cmd").read_text()
        self.assertIn('-X "pycache_prefix=%LIGHTTABLE_BYTECODE%"', wrapper)
        self.assertNotIn(" -B ", wrapper)
        self.assertIn(r"%LOCALAPPDATA%\LightTable\python-bytecode", wrapper)

    def test_resident_engine_reads_windows_named_file_mappings(self):
        engine = (ROOT / "rust-engine/src/main.rs").read_text()
        self.assertIn("#[cfg(windows)]\nfn load_shared_input", engine)
        for symbol in ("OpenFileMappingW", "MapViewOfFile", "UnmapViewOfFile",
                       "VirtualQuery", "CloseHandle"):
            self.assertIn(symbol, engine)
        self.assertIn("#[cfg(not(any(unix, windows)))]\nfn load_shared_input", engine)
        self.assertIn("#[cfg(all(test, windows))]\nmod windows_shared_input_tests", engine)


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell is required")
class WindowsSigningGateTests(unittest.TestCase):
    def invoke(self, script, *arguments, certificate=None, password=None):
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("WINDOWS_CERTIFICATE_BASE64", "WINDOWS_CERTIFICATE_PASSWORD")}
        if certificate is not None:
            environment["WINDOWS_CERTIFICATE_BASE64"] = certificate
        if password is not None:
            environment["WINDOWS_CERTIFICATE_PASSWORD"] = password
        return subprocess.run(
            [shutil.which("pwsh") or shutil.which("powershell"), "-NoLogo", "-NoProfile",
             "-NonInteractive", "-File", str(ROOT / "scripts/windows" / script), *arguments],
            capture_output=True, text=True, env=environment, timeout=30,
        )

    def test_optional_signing_without_credentials_needs_no_windows_sdk(self):
        result = self.invoke("sign-release.ps1", "-CheckOnly")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("False", result.stdout)

    def test_required_signing_without_credentials_fails_before_a_build_starts(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "not-created"
            result = self.invoke("build-release.ps1", "-RequireSigning", "-OutputDirectory", str(output))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Windows signing requires both", result.stderr)
            self.assertFalse(output.exists())

    def test_partial_configuration_never_silently_produces_an_unsigned_build(self):
        for configuration in ({"certificate": "synthetic-test-certificate"},
                              {"password": "synthetic-test-password"}):
            with self.subTest(configuration=next(iter(configuration))):
                result = self.invoke("sign-release.ps1", "-CheckOnly", **configuration)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Windows signing requires both", result.stderr)
                for value in configuration.values():
                    self.assertNotIn(value, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
