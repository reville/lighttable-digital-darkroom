#!/usr/bin/env python3
"""Temporary cross-revision production-key test with a CI baseline version override.

Run with the bundled Python under Xvfb and a private D-Bus session. Only temporary
copies receive test versions and an ephemeral signing key. The signed archive is
staged locally: this gate tests production validation/apply, not HTTPS delivery,
the update dialog, hardware graphics, or migration between different revisions.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def isolated_environment(root: Path) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("LIGHTTABLE_", "PYTHON", "XDG_"))}
    environment.update({
        "XDG_DATA_HOME": str(root / "data"), "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"), "XDG_STATE_HOME": str(root / "state"),
        "XDG_RUNTIME_DIR": str(root / "runtime"), "LIGHTTABLE_DIR": str(root / "photos"),
        "LIGHTTABLE_CATALOG_FILE": str(root / "data/lighttable/Catalog/library.sqlite3"),
        "LIGHTTABLE_INSTANCE_DIR": str(root / "instances"),
        "NUMBA_CACHE_DIR": str(root / "compiled"), "MPLCONFIGDIR": str(root / "matplotlib"),
        "SPEKTRAFILM_BACKEND": "cpu", "LIBGL_ALWAYS_SOFTWARE": "1", "GDK_BACKEND": "x11",
    })
    return environment


def stop_group(process: subprocess.Popen) -> None:
    # A failed shell can exit before its server/WebKit children do. Always reap
    # the entire group we created, even when the original Popen has exited.
    for number, duration in ((signal.SIGTERM, 5), (signal.SIGKILL, 3)):
        try:
            os.killpg(process.pid, number)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            continue
        break
    process.wait(timeout=3)


def request(port: int, route: str, body=None, token: str = "") -> dict:
    headers = {"Content-Type": "application/json", "X-LightTable-Token": token}
    query = urllib.request.Request(f"http://127.0.0.1:{port}{route}",
        data=json.dumps(body).encode() if body is not None else None, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(query, timeout=3) as response:
        require(response.status == 200, "Native server did not return HTTP 200")
        return json.load(response)


def verify_processes(bundle: Path, desktop_pid: int, health: dict) -> None:
    server_pid = health.get("pid")
    require(type(server_pid) is int and server_pid > 1, "Health has no server process identity")
    require(Path(f"/proc/{desktop_pid}/exe").resolve() ==
            (bundle / "bin/lighttable-desktop-shell").resolve(), "Unexpected native shell executable")
    require(Path(f"/proc/{server_pid}/exe").resolve() ==
            (bundle / "Python/bin/python3").resolve(), "Server is not using the updated bundled Python")
    command = Path(f"/proc/{server_pid}/cmdline").read_bytes().split(b"\0")
    require(os.fsencode(bundle / "Resources/LightTable/server.py") in command,
            "Server is not running the expected bundle's code")
    require(os.getpgid(server_pid) == desktop_pid, "Server is outside the owned desktop process group")


def rendered_photo(state: dict) -> bool:
    current, render = state.get("current"), state.get("render") or {}
    return bool(state.get("client") and state.get("age", 999) < 10
                and current and current.endswith("updater-smoke.jpg")
                and render.get("name") == current and render.get("state") == "ready")


def native_ready(bundle: Path, process: subprocess.Popen, root: Path, timeout: int) -> dict:
    deadline = time.monotonic() + timeout
    state, selected = {}, False
    while time.monotonic() < deadline:
        require(process.poll() is None, f"Native desktop exited with {process.returncode}")
        for path in (root / "instances").glob("[0-9]*.json"):
            try:
                instance = json.loads(path.read_text())
                if instance.get("folder") != str(root / "photos"):
                    continue
                health = request(int(instance["port"]), "/api/health")
                # Ignore a stale old-instance file, never mistake its server for
                # the newly launched native application.
                if health.get("pid") != instance.get("pid"):
                    continue
                require(health.get("ok") and health.get("folder") == str(root / "photos")
                        and health.get("catalog") == str(root / "data/lighttable/Catalog/library.sqlite3")
                        and not health.get("headless"), "Native app opened unexpected data or ran headless")
                verify_processes(bundle, process.pid, health)
                state = request(instance["port"], "/api/ui/state")
                if health.get("windowConnected") and rendered_photo(state):
                    return {"desktop_pid": process.pid, "server_pid": health["pid"],
                            "http": 200, "current": state["current"], "render": state["render"]}
                if state.get("client") and not selected:
                    photos = request(instance["port"], "/api/images").get("images", [])
                    photo = next((item for item in photos if item["name"].endswith("updater-smoke.jpg")), None)
                    if photo:
                        selected = True
                        request(instance["port"], "/api/ui/command", {
                            "command": "goto", "args": {"name": photo["name"]}, "timeout": 1,
                        }, instance.get("token", ""))
            except (OSError, ValueError, KeyError, urllib.error.URLError):
                pass  # Native startup, instance registration and rendering are asynchronous.
        time.sleep(0.2)
    raise RuntimeError(f"Native update timed out before a connected window rendered the photo: {state}")


def make_archive(source: Path, output: Path, manifest: dict) -> None:
    """Archive unchanged package contents with a test-only version manifest."""
    with tarfile.open(output, "w:gz", compresslevel=1, dereference=False) as archive:
        archive.add(source, arcname="LightTable",
                    filter=lambda member: None if member.name == "LightTable/build-manifest.json" else member)
        content = (json.dumps(manifest) + "\n").encode()
        entry = tarfile.TarInfo("LightTable/build-manifest.json")
        entry.size, entry.mode = len(content), 0o644
        archive.addfile(entry, io.BytesIO(content))


def signed_release(update, key, archive: Path, manifest: dict) -> dict:
    now = int(time.time())
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    signed = {**{field: manifest[field] for field in ("version", "platform", "architecture", "source_revision")},
              "schema": 1, "issued_at": now, "expires_at": now + 86400,
              "url": "https://updates.invalid/release.tar.gz", "size": archive.stat().st_size,
              "sha256": digest, "release_notes_url": "https://updates.invalid/notes"}
    return {"signed": signed, "signature": base64.b64encode(key.sign(update.canonical_json(signed))).decode()}


def expect_rejected(update, updater, arguments: dict, text: str) -> None:
    try:
        updater.apply(**arguments)
    except update.UpdateError as error:
        require(text in str(error), f"Wrong update rejection: {error}")
    else:
        raise RuntimeError(f"Updater accepted a release that should fail with {text}")


def run(bundle: Path, timeout: int, target_archive: Path, target_feed: Path, live_download: bool = False) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from PIL import Image

    original_manifest = json.loads((bundle / "build-manifest.json").read_text())
    require(original_manifest.get("source_dirty") is False and original_manifest.get("platform") == "linux",
            "The gate requires an actual clean Linux release package")
    # Import the code shipped in this package, never a source checkout's updater.
    update = load("packaged_linux_updater", bundle / "Resources/LightTable/desktop_updater.py")
    children = []

    class ObservedUpdater(update.LinuxUpdater):
        def _restart(self, *args, **kwargs):
            process = super()._restart(*args, **kwargs)
            children.append(process)
            return process

    with tempfile.TemporaryDirectory(prefix="lighttable-native-update-") as temporary:
        root = Path(temporary).resolve()
        environment = isolated_environment(root)
        for name in ("runtime", "photos"):
            (root / name).mkdir(mode=0o700)
        prefs = root / "config/lighttable/prefs.json"
        prefs.parent.mkdir(parents=True)
        prefs.write_text(json.dumps({"locale": "en", "localeChosen": True,
            "allowAutomation": True, "viewMode": "detail",
            "firstRunSetup": {"version": 1, "status": "completed", "source": "folder"}}))
        source = root / "photos/updater-smoke.jpg"
        Image.linear_gradient("L").resize((160, 120)).convert("RGB").save(source)
        source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        # Copy rather than hard-link: a temporary test must not mutate the release.
        old = root / "original bundle/LightTable"
        shutil.copytree(bundle, old, symlinks=True)
        # Bootstrap the CI baseline with a stable test version. Its source,
        # executable bytes, catalog schema, and production public key stay intact.
        update.atomic_json(old / "build-manifest.json", {**original_manifest, "version": "0.4.99"})
        envelope = json.loads(target_feed.read_text())
        require(envelope['signed']['source_revision'] != original_manifest['source_revision'],
                'Cross-revision test requires two different source commits')
        previous_environment = dict(os.environ)
        os.environ.clear()
        os.environ.update(environment)
        try:
            installer = load("packaged_smoke_integration", old / "desktop-integration.py")
            commands = root / "commands"
            with contextlib.redirect_stdout(sys.stderr):
                installer.integrate("install", old, commands)
            updater = ObservedUpdater(old)
            require(updater.status().get("supported"), f"Packaged updater is disabled: {updater.status()}")
            updater.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if live_download:
                checked = updater.check()
                require(checked.get('state')=='available', 'Public update feed did not offer the release')
                downloaded = updater.download()
                require(downloaded.get('state')=='ready', 'Public HTTPS archive download did not verify')
                delivered = json.loads(updater.envelope_file.read_text())
                require(delivered['signed']['sha256']==envelope['signed']['sha256'] and
                        delivered['signed']['source_revision']==envelope['signed']['source_revision'],
                        'Public feed delivered different release bytes')
                envelope = delivered
            else:
                shutil.copy2(target_archive, updater.archive_file)
            authorization = root / "cache/lighttable/updates/pending-0123456789abcdef"
            authorization.mkdir(mode=0o700, parents=True)
            (authorization / "authorized").write_text("isolated native smoke\n")

            with (root / "original-desktop.log").open("w") as log:
                original = subprocess.Popen([str(commands / "lighttable-desktop")],
                    stdout=log, stderr=log, start_new_session=True)
            children.append(original)
            before = native_ready(old, original, root, timeout)
            instance = next(json.loads(p.read_text()) for p in (root/'instances').glob('*.json')
                            if json.loads(p.read_text()).get('pid') == before['server_pid'])
            request(instance['port'], '/api/state', {'name': before['current'],
                    'grade': {'exposure': 0.5}, 'rating': 4, 'params': {'profile_enabled':False},
                    'origin':'release-validation','historyLabel':'Release upgrade persistence'}, instance['token'])
            arguments = {"wait_pids": [before["desktop_pid"], before["server_pid"]],
                         "authorization_directory": authorization,
                         "launch_env": {key: environment[key] for key in update.LAUNCH_ENV if key in environment}}

            tampered = {**envelope, "signed": {**envelope["signed"], "version": "9.9.9"}}
            update.atomic_json(updater.envelope_file, tampered)
            expect_rejected(update, updater, arguments, "signature")
            require((commands / "lighttable-desktop").resolve() == old / "bin/lighttable-desktop",
                    "Rejected metadata changed the desktop launcher")
            backup_verified = False
            if live_download:
                from urllib.error import HTTPError
                for attempt in range(50):
                    try:
                        prepared = request(instance['port'], '/api/updates/prepare', {}, instance['token'])
                        break
                    except HTTPError as error:
                        if error.code != 409 or attempt == 49: raise
                        time.sleep(0.2)
                backup = Path(prepared.get('backup') or '')
                require(backup.is_file() and backup.is_relative_to(root), 'Update preparation did not create an isolated catalog backup')
                backup_verified = backup.stat().st_size > 0
                request(instance['port'], '/api/updates/cancel', {}, instance['token'])
                native = load('live_native_acceptance', Path(__file__).with_name('desktop-acceptance.py'))
                native.normal_close(original, before['server_pid'], time.monotonic()+35)
            stop_group(original)
            children.remove(original)
            require(all(update.process_identity(pid) is None for pid in arguments["wait_pids"]),
                    "The original desktop/server did not exit before applying the update")
            update.atomic_json(updater.envelope_file, envelope)
            with updater.archive_file.open("r+b") as archive:
                first = archive.read(1)
                archive.seek(0)
                archive.write(bytes([first[0] ^ 1]))
            try:
                expect_rejected(update, updater, arguments, "checksum")
            finally:
                with updater.archive_file.open("r+b") as archive:
                    archive.write(first)
            require((commands / "lighttable-desktop").resolve() == old / "bin/lighttable-desktop",
                    "Rejected archive changed the desktop launcher")

            result = updater.apply(**arguments)
            require(result.get("state") == "installed", f"Update was not installed: {result}")
            installed = Path(result["installed_bundle"])
            require(installed.is_relative_to(root / "data/lighttable/versions") and installed != old,
                    "Updater did not install a separate version in the isolated data directory")
            require((commands / "lighttable-desktop").resolve() == installed / "bin/lighttable-desktop"
                    and (commands / "lighttable").resolve() == installed / "bin/lighttable",
                    "Owned launchers did not move to the new bundle")
            after = native_ready(installed, children[-1], root, timeout)
            instance = next(json.loads(p.read_text()) for p in (root/'instances').glob('*.json')
                            if json.loads(p.read_text()).get('pid') == after['server_pid'])
            from urllib.parse import urlencode
            saved = request(instance['port'], '/api/state?'+urlencode({'name':after['current']}))
            require(saved.get('rating')==4 and saved.get('grade',{}).get('exposure')==0.5,
                    'Cross-revision update lost saved edits')
            installed_manifest = json.loads((installed/'build-manifest.json').read_text())
            require(installed_manifest['source_revision']==envelope['signed']['source_revision'],
                    'Wrong target source was installed')
            require(after["server_pid"] != before["server_pid"], "The upgraded server did not restart")
            if live_download:
                screenshot = Path.cwd()/'evidence/public-upgraded-desktop.png'
                screenshot.parent.mkdir(parents=True,exist_ok=True)
                subprocess.run(['import','-window','root',str(screenshot)],check=True,timeout=15)
            stop_group(children[-1])
            children.pop()

            # Repeat the same update from the original install while making GTK
            # fail to open a display. The real packaged launcher/shell still run;
            # no stub executable or fake readiness server supplies this failure.
            with contextlib.redirect_stdout(sys.stderr):
                installer.integrate("install", old, commands)
            saved_integration = updater.integration_file.read_bytes()
            os.environ["DISPLAY"] = str(root / "missing-x11-display")
            count = len(children)
            expect_rejected(update, updater, arguments, "exited before it was ready")
            require(len(children) == count + 1 and children[-1].poll() is not None,
                    "The deliberate GTK startup failure did not execute and exit")
            stop_group(children[-1])
            children.pop()
            status = updater.status()
            require(status.get("launchers_restored") is True
                    and updater.integration_file.read_bytes() == saved_integration,
                    "Failed startup did not restore the original owned integrations")
            require((commands / "lighttable-desktop").resolve() == old / "bin/lighttable-desktop"
                    and (commands / "lighttable").resolve() == old / "bin/lighttable",
                    "Failed startup left a launcher pointing to the failed version")
            require(hashlib.sha256(source.read_bytes()).hexdigest() == source_digest,
                    "The native update changed the original photo")
            return {"ok": True, "source_revision": original_manifest["source_revision"],
                    "source_package_version": original_manifest["version"], "test_versions": ["0.4.99", envelope["signed"]["version"]],
                    "target_source": envelope["signed"]["source_revision"], "production_signature": True,
                    "saved_edit_persistence": True, "baseline_version_override": True,
                    "desktop": "GTK/WebKitGTK", "display_backend": "x11", "archive_delivery": "public HTTPS" if live_download else "locally staged",
                    "catalog_backup_verified": backup_verified, "normal_original_close": live_download,
                    "signature_rejection": True, "checksum_rejection": True,
                    "owned_launchers_retargeted": True, "native_before": before, "native_after": after,
                    "failed_native_startup_rollback": True, "original_photo_preserved": True}
        except BaseException:
            for log in (root / "original-desktop.log", root / "state/lighttable/logs/server.log"):
                if log.exists():
                    print(f"{log.name}:\n{log.read_text(errors='replace')[-5000:]}", file=sys.stderr)
            raise
        finally:
            try:
                for process in reversed(children):
                    stop_group(process)
            finally:
                os.environ.clear()
                os.environ.update(previous_environment)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--target-archive", type=Path, required=True)
    parser.add_argument("--target-feed", type=Path, required=True)
    parser.add_argument("--live-download", action="store_true")
    parser.add_argument("--timeout", type=int, default=90, help="Seconds allowed for each native UI to render (10–105)")
    args = parser.parse_args()
    if sys.platform != "linux" or not os.environ.get("DISPLAY") or not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        parser.error("Run on Linux under Xvfb and a private dbus-run-session")
    if not 10 <= args.timeout <= 105:
        parser.error("--timeout must be between 10 and 105 seconds")
    bundle = args.bundle.resolve()
    if Path(sys.prefix).resolve() != bundle / "Python":
        parser.error("Run this gate with the package's own Python runtime")
    signal.signal(signal.SIGTERM, lambda number, _frame: sys.exit(128 + number))
    print(json.dumps(run(bundle, args.timeout, args.target_archive.resolve(), args.target_feed.resolve(), args.live_download)))


if __name__ == "__main__":
    main()
