#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Exercise signed Linux upgrades with the actual packaged GTK/WebKit runtime.

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


def run(bundle: Path, timeout: int) -> dict:
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
        key = Ed25519PrivateKey.generate()  # Never serialized to disk or printed.
        public = base64.b64encode(key.public_key().public_bytes_raw()).decode()
        update.atomic_json(old / "build-manifest.json", {**original_manifest, "version": "0.0.0"})
        update.atomic_json(old / "update-config.json", {
            "public_key": public, "feed_url": "https://updates.invalid/linux.json"})
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
            make_archive(old, updater.archive_file, {**original_manifest, "version": "0.0.1"})
            envelope = signed_release(update, key, updater.archive_file,
                                      {**original_manifest, "version": "0.0.1"})
            authorization = root / "cache/lighttable/updates/pending-0123456789abcdef"
            authorization.mkdir(mode=0o700, parents=True)
            (authorization / "authorized").write_text("isolated native smoke\n")

            with (root / "original-desktop.log").open("w") as log:
                original = subprocess.Popen([str(commands / "lighttable-desktop")],
                    stdout=log, stderr=log, start_new_session=True)
            children.append(original)
            before = native_ready(old, original, root, timeout)
            arguments = {"wait_pids": [before["desktop_pid"], before["server_pid"]],
                         "authorization_directory": authorization,
                         "launch_env": {key: environment[key] for key in update.LAUNCH_ENV if key in environment}}

            tampered = {**envelope, "signed": {**envelope["signed"], "version": "9.9.9"}}
            update.atomic_json(updater.envelope_file, tampered)
            expect_rejected(update, updater, arguments, "signature")
            require((commands / "lighttable-desktop").resolve() == old / "bin/lighttable-desktop",
                    "Rejected metadata changed the desktop launcher")
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
            require(after["server_pid"] != before["server_pid"], "The upgraded server did not restart")
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
                    "source_package_version": original_manifest["version"], "test_versions": ["0.0.0", "0.0.1"],
                    "desktop": "GTK/WebKitGTK", "display_backend": "x11", "archive_delivery": "locally staged",
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
    print(json.dumps(run(bundle, args.timeout)))


if __name__ == "__main__":
    main()
