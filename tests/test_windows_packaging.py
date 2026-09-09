from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


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


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell is required")
class WindowsPayloadMetadataTests(unittest.TestCase):
    def test_real_build_writes_metadata_to_the_payload_without_stray_files(self):
        # Execute only the build's actual metadata writes. PowerShell dynamic
        # parameter binding can silently swap positional Path and Value args.
        writes = [line.strip() for line in (ROOT / "scripts/windows/build-release.ps1").read_text().splitlines()
                  if "Set-Content" in line and any(name in line for name in ('"install-channel.txt"', '"VERSION.txt"'))]
        self.assertEqual(len(writes), 2)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload, engine = root / "payload with spaces", root / "engine with spaces"
            payload.mkdir()
            engine.mkdir()
            environment = dict(os.environ, LIGHTTABLE_METADATA_PAYLOAD=str(payload),
                               LIGHTTABLE_METADATA_ENGINE=str(engine))
            script = '$ErrorActionPreference="Stop"; $Payload=$env:LIGHTTABLE_METADATA_PAYLOAD; '
            script += '$Engine=$env:LIGHTTABLE_METADATA_ENGINE; $RustSourceRevision="a" * 40;\n'
            result = subprocess.run([shutil.which("pwsh") or shutil.which("powershell"),
                                     "-NoProfile", "-Command", script + "\n".join(writes)],
                                    cwd=root, env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((payload / "install-channel.txt").read_bytes(), b"portable")
            self.assertEqual((engine / "VERSION.txt").read_text().strip(), "a" * 40)
            self.assertEqual({path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()},
                             {"payload with spaces/install-channel.txt", "engine with spaces/VERSION.txt"})


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

    def test_timeout_preserves_last_phase_and_reports_later_read_error_without_tokens(self):
        startup = {"phase": "listening", "detail": "Starting the local server", "updatedAt": 123.5,
                   "pid": 4321, "port": 12345, "token": "SECRET_TOKEN",
                   "environment": {"PRIVATE_KEY": "SECRET_ENV"}}
        real_read_text = Path.read_text
        reads = []
        def read(path, *args, **kwargs):
            if path.name != "startup.json":
                return real_read_text(path, *args, **kwargs)
            reads.append(path)
            if len(reads) == 1:
                return json.dumps(startup)
            raise PermissionError(13, "Permission denied", "SECRET_FILENAME")
        with mock.patch.object(smoke.Path, "read_text", read):
            error = self._run_failed_server("import time\ntime.sleep(30)\n", "did not start", timeout=0.3)
        self.assertGreaterEqual(len(reads), 2)
        message = str(error)
        diagnostic = json.loads(message.split("startup diagnostics: ", 1)[1].split("\n", 1)[0])
        self.assertEqual(diagnostic["startup"], {key: startup[key] for key in
                                                ("phase", "detail", "updatedAt", "pid", "port")})
        self.assertEqual(diagnostic["last_read_error"]["type"], "PermissionError")
        self.assertEqual(diagnostic["last_read_error"]["errno"], 13)
        self.assertEqual(diagnostic["last_read_error"]["message"], "Permission denied")
        self.assertNotIn("SECRET", message)

    def test_timeout_reports_json_read_error_without_copying_the_invalid_document(self):
        real_read_text = Path.read_text
        def read(path, *args, **kwargs):
            if path.name == "startup.json":
                return '{"token": "SECRET_TOKEN", "phase": '
            return real_read_text(path, *args, **kwargs)
        with mock.patch.object(smoke.Path, "read_text", read):
            error = self._run_failed_server("import time\ntime.sleep(30)\n", "did not start", timeout=0.2)
        message = str(error)
        diagnostic = json.loads(message.split("startup diagnostics: ", 1)[1].split("\n", 1)[0])
        self.assertEqual(diagnostic["startup"], {})
        self.assertEqual(diagnostic["last_read_error"]["type"], "JSONDecodeError")
        self.assertEqual(diagnostic["last_read_error"]["message"], "Expecting value")
        self.assertNotIn("SECRET", message)

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
                with self.assertRaisesRegex(RuntimeError, expected_error) as raised:
                    smoke.check_server_startup(root, root, environment, timeout=timeout)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            return raised.exception


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
        self.assertEqual(workflow.count("ref: ${{ inputs.source_ref || github.sha }}"), 3)
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
        self.assertIn('WebContext::new(Some(paths.support.join(profile_name)))', shell)
        self.assertIn('"WebView2"', shell)
        self.assertIn('"WebKitGTK"', shell)
        self.assertRegex(shell, r'\.with_theme\(if cfg!\(target_os = "linux"\) \{\s*None\s*\} else \{\s*Some\(Theme::Dark\)\s*\}\)')
        self.assertIn(".with_background_color(BACKGROUND)", shell)

    def test_desktop_shell_starts_servers_off_the_ui_thread_after_stopping_the_last(self):
        shell = (ROOT / "windows-shell/src/main.rs").read_text()
        begin = shell.index("fn begin_server(")
        self.assertLess(shell.index("previous.stop();", begin),
                        shell.index("ServerController::start(&paths, &folder)", begin))
        self.assertIn("thread::spawn(move || {", shell[begin:])
        self.assertIn("load_html(&loading_page())", shell[begin:])
        self.assertIn("if generation != self.launch_generation {", shell)
        self.assertIn("self.queued = Some(folder);", shell)

    def test_explorer_and_default_application_launches_do_not_flash_consoles(self):
        shell = (ROOT / "windows-shell/src/main.rs").read_text()
        self.assertIn('.raw_arg(format!("/select,\\"{}\\"", path.display()))', shell)
        self.assertNotIn('Command::new("explorer.exe")\n        .arg(format!("/select,{}"', shell)
        opener = shell.split("fn open_with_default_application(", 1)[1].split("fn reveal(", 1)[0]
        self.assertIn("ShellExecuteW", opener)
        self.assertIn("file.as_ptr()", opener)
        self.assertNotIn('Command::new("cmd.exe")', opener)
        self.assertIn("if result <= 32", opener)

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
    def invoke(self, script, *arguments, certificate=None, password=None, azure=None):
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("WINDOWS_CERTIFICATE_BASE64", "WINDOWS_CERTIFICATE_PASSWORD")
                       and not key.startswith(("AZURE_", "ACTIONS_ID_TOKEN_", "GITHUB_"))}
        if certificate is not None:
            environment["WINDOWS_CERTIFICATE_BASE64"] = certificate
        if password is not None:
            environment["WINDOWS_CERTIFICATE_PASSWORD"] = password
        environment.update(azure or {})
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

    AZURE = {
        "AZURE_SIGNING_ENDPOINT": "https://eus.codesigning.azure.net/",
        "AZURE_SIGNING_ACCOUNT": "fixture-account",
        "AZURE_SIGNING_PROFILE": "fixture-profile",
        "AZURE_TENANT_ID": "00000000-0000-0000-0000-000000000001",
        "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000002",
    }

    def test_partial_azure_configuration_fails_closed_without_leaking_values(self):
        for missing in self.AZURE:
            with self.subTest(missing=missing):
                config = {key: value for key, value in self.AZURE.items() if key != missing}
                result = self.invoke("sign-release.ps1", "-CheckOnly", azure=config)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"requires {missing}", result.stderr)
                self.assertNotIn("fixture-account", result.stderr)

    def test_azure_and_pfx_configuration_is_rejected(self):
        result = self.invoke("sign-release.ps1", "-CheckOnly", azure=self.AZURE,
                             certificate="synthetic-test-certificate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("never both", result.stderr)

    def test_azure_rejects_an_untrusted_endpoint_before_authentication(self):
        for endpoint in ("http://eus.codesigning.azure.net/", "https://example.com/",
                         "https://eus.codesigning.azure.net.evil.example/",
                         "https://eus.codesigning.azure.net/?token=synthetic"):
            result = self.invoke("sign-release.ps1", "-CheckOnly",
                                 azure={**self.AZURE, "AZURE_SIGNING_ENDPOINT": endpoint})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("HTTPS regional", result.stderr)

    def test_azure_rejects_untrusted_build_contexts(self):
        trusted = {**self.AZURE, "GITHUB_ACTIONS": "true",
                   "GITHUB_REPOSITORY": "reville/lighttable-digital-darkroom",
                   "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main"}
        for override in ({"GITHUB_REF": "refs/heads/topic"},
                         {"GITHUB_REF": "refs/tags/arbitrary"},
                         {"GITHUB_EVENT_NAME": "pull_request"},
                         {"GITHUB_REPOSITORY": "someone-else/fork"},
                         {"GITHUB_ACTIONS": "false"}):
            result = self.invoke("sign-release.ps1", "-CheckOnly", azure={**trusted, **override})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("restricted to this repository", result.stderr)
        for ref in ("refs/heads/main", "refs/tags/v0.5.0", "refs/tags/v0.5.0-rc.1"):
            result = self.invoke("sign-release.ps1", "-CheckOnly", azure={**trusted, "GITHUB_REF": ref})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires GitHub OIDC", result.stderr)

    def test_azure_failure_redacts_sdk_headers_and_removes_temporary_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            signer = folder / "fake-signtool.ps1"
            signer.write_text(
                'Write-Output "Set-Cookie: private-cookie-for-test"\n'
                'Write-Output "AADSTS700213 private-assertion-for-test"\nexit 1\n')
            dlib = folder / "fake.dll"
            executable = folder / "fixture.exe"
            dlib.touch()
            executable.touch()
            environment = {key: value for key, value in os.environ.items()
                           if key not in ("WINDOWS_CERTIFICATE_BASE64", "WINDOWS_CERTIFICATE_PASSWORD")
                           and not key.startswith(("AZURE_", "ACTIONS_ID_TOKEN_", "GITHUB_"))}
            environment.update({
                **self.AZURE, "OS": "Windows_NT", "GITHUB_ACTIONS": "true",
                "GITHUB_REPOSITORY": "reville/lighttable-digital-darkroom",
                "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
                "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.invalid/?fixture=true",
                "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "private-request-token-for-test",
                "AZURE_SIGNING_SIGNTOOL": str(signer), "AZURE_SIGNING_DLIB": str(dlib),
                "TEMP": temporary, "TMP": temporary, "TMPDIR": temporary,
            })
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            command = (
                "function Invoke-RestMethod { @{ value = 'private-assertion-for-test' } }; "
                f"& {quote(ROOT / 'scripts/windows/sign-release.ps1')} -RequireSigning -Files {quote(executable)}"
            )
            result = subprocess.run(
                [shutil.which("pwsh") or shutil.which("powershell"), "-NoLogo", "-NoProfile",
                 "-NonInteractive", "-Command", command], env=environment,
                capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("AADSTS700213", result.stderr)
            for secret in ("private-cookie-for-test", "private-assertion-for-test", "private-request-token-for-test"):
                self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertEqual(list(folder.glob("lighttable-signing-*")), [])


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell is required")
class WindowsSignatureReportTests(unittest.TestCase):
    def test_finished_package_layout_source_identity_and_timestamps(self):
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        # This is the shipped runtime layout: Python resources are nested under
        # Resources/LightTable, independently of the verifier's implementation.
        payload = (
            "LightTable.exe", "WinSparkle.dll",
            "Resources/LightTable/engine/lighttable-engine.exe",
            "Resources/LightTable/engine/spektrafilm-rs.exe",
        )
        for case in ("valid", "wrong-source", "missing-timestamp"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                archive = folder / "LightTable.zip"
                installer = folder / "LightTable-0.5.0-windows-x64-setup.exe"
                report = folder / "signatures.json"
                installer.write_bytes(b"installer fixture")
                with zipfile.ZipFile(archive, "w") as bundle:
                    for path in payload:
                        bundle.writestr("LightTable/" + path, b"executable fixture")
                    bundle.writestr("LightTable/build-manifest.json", json.dumps({
                        "version": "0.5.0", "authenticode_signed": True,
                        "source_revision": "0" * 40 if case == "wrong-source" else revision,
                    }))
                quote = lambda value: "'" + str(value).replace("'", "''") + "'"
                # Stub only the native certificate API; use real ZIP extraction,
                # files, hashes, manifest loading, and source-revision checks.
                timestamp = "$null" if case == "missing-timestamp" else "@{ Subject = 'fixture timestamp authority' }"
                command = (
                    "function Get-AuthenticodeSignature { param($LiteralPath) "
                    "if (-not (Test-Path -LiteralPath $LiteralPath -PathType Leaf)) { throw 'Packaged executable not found' }; "
                    "[pscustomobject]@{ Status = 'Valid'; SignerCertificate = @{ Subject = 'fixture publisher'; Thumbprint = 'abc' }; "
                    f"TimeStamperCertificate = {timestamp} }} }}; "
                    f"& {quote(ROOT / 'scripts/windows/verify-release-signatures.ps1')} "
                    f"-Archive {quote(archive)} -Installer {quote(installer)} -Report {quote(report)}"
                )
                result = subprocess.run(
                    [shutil.which("pwsh") or shutil.which("powershell"), "-NoLogo", "-NoProfile",
                     "-NonInteractive", "-Command", command], cwd=ROOT,
                    capture_output=True, text=True, timeout=30)
                if case != "valid":
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("source revision" if case == "wrong-source" else "timestamped Authenticode", result.stderr)
                    self.assertFalse(report.exists())
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
                evidence = json.loads(report.read_text(encoding="utf-8-sig"))
                self.assertEqual(evidence["source_revision"], revision)
                self.assertEqual(evidence["version"], "0.5.0")
                self.assertEqual({item["file"] for item in evidence["signatures"]},
                                 {Path(path).name for path in payload} | {installer.name})
                for item in evidence["signatures"]:
                    self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
