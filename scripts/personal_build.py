#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build one clean origin/main snapshot, then install and verify that artifact.

This command runs a native package journey; installation reopens the personal
app. Tests inject command/process boundaries and do not require a desktop.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import ctypes
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
PRODUCT = "LightTable - NPR Installed.app"
LOCK_REASON = "LightTable managed personal build"


class BuildError(RuntimeError):
    pass


def command(argv, *, cwd=None, timeout=60, log=None, env=None):
    """Never include command output in exceptions (CLI data may contain tokens)."""
    with subprocess.Popen([str(x) for x in argv], cwd=cwd, start_new_session=True,
                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1") | (env or {}),
                          stdout=log or subprocess.PIPE,
                          stderr=log or subprocess.PIPE, text=True) as process:
        try:
            output, _ = process.communicate(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            # A timed-out shell may have compiler/smoke children. End the whole
            # group before returning, so no single-turn work is left running.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise
        if process.returncode:
            raise BuildError(f"{Path(str(argv[0])).name} failed (exit {process.returncode})")
        return output.strip() if output else ""


def git(root, *args):
    return command(["git", "-C", root, *args])


def common_dir(root):
    return Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def exclusive_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BuildError("Another personal build owns the app or worktree lock") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def require_clean(root, cutoff=None):
    if git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise BuildError(f"Build source is dirty; preserving it: {root}")
    if cutoff and git(root, "rev-parse", "HEAD") != cutoff:
        raise BuildError("Build source moved away from the pinned cutoff")


def validate_owned_worktree(root, worktree, marker, expected):
    try:
        owner = json.loads(marker.read_text())
    except (OSError, ValueError) as error:
        raise BuildError(f"Unowned worktree; preserving {worktree}. Choose a new LIGHTTABLE_MNB_WORKTREE.") from error
    if owner != expected or common_dir(worktree) != common_dir(root):
        raise BuildError("Worktree ownership or repository does not match")
    if Path(git(worktree, "rev-parse", "--show-toplevel")).resolve() != worktree:
        raise BuildError("Build path is not a worktree root")
    if git(worktree, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD":
        raise BuildError("Managed build worktree has a branch; preserving it")
    lock = Path(git(worktree, "rev-parse", "--absolute-git-dir")) / "locked"
    if not lock.is_file() or lock.read_text().strip() != LOCK_REASON:
        raise BuildError("Managed worktree lock is missing or belongs to another task")
    require_clean(worktree)


def prepare_worktree(root, worktree, cutoff, state):
    """Reuse only an owned, clean, detached, locked worktree; never reset/clean."""
    marker = state / (hashlib.sha256(os.fsencode(worktree)).hexdigest() + ".json")
    expected = {"format": 1, "repository": str(common_dir(root)), "worktree": str(worktree)}
    if worktree.exists():
        validate_owned_worktree(root, worktree, marker, expected)
        git(worktree, "checkout", "--detach", cutoff, "--quiet")
    else:
        if marker.exists():
            raise BuildError("Owned worktree is missing; inspect its registration before recreating it")
        git(root, "worktree", "add", "--detach", str(worktree), cutoff, "--quiet")
        git(root, "worktree", "lock", "--reason", LOCK_REASON, str(worktree))
        write_json(marker, expected)
    require_clean(worktree, cutoff)


def retire_worktree(root, worktree):
    """Explicitly retire only the clean worktree owned by this build tool."""
    state = common_dir(root) / "lighttable-personal-builds"
    key = hashlib.sha256(os.fsencode(worktree)).hexdigest()
    marker = state / f"{key}.json"
    expected = {"format": 1, "repository": str(common_dir(root)), "worktree": str(worktree)}
    with exclusive_lock(Path.home() / "Library/Caches/LightTable/build-locks" / f"{key}.lock"):
        validate_owned_worktree(root, worktree, marker, expected)
        built = worktree / ".build/personal" / PRODUCT
        if built.exists() and any(bundle_processes(built).values()):
            raise BuildError("Managed worktree has a running package; preserving it")
        git(root, "worktree", "unlock", str(worktree))
        try:
            # No --force: Git must still agree it is safe to remove.
            git(root, "worktree", "remove", str(worktree))
        except BuildError:
            if worktree.exists():
                git(root, "worktree", "lock", "--reason", LOCK_REASON, str(worktree))
            raise
        marker.unlink()


def bundle_identity(app, cutoff):
    contents = app / "Contents"
    info = plistlib.loads((contents / "Info.plist").read_bytes())
    manifest = dict(line.split("=", 1) for line in
                    (contents / "Resources/LightTable/personal-build.env").read_text().splitlines()
                    if "=" in line)
    if info.get("LightTableSourceRevision") != cutoff or manifest.get("SOURCE_REVISION") != cutoff:
        raise BuildError("Bundle revision does not match the pinned cutoff")
    if info.get("LightTableSourceDirty") is not False or manifest.get("SOURCE_DIRTY") != "false":
        raise BuildError("Bundle was built from dirty or unknown source")
    tree = manifest.get("SOURCE_TREE_HASH")
    if not tree or info.get("LightTableSourceTree") != tree:
        raise BuildError("Bundle and manifest source fingerprints disagree")
    if info.get("CFBundleIdentifier") != "com.reville.filmlab.nprinstalled":
        raise BuildError("Bundle is not the isolated personal app")
    catalog = info.get("LSEnvironment", {}).get("LIGHTTABLE_CATALOG_FILE")
    if not catalog or not Path(catalog).is_absolute():
        raise BuildError("Bundle has no explicit isolated catalog")
    command(["/usr/bin/codesign", "--verify", "--deep", "--strict", app])
    return {"revision": cutoff, "source_dirty": False, "source_tree": tree,
            "version": info.get("CFBundleShortVersionString"), "catalog": catalog,
            "signature": "passed"}


def bundle_processes(app):
    """Use kernel executable paths, never process-name patterns or bundle IDs."""
    native = (app / "Contents/MacOS/LightTable").resolve()
    python = (app / "Contents/Resources/Python").resolve()
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidpath.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32)
    library.proc_pidpath.restype = ctypes.c_int
    found = {"native": [], "server": []}
    for value in command(["/bin/ps", "-axo", "pid="]).split():
        pid = int(value)
        buffer = ctypes.create_string_buffer(4096)
        if library.proc_pidpath(pid, buffer, len(buffer)) <= 0:
            continue
        executable = Path(os.fsdecode(buffer.value)).resolve()
        if executable == native:
            found["native"].append(pid)
        elif executable.is_relative_to(python):
            found["server"].append(pid)
    return found


def quit_bundle(app, timeout=20):
    found = bundle_processes(app)
    for pid in found["native"]:
        command(["/usr/bin/osascript", "-l", "JavaScript", "-e",
                 'ObjC.import("AppKit"); '
                 f'$.NSRunningApplication.runningApplicationWithProcessIdentifier({pid}).terminate;'])
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = bundle_processes(app)
        if not found["native"] and not found["server"]:
            return
        if not found["native"]:
            for pid in found["server"]:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        time.sleep(0.25)
    raise BuildError("Personal app did not quit cleanly; existing installation preserved")


def verify_health(payload, identity, processes, http_status):
    if http_status != 200 or payload.get("ok") is not True:
        raise BuildError("Personal app API is not healthy")
    if not processes["native"] or payload.get("pid") not in processes["server"]:
        raise BuildError("Health response does not belong to both installed app/server processes")
    if Path(payload.get("catalog") or "").resolve() != Path(identity["catalog"]).resolve():
        raise BuildError("Health response belongs to a different catalog")
    if payload.get("version") != identity["version"]:
        raise BuildError("Running server version does not match the installed bundle")
    # Packaged macOS source returns null here. The signed plist/manifest proves
    # its revision; kernel process paths bind this response to that installed app.
    if payload.get("sourceRevision") not in (None, identity["revision"]):
        raise BuildError("Running server reports a different source revision")
    if payload.get("windowConnected") is not True or payload.get("headless") or payload.get("restarting"):
        raise BuildError("Installed native window has not connected to its server")
    return {"http_status": 200, "port": payload["port"], "native_pids": processes["native"],
            "server_pid": payload["pid"], "window_connected": True}


def wait_for_health(app, identity, timeout=30):
    deadline = time.monotonic() + timeout
    last = "Personal app has not registered its server"
    cli = app / "Contents/MacOS/lighttable-cli"
    while time.monotonic() < deadline:
        try:
            status = json.loads(command([cli, "--catalog", identity["catalog"], "status", "--json"], timeout=3))
            port = status.get("port")
            if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
                raise BuildError("CLI did not identify a valid local port")
            with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
                return verify_health(json.load(response), identity, bundle_processes(app), response.status)
        except (BuildError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            last = str(error) if isinstance(error, BuildError) else "Waiting for matching local API"
            time.sleep(0.25)
    raise BuildError(last)


def install_staged(candidate, app, backup):
    if backup.exists():
        raise BuildError("Rollback path already exists; refusing to replace it")
    if bundle_processes(app) != {"native": [], "server": []}:
        raise BuildError("Personal app restarted before installation; existing app preserved")
    os.replace(app, backup)
    try:
        os.replace(candidate, app)
    except OSError:
        os.replace(backup, app)
        raise


@contextmanager
def phase(receipt, name):
    print(f"mnb: {name}", flush=True)
    started = time.monotonic()
    item = {"name": name, "status": "failed"}
    receipt["phases"].append(item)
    try:
        yield
        item["status"] = "passed"
    finally:
        item["seconds"] = round(time.monotonic() - started, 3)


def run_build(root, app, worktree, receipt, *, build_only=False):
    state = common_dir(root) / "lighttable-personal-builds"
    lock_root = Path.home() / "Library/Caches/LightTable/build-locks"
    with ExitStack() as stack:
        for target in sorted((str(app), str(worktree))):
            key = hashlib.sha256(target.encode()).hexdigest()
            stack.enter_context(exclusive_lock(lock_root / f"{key}.lock"))
        with phase(receipt, "pin-source"):
            git(root, "fetch", "origin", "--quiet")
            cutoff = git(root, "rev-parse", "origin/main^{commit}")
            receipt["cutoff"] = cutoff
        with phase(receipt, "prepare-worktree"):
            previous_package = worktree / ".build/personal" / PRODUCT
            if previous_package.exists() and any(bundle_processes(previous_package).values()):
                raise BuildError("Managed worktree has a running package; preserving its source and bundle")
            prepare_worktree(root, worktree, cutoff, state)
        with phase(receipt, "build-and-verify-package"):
            updater = worktree / "scripts/update-personal-app.sh"
            if not updater.is_file():
                raise BuildError("Pinned source has no personal updater; no scripts will be overlaid")
            log_path = Path(receipt["build_log"])
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("w") as log:
                command(["/bin/bash", updater, "--build-only"], cwd=worktree, timeout=1800, log=log,
                        env={"LIGHTTABLE_PERSONAL_APP": str(app)})
            require_clean(worktree, cutoff)
            built = worktree / ".build/personal" / PRODUCT
            identity = bundle_identity(built, cutoff)
            receipt["package"] = identity | {"path": str(built)}
            receipt["checks"] = {"clean_snapshot": "passed", "signature": "passed",
                                 "package_smoke": "passed", "installed_runtime": "not_run",
                                 "visual_review": "not_run"}
        if build_only:
            receipt["status"] = "build_verified"
            return
        # Stage on the destination volume and verify before closing the old app.
        with tempfile.TemporaryDirectory(prefix=".lighttable-stage-", dir=app.parent) as staging:
            candidate = Path(staging) / PRODUCT
            with phase(receipt, "stage-installation"):
                try:
                    command(["/bin/cp", "-cRp", built, candidate])
                except BuildError:
                    command(["/usr/bin/ditto", built, candidate])
                bundle_identity(candidate, cutoff)
            with phase(receipt, "quit-personal-app"):
                quit_bundle(app)
            with phase(receipt, "install"):
                backup = app.with_name(f".{app.name}.pre-{time.time_ns()}")
                install_staged(candidate, app, backup)
                receipt["rollback_app"] = str(backup)
            with phase(receipt, "verify-installed-runtime"):
                installed = bundle_identity(app, cutoff)
                command(["/usr/bin/open", app])
                receipt["runtime"] = wait_for_health(app, installed)
                receipt["checks"]["installed_runtime"] = "passed"
                receipt["installed_app"] = str(app)
        receipt["status"] = "installed_verified"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--retire-worktree", action="store_true", help="remove only this tool's clean, idle managed worktree")
    modes.add_argument("--build-only", action="store_true", help="verify package without installing (native package journey still runs)")
    parser.add_argument("--receipt", type=Path, help="JSON receipt destination")
    args = parser.parse_args(argv)
    root = ROOT.resolve()
    app = Path(os.environ.get("LIGHTTABLE_PERSONAL_APP", f"/Applications/{PRODUCT}")).expanduser().absolute()
    worktree = Path(os.environ.get("LIGHTTABLE_MNB_WORKTREE", root.parent / "mnb-managed-worktree")).expanduser().absolute()
    receipt_path = args.receipt or root / ".build/mnb" / f"{time.time_ns()}.json"
    receipt_path = receipt_path.expanduser().absolute()
    receipt = {"format": 1, "status": "failed", "mode": "build" if args.build_only else "install",
               "worktree": str(worktree), "phases": [], "build_log": str(receipt_path.with_suffix(".log"))}
    started = time.monotonic()
    try:
        if app.is_symlink() or worktree.is_symlink():
            raise BuildError("App and managed worktree must use real paths, not symlinks")
        if args.retire_worktree:
            receipt["mode"] = "retire"
            with phase(receipt, "retire-managed-worktree"):
                retire_worktree(root, worktree.resolve())
            receipt["status"] = "worktree_retired"
        else:
            run_build(root, app.resolve(), worktree.resolve(), receipt, build_only=args.build_only)
        return 0
    except (BuildError, OSError, ValueError, subprocess.SubprocessError) as error:
        receipt["error"] = str(error) if isinstance(error, BuildError) else type(error).__name__
        print(f"mnb: FAILED: {receipt['error']}; see {receipt['build_log']}", file=sys.stderr)
        return 1
    finally:
        receipt["seconds"] = round(time.monotonic() - started, 3)
        write_json(receipt_path, receipt)
        print(f"mnb: {receipt['status']}; receipt: {receipt_path}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
