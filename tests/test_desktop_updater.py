# SPDX-License-Identifier: GPL-3.0-only
"""Real signed archives, filesystem upgrades and process/HTTP restart fixtures.

The worker suite runs on Linux and macOS. macOS overrides only the platform gate
and /proc identity lookup; all crypto, extraction, integration, subprocesses and
HTTP checks are real. Native desktop-shell validation remains a separate gate.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import desktop_updater as update


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load("updater_test_integration", "scripts/linux/desktop-integration.py")
signer = load("updater_test_signer", "scripts/generate-linux-update.py")


class PortableUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data, self.state, self.commands = (self.root / name for name in ("custom data", "custom state", "custom commands"))
        self.environment = patch.dict(os.environ, {
            "XDG_DATA_HOME": str(self.data), "XDG_STATE_HOME": str(self.state),
            "XDG_CONFIG_HOME": str(self.root / "config"), "XDG_CACHE_HOME": str(self.root / "cache"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        if update.platform.machine() == "arm64":
            architecture_patch = patch.object(update.platform, "machine", return_value="aarch64")
            architecture_patch.start()
            self.addCleanup(architecture_patch.stop)
        self.key = Ed25519PrivateKey.generate()
        self.public = base64.b64encode(self.key.public_key().public_bytes_raw()).decode()
        self.bundle = self.make_bundle("original bundle", "0.5.0")
        self.new = self.make_bundle("next bundle", "0.5.1")
        with contextlib.redirect_stdout(io.StringIO()):
            installer.integrate("install", self.bundle, self.commands)
        self.updater = update.LinuxUpdater(self.bundle)
        self.authorization = self.root / "cache/lighttable/updates/pending-0123456789abcdef"
        self.authorization.mkdir(mode=0o700, parents=True)
        (self.authorization / "authorized").write_text("yes")
        self.archive = self.root / "release.tar.gz"
        self.feed = self.root / "feed.json"
        self.sign()
        self.real_platform = sys.platform
        if sys.platform != "linux":
            platform_patch = patch.object(update.sys, "platform", "linux")
            platform_patch.start()
            self.addCleanup(platform_patch.stop)
            process_patch = patch.object(update, "process_identity", side_effect=self.portable_identity)
            process_patch.start()
            self.addCleanup(process_patch.stop)
        self.fetch_patch = patch.object(update, "fetch", side_effect=self.fixture_fetch)
        self.fetch = self.fetch_patch.start()
        self.addCleanup(self.fetch_patch.stop)
        self.processes = []
        self.addCleanup(self.stop_processes)
        self.photos = self.root / "photos"
        self.photos.mkdir()
        (self.photos / "original.raw").write_bytes(b"original photo")
        self.catalog = self.data / "lighttable/Catalog/library.sqlite3"
        self.catalog.parent.mkdir(parents=True)
        self.catalog.write_bytes(b"catalog owned only by server")

    @staticmethod
    def portable_identity(pid):
        if type(pid) is not int or pid <= 1:
            raise update.UpdateError("Invalid process to wait for")
        try:
            os.kill(pid, 0)
            return "live"
        except ProcessLookupError:
            return None

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)

    def make_bundle(self, name, version, *, fail=False):
        bundle = self.root / name
        for relative in ("bin/lighttable", "bin/lighttable-desktop-shell", "Python/bin/python3",
                         "share/icons/lighttable.png", "Resources/LightTable/server.py"):
            path = bundle / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        (bundle / "bin/lighttable-desktop").write_text(
            f"#!{Path(sys.executable).resolve()}\n" + ("raise SystemExit(7)\n" if fail else
            "import http.server,json,os,pathlib\n"
            "class Handler(http.server.BaseHTTPRequestHandler):\n"
            " def do_GET(self):\n"
            "  self.send_response(200); self.end_headers(); self.wfile.write(b'{}')\n"
            " def log_message(self,*args): pass\n"
            "server=http.server.HTTPServer(('127.0.0.1',0),Handler)\n"
            "ready=pathlib.Path(os.environ['LIGHTTABLE_UPDATE_READY_FILE'])\n"
            "ready.with_name('observed-restart.json').write_text(json.dumps(dict(os.environ)))\n"
            "ready.write_text(json.dumps({'pid':os.getpid(),'port':server.server_port}))\n"
            "server.serve_forever()\n"))
        (bundle / "bin/lighttable-desktop").chmod(0o755)
        shutil.copy2(ROOT / "scripts/linux/desktop-integration.py", bundle / "desktop-integration.py")
        (bundle / "build-manifest.json").write_text(json.dumps({
            "version": version, "platform": "linux", "architecture": update.platform.machine(),
            "source_dirty": False, "source_revision": "a" * 40,
        }))
        (bundle / "installation-owner.json").write_text('{"owner":"portable"}')
        (bundle / "update-config.json").write_text(json.dumps({
            "public_key": self.public,
            "feed_url": "https://github.com/reville/lighttable-digital-darkroom/releases/download/desktop-updates/linux-test.json",
        }))
        return bundle

    def sign(self):
        with tarfile.open(self.archive, "w:gz", dereference=False) as stream:
            stream.add(self.new, arcname="LightTable")
        signer.generate(self.archive, self.feed, key=self.key,
                        download_url="https://github.com/reville/lighttable-digital-darkroom/releases/download/v0.5.1/release.tar.gz",
                        release_notes_url="https://github.com/reville/lighttable-digital-darkroom/releases/tag/v0.5.1")

    def fixture_fetch(self, url, output=None, *, limit, hosts):
        source = self.archive if output else self.feed
        self.assertLessEqual(source.stat().st_size, limit)
        if output:
            shutil.copyfile(source, output)
        else:
            return source.read_bytes()

    def ready(self):
        self.assertEqual(self.updater.check()["state"], "available")
        self.assertEqual(self.updater.download()["state"], "ready")

    def assert_data_preserved(self):
        self.assertEqual(self.catalog.read_bytes(), b"catalog owned only by server")
        self.assertEqual((self.photos / "original.raw").read_bytes(), b"original photo")
        self.assertTrue(self.bundle.exists())

    def test_signed_upgrade_waits_for_exit_preserves_paths_data_and_verifies_live_http(self):
        self.ready()
        old_process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.processes.append(old_process)
        armed = self.updater.state_dir / "armed.json"
        errors = []
        def stop_after_armed():
            try:
                deadline = time.monotonic() + 10
                while not armed.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(armed.exists())
                self.assertEqual((self.commands / "lighttable").resolve(), self.bundle / "bin/lighttable")
                old_process.terminate()
                old_process.wait(timeout=5)
            except Exception as error:
                errors.append(error)
        stopper = threading.Thread(target=stop_after_armed)
        stopper.start()
        original_restart = self.updater._restart
        def track_restart(*args):
            process = original_restart(*args)
            self.processes.append(process)
            return process
        with patch.object(self.updater, "_restart", side_effect=track_restart), patch.dict(os.environ, {
                "LIGHTTABLE_PORT": "old port", "LIGHTTABLE_PARENT_PID": "old parent", "PYTHONPATH": "/old/code"}):
            result = self.updater.apply(wait_pids=[old_process.pid], armed_file=armed,
                authorization_directory=self.authorization, launch_env={
                "LIGHTTABLE_DIR": str(self.photos), "LIGHTTABLE_CATALOG_FILE": str(self.catalog)})
        stopper.join(timeout=10)
        self.assertFalse(stopper.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result["state"], "installed")
        installed = Path(result["installed_bundle"])
        self.assertTrue(installed.is_relative_to(self.data / "lighttable/versions"))
        self.assertEqual((self.commands / "lighttable").resolve(), installed / "bin/lighttable")
        self.assertEqual(json.loads(self.updater.integration_file.read_text())["bin_dir"], str(self.commands))
        observed = json.loads((self.updater.state_dir / "observed-restart.json").read_text())
        self.assertEqual(observed["LIGHTTABLE_DIR"], str(self.photos))
        self.assertEqual(observed["LIGHTTABLE_CATALOG_FILE"], str(self.catalog))
        self.assertEqual(observed["XDG_DATA_HOME"], str(self.data))
        self.assertNotIn("LIGHTTABLE_PORT", observed)
        self.assertNotIn("LIGHTTABLE_PARENT_PID", observed)
        self.assertNotIn("PYTHONPATH", observed)
        self.assert_data_preserved()

    def test_relaunch_failure_restores_original_launchers_and_does_not_reopen_old_app(self):
        (self.new / "bin/lighttable-desktop").write_text("#!/bin/sh\nexit 7\n")
        self.sign()
        self.ready()
        original_restart = self.updater._restart
        with patch.object(self.updater, "_restart", wraps=original_restart) as restart:
            with self.assertRaisesRegex(update.UpdateError, "exited"):
                self.updater.apply(wait_pids=[99999999], authorization_directory=self.authorization)
        self.assertEqual(restart.call_count, 1)
        self.assertEqual((self.commands / "lighttable").resolve(), self.bundle / "bin/lighttable")
        self.assertTrue(self.updater.status()["launchers_restored"])
        self.assert_data_preserved()

    def test_live_application_timeout_never_changes_launchers(self):
        self.ready()
        with patch.object(update, "process_identity", return_value="live"), patch.object(update, "EXIT_TIMEOUT", 0):
            with self.assertRaisesRegex(update.UpdateError, "still running"):
                self.updater.apply(wait_pids=[12345], authorization_directory=self.authorization)
        self.assertFalse((self.data / "lighttable/versions").exists())
        self.assertEqual((self.commands / "lighttable").resolve(), self.bundle / "bin/lighttable")

    def test_cancelled_shutdown_never_installs_after_application_exits(self):
        self.ready()
        (self.authorization / "cancelled").write_text("yes")
        with self.assertRaisesRegex(update.UpdateError, "cancelled"):
            self.updater.apply(wait_pids=[99999999], authorization_directory=self.authorization)
        self.assertFalse((self.data / "lighttable/versions").exists())
        self.assert_data_preserved()

    def test_reaped_worker_cancellation_revalidates_archive_and_restores_retry(self):
        self.ready()
        checks_before = self.fetch.call_count
        worker = subprocess.Popen([sys.executable, "-c",
            "import sys,time; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
            "from desktop_updater import LinuxUpdater; updater=LinuxUpdater(Path(sys.argv[2]),Path(sys.argv[3])); "
            "\nwith updater._lock():\n updater._save('applying'); time.sleep(60)\n",
            str(ROOT), str(self.bundle), str(self.updater.state_dir)], start_new_session=True)
        self.processes.append(worker)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.updater.status()["state"] != "applying":
            time.sleep(0.01)
        self.assertEqual(self.updater.status()["state"], "applying")
        worker.terminate()
        worker.wait(timeout=5)
        self.assertEqual(self.updater.cancel_apply()["state"], "ready")
        self.assertEqual(self.fetch.call_count, checks_before)
        original_restart = self.updater._restart
        def track_restart(*args):
            child = original_restart(*args)
            self.processes.append(child)
            return child
        with patch.object(self.updater, "_restart", side_effect=track_restart):
            result = self.updater.apply(wait_pids=[99999999], authorization_directory=self.authorization)
        self.assertEqual(result["state"], "installed")
        self.assert_data_preserved()

    def test_cancel_recovery_handles_tampered_stage_without_network_or_exception(self):
        self.ready()
        checks_before = self.fetch.call_count
        self.updater.archive_file.write_bytes(b"tampered")
        self.assertEqual(self.updater.cancel_apply()["state"], "available")
        self.updater.envelope_file.write_text("invalid signature metadata")
        self.assertEqual(self.updater.cancel_apply()["state"], "error")
        self.assertEqual(self.fetch.call_count, checks_before)
        self.assertEqual((self.commands / "lighttable").resolve(), self.bundle / "bin/lighttable")

    def test_restart_preserves_runtime_xdg_when_installation_uses_other_data_directory(self):
        runtime_data = self.root / "runtime-data"
        self.ready()
        original_restart = self.updater._restart
        def track_restart(*args):
            child = original_restart(*args)
            self.processes.append(child)
            return child
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(runtime_data)}), \
                patch.object(self.updater, "_restart", side_effect=track_restart):
            result = self.updater.apply(wait_pids=[99999999], authorization_directory=self.authorization,
                                       launch_env={"XDG_DATA_HOME": str(runtime_data)})
        observed = json.loads((self.updater.state_dir / "observed-restart.json").read_text())
        self.assertEqual(observed["XDG_DATA_HOME"], str(runtime_data))
        self.assertEqual(json.loads(self.updater.integration_file.read_text())["data_dir"], str(self.data))
        self.assertTrue(Path(result["installed_bundle"]).is_relative_to(self.data / "lighttable/versions"))

    def test_missing_native_authorization_times_out_without_installing(self):
        self.ready()
        (self.authorization / "authorized").unlink()
        with patch.object(update, "EXIT_TIMEOUT", 0), self.assertRaisesRegex(update.UpdateError, "not authorized"):
            self.updater.apply(wait_pids=[99999999], authorization_directory=self.authorization)
        self.assertFalse((self.data / "lighttable/versions").exists())

    def test_untrusted_authorization_directory_and_symlink_are_rejected(self):
        self.ready()
        for path in (None, self.root / "elsewhere"):
            with self.subTest(path=path), self.assertRaises(update.UpdateError):
                self.updater.apply(wait_pids=[99999999], authorization_directory=path)
        (self.authorization / "authorized").unlink()
        (self.authorization / "authorized").symlink_to(self.photos / "original.raw")
        with self.assertRaisesRegex(update.UpdateError, "authorization"):
            self.updater.apply(wait_pids=[99999999], authorization_directory=self.authorization)

    def test_apply_rechecks_bytes_after_download_and_never_executes_tampered_archive(self):
        self.ready()
        with self.updater.archive_file.open("ab") as output:
            output.write(b"tampering")
        with patch.object(self.updater, "_restart") as restart:
            with self.assertRaisesRegex(update.UpdateError, "size"):
                self.updater.apply(wait_pids=[99999999], authorization_directory=self.authorization)
            restart.assert_not_called()
        self.assertEqual((self.commands / "lighttable").resolve(), self.bundle / "bin/lighttable")
        self.assert_data_preserved()

    def test_unconfigured_and_package_managed_installs_do_not_contact_network(self):
        for owner in ("arch", "snap", "flatpak", "development"):
            with self.subTest(owner=owner):
                if owner == "development":
                    (self.bundle / "installation-owner.json").unlink()
                else:
                    (self.bundle / "installation-owner.json").write_text(json.dumps({"owner": owner}))
                self.assertFalse(self.updater.status()["supported"])
                with self.assertRaises(update.UpdateError):
                    self.updater.check()
        (self.bundle / "installation-owner.json").write_text('{"owner":"portable"}')
        (self.bundle / "update-config.json").write_text('{"public_key":""}')
        self.assertFalse(self.updater.status()["supported"])
        self.fetch.assert_not_called()

    def test_flatpak_and_snap_environment_override_portable_marker(self):
        for key, owner in (("SNAP", "snap"), ("FLATPAK_ID", "flatpak")):
            with patch.dict(os.environ, {key: "test"}):
                self.assertEqual(self.updater.status()["owner"], owner)
                self.assertFalse(self.updater.status()["supported"])

    def test_missing_or_other_bundle_integration_is_disabled(self):
        record = json.loads(self.updater.integration_file.read_text())
        record["bundle"] = "/another/bundle"
        self.updater.integration_file.write_text(json.dumps(record))
        self.assertFalse(self.updater.status()["supported"])
        self.fetch.assert_not_called()

    def test_signature_tampering_and_foreign_signer_are_rejected(self):
        for change in ("content", "signer"):
            self.sign()
            value = json.loads(self.feed.read_bytes())
            if change == "content":
                value["signed"]["version"] = "99.0.0"
            else:
                value["signature"] = base64.b64encode(Ed25519PrivateKey.generate().sign(update.canonical_json(value["signed"]))).decode()
            self.feed.write_text(json.dumps(value))
            with self.subTest(change=change), self.assertRaisesRegex(update.UpdateError, "signature"):
                self.updater.check()
            self.assertFalse(self.updater.archive_file.exists())

    def rewrite_signed(self, **fields):
        envelope = json.loads(self.feed.read_bytes())
        envelope["signed"].update(fields)
        envelope["signature"] = base64.b64encode(self.key.sign(update.canonical_json(envelope["signed"]))).decode()
        self.feed.write_text(json.dumps(envelope))

    def test_wrong_platform_architecture_downgrade_expiry_and_http_are_rejected(self):
        for fields in ({"platform": "windows"}, {"architecture": "wrong-architecture"}, {"version": "0.4.9"},
                       {"version": "0.5.2-beta"}, {"expires_at": int(time.time()) - 1},
                       {"url": "http://github.com/release"}, {"url": "https://evil.invalid/release"},
                       {"size": update.MAX_ARCHIVE + 1}):
            self.sign()
            self.rewrite_signed(**fields)
            with self.subTest(fields=fields), self.assertRaises(update.UpdateError):
                self.updater.check()

    def test_feed_cannot_replay_release_older_than_previously_seen(self):
        self.updater.check()
        self.rewrite_signed(version="0.5.0")
        with self.assertRaisesRegex(update.UpdateError, "previously seen"):
            self.updater.check()

    def test_concurrent_update_operation_is_rejected(self):
        with self.updater._lock(), self.assertRaisesRegex(update.UpdateError, "Another update"):
            self.updater.check()

    def test_download_failure_removes_partial_and_retains_original(self):
        self.updater.check()
        def fail(url, output, **kwargs):
            output.write_bytes(b"partial")
            raise OSError("Connection closed")
        with patch.object(update, "fetch", side_effect=fail), self.assertRaises(OSError):
            self.updater.download()
        self.assertFalse((self.updater.state_dir / "release.partial").exists())
        self.assertFalse(self.updater.archive_file.exists())
        self.assertEqual((self.commands / "lighttable").resolve(), self.bundle / "bin/lighttable")

    def test_payload_version_mismatch_rejected_before_switch(self):
        self.ready()
        envelope = json.loads(self.updater.envelope_file.read_bytes())
        envelope["signed"]["version"] = "0.5.2"
        envelope["signature"] = base64.b64encode(self.key.sign(update.canonical_json(envelope["signed"]))).decode()
        self.updater.envelope_file.write_text(json.dumps(envelope))
        with self.assertRaisesRegex(update.UpdateError, "Bundled version"):
            self.updater.apply(wait_pids=[99999999], authorization_directory=self.authorization)
        self.assertEqual((self.commands / "lighttable").resolve(), self.bundle / "bin/lighttable")

    def test_signer_requires_embedded_matching_public_key(self):
        with self.assertRaisesRegex(update.UpdateError, "does not match"):
            signer.generate(self.archive, self.feed, key=Ed25519PrivateKey.generate(),
                            download_url="https://github.com/release", release_notes_url="https://github.com/notes")

    def test_integration_disk_failure_restores_every_original_launcher_and_record(self):
        files = list(map(Path, json.loads(self.updater.integration_file.read_bytes())["files"])) + [self.updater.integration_file]
        before = installer.snapshot(files)
        original = installer.atomic_link
        calls = 0
        def fail_second(path, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated disk error")
            return original(path, target)
        with patch.object(installer, "atomic_link", side_effect=fail_second), self.assertRaises(OSError):
            installer.integrate("install", self.new, self.commands)
        self.assertEqual(installer.snapshot(files), before)
        self.assert_data_preserved()

    def test_interrupted_integration_recovers_durable_journal_before_retry(self):
        original = installer.atomic_link
        calls = 0
        def interrupt_second(path, target):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("simulate abrupt process exit")
            return original(path, target)
        with patch.object(installer, "atomic_link", side_effect=interrupt_second), self.assertRaises(KeyboardInterrupt):
            installer.integrate("install", self.new, self.commands)
        journal = self.updater.integration_file.with_suffix(".pending.json")
        self.assertTrue(journal.exists())
        with contextlib.redirect_stdout(io.StringIO()):
            installer.integrate("install", self.bundle, self.commands)
        self.assertFalse(journal.exists())
        self.assertEqual((self.commands / "lighttable").resolve(), self.bundle / "bin/lighttable")
        self.assertEqual((self.commands / "lighttable-desktop").resolve(), self.bundle / "bin/lighttable-desktop")
        self.assert_data_preserved()

    def test_recovery_journal_cannot_name_unrelated_paths(self):
        unrelated = self.root / "personal.txt"
        unrelated.write_text("keep")
        journal = self.updater.integration_file.with_suffix(".pending.json")
        journal.write_text(json.dumps({str(unrelated): None}))
        with self.assertRaisesRegex(ValueError, "unexpected paths"):
            installer.integrate("install", self.new, self.commands)
        self.assertEqual(unrelated.read_text(), "keep")


class ArchiveSafetyTests(unittest.TestCase):
    def test_https_fetch_enforces_declared_and_streamed_size_limits(self):
        for payload, length, limit, accepted in ((b"ok", "2", 2, True), (b"large", "5", 2, False),
                                                 (b"large", None, 2, False), (b"x", "2", 2, False)):
            response = io.BytesIO(payload)
            response.url = "https://github.com/release"
            response.headers = {"Content-Length": length} if length else {}
            opener = Mock()
            opener.open.return_value = response
            with self.subTest(payload=payload, length=length), patch.object(update, "build_opener", return_value=opener):
                if accepted:
                    self.assertEqual(update.fetch(response.url, limit=limit, hosts={"github.com"}), payload)
                else:
                    with self.assertRaises(update.UpdateError):
                        update.fetch(response.url, limit=limit, hosts={"github.com"})

    def validate(self, entries):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as stream:
            root = tarfile.TarInfo("LightTable")
            root.type = tarfile.DIRTYPE
            stream.addfile(root)
            for entry in entries:
                stream.addfile(entry, io.BytesIO(b"x" * entry.size) if entry.isfile() else None)
        raw.seek(0)
        with tarfile.open(fileobj=raw) as stream:
            return update.archive_members(stream)

    def test_traversal_absolute_duplicate_and_special_entries_rejected(self):
        for name in ("../escape", "/absolute", "LightTable/../../escape", "LightTable/a/../b", "LightTable//b"):
            with self.subTest(name=name), self.assertRaises(update.UpdateError):
                self.validate([tarfile.TarInfo(name)])
        for kind in (tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            entry = tarfile.TarInfo("LightTable/special")
            entry.type = kind
            entry.linkname = "../../escape"
            with self.subTest(kind=kind), self.assertRaises(update.UpdateError):
                self.validate([entry])
        with self.assertRaisesRegex(update.UpdateError, "Duplicate"):
            self.validate([tarfile.TarInfo("LightTable/file"), tarfile.TarInfo("LightTable/file")])

    def test_symbolic_link_escapes_and_writes_through_links_rejected(self):
        for target in ("/tmp/escape", "../../escape"):
            entry = tarfile.TarInfo("LightTable/link")
            entry.type, entry.linkname = tarfile.SYMTYPE, target
            with self.subTest(target=target), self.assertRaises(update.UpdateError):
                self.validate([entry])
        link = tarfile.TarInfo("LightTable/link")
        link.type, link.linkname = tarfile.SYMTYPE, "inside"
        with self.assertRaisesRegex(update.UpdateError, "writes through"):
            self.validate([link, tarfile.TarInfo("LightTable/link/file")])

    def test_safe_internal_python_symlink_chain_supported_and_cycle_rejected(self):
        python, python3 = tarfile.TarInfo("LightTable/bin/python"), tarfile.TarInfo("LightTable/bin/python3")
        python.type, python.linkname = tarfile.SYMTYPE, "python3"
        python3.type, python3.linkname = tarfile.SYMTYPE, "python3.13"
        self.assertEqual(len(self.validate([python, python3, tarfile.TarInfo("LightTable/bin/python3.13")])), 4)
        python3.linkname = "python"
        with self.assertRaisesRegex(update.UpdateError, "cycle"):
            self.validate([python, python3])

    def test_redirects_cannot_downgrade_transport_or_change_host(self):
        redirect = update.SecureRedirect({"github.com"})
        for url in ("http://github.com/release", "https://evil.invalid/release", "file:///tmp/file"):
            with self.subTest(url=url), self.assertRaises(update.UpdateError):
                redirect.redirect_request(None, None, 302, "", {}, url)


if __name__ == "__main__":
    unittest.main()
