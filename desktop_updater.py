# SPDX-License-Identifier: GPL-3.0-only
"""Signed, per-user Linux portable updates. Package managers own other installs.

The catalog stays exclusively owned by the server. The caller must finish work,
back up its catalog and stop both processes before this worker changes launchers.
Only a bundled public key authenticates release metadata; no network key is used.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_MANIFEST = 65536
MAX_ARCHIVE = 4 * 1024**3
MAX_EXTRACTED = 12 * 1024**3
MAX_MEMBERS = 250000
NETWORK_TIMEOUT = 15
DOWNLOAD_TIMEOUT = 1800
EXIT_TIMEOUT = 120
READY_TIMEOUT = 120
DEFAULT_FEED = "https://github.com/reville/lighttable-digital-darkroom/releases/download/desktop-updates/linux-{architecture}.json"
DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
LAUNCH_ENV = {"LIGHTTABLE_DIR", "LIGHTTABLE_CATALOG_FILE", "XDG_DATA_HOME", "XDG_CONFIG_HOME",
              "XDG_CACHE_HOME", "XDG_STATE_HOME"}
PERSISTENT_ENV = {"LIGHTTABLE_CACHE_DIR", "LIGHTTABLE_PREFS_FILE", "LIGHTTABLE_PRESETS_FILE",
                  "LIGHTTABLE_AI_DIR", "LIGHTTABLE_MODEL_DIR", "LIGHTTABLE_INSTANCE_DIR",
                  "LIGHTTABLE_LOG_FILE", "LIGHTTABLE_SERVER_LOG", "LIGHTTABLE_PROFILE"}


class UpdateError(ValueError):
    pass


def canonical_json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def read_json(path: Path, limit: int = MAX_MANIFEST) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise UpdateError(f"Invalid update file: {path.name}")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise UpdateError("Update data must be a JSON object")
    return value


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(canonical_json(value) + b"\n")
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value):
        raise UpdateError("Automatic updates require a stable major.minor.patch version")
    if len(value) > 32:
        raise UpdateError("Invalid release version")
    return tuple(map(int, value.split(".")))


def https_url(value: str, hosts: set[str] | None = None) -> str:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise UpdateError("Invalid update URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or parsed.port not in (None, 443)):
        raise UpdateError("Updates require an HTTPS URL without credentials")
    if hosts is not None and parsed.hostname.lower() not in hosts:
        raise UpdateError("Update download host is not trusted")
    return value


class SecureRedirect(HTTPRedirectHandler):
    max_redirections = 5

    def __init__(self, hosts: set[str]):
        self.hosts = hosts

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        https_url(newurl, self.hosts)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def fetch(url: str, output: Path | None = None, *, limit: int, hosts: set[str]) -> bytes | None:
    """Bound HTTPS including redirects, declared length, actual bytes and time."""
    https_url(url, hosts)
    opener = build_opener(SecureRedirect(hosts))
    request = Request(url, headers={"User-Agent": "LightTable-Updater/1", "Cache-Control": "no-cache"})
    deadline = time.monotonic() + (DOWNLOAD_TIMEOUT if output else 60)
    with opener.open(request, timeout=NETWORK_TIMEOUT) as response:
        https_url(response.url, hosts)
        length = response.headers.get("Content-Length")
        if length and (not length.isdigit() or int(length) > limit):
            raise UpdateError("Update response exceeds the allowed size")
        stream = output.open("xb") if output else None
        data, total = bytearray(), 0
        try:
            while True:
                if time.monotonic() > deadline:
                    raise UpdateError("Update download timed out")
                # read1 returns after a single socket read, so a peer sending
                # tiny chunks cannot keep a large read() alive past the deadline.
                chunk = response.read1(min(1024 * 1024, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise UpdateError("Update response exceeds the allowed size")
                if stream:
                    stream.write(chunk)
                else:
                    data.extend(chunk)
            if length and total != int(length):
                raise UpdateError("Update download was incomplete")
            if stream:
                stream.flush()
                os.fsync(stream.fileno())
            return bytes(data) if output is None else None
        finally:
            if stream:
                stream.close()


def verify_manifest(envelope: dict, public_key: str, *, architecture: str, current_version: str,
                    hosts: set[str]) -> dict:
    # Import only on Linux updater use: macOS/Windows do not require this wheel.
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        signed = envelope["signed"]
        if not isinstance(signed, dict):
            raise UpdateError("Invalid signed update metadata")
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(base64.b64decode(envelope["signature"], validate=True), canonical_json(signed))
    except (InvalidSignature, KeyError, TypeError, ValueError) as error:
        raise UpdateError("The update signature could not be verified") from error
    if signed.get("schema") != 1 or type(signed.get("schema")) is not int:
        raise UpdateError("Unsupported update manifest schema")
    now = time.time()
    if (type(signed.get("issued_at")) is not int or type(signed.get("expires_at")) is not int
            or not 0 < signed["issued_at"] <= now + 86400
            or not signed["issued_at"] < signed["expires_at"] <= signed["issued_at"] + 366 * 86400
            or signed["expires_at"] <= now):
        raise UpdateError("The signed update metadata is expired or has an invalid timestamp")
    if signed.get("platform") != "linux" or signed.get("architecture") != architecture:
        raise UpdateError("The update targets a different platform or architecture")
    version_tuple(signed.get("version"))
    if version_tuple(signed["version"]) < version_tuple(current_version):
        raise UpdateError("Refusing an older release")
    if type(signed.get("size")) is not int or not 0 < signed["size"] <= MAX_ARCHIVE:
        raise UpdateError("Invalid update archive size")
    if not isinstance(signed.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", signed["sha256"]):
        raise UpdateError("Invalid archive checksum")
    if not isinstance(signed.get("source_revision"), str) or not re.fullmatch(r"[0-9a-f]{40}", signed["source_revision"]):
        raise UpdateError("Invalid release source revision")
    https_url(signed.get("url"), hosts)
    https_url(signed.get("release_notes_url"))
    return signed


def verify_archive(archive: Path, signed: dict) -> None:
    if archive.is_symlink() or not archive.is_file() or archive.stat().st_size != signed["size"]:
        raise UpdateError("Update archive size does not match its signed manifest")
    with archive.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != signed["sha256"]:
        raise UpdateError("Update archive checksum does not match its signed manifest")


def archive_members(stream: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Validate the entire member/link graph before creating a single file."""
    members, names, total = [], {}, 0
    for item in stream:
        if len(members) >= MAX_MEMBERS:
            raise UpdateError("Update archive contains too many files")
        path = PurePosixPath(item.name)
        if (not item.name or path.is_absolute() or ".." in path.parts or "\\" in item.name
                or any(ord(c) < 32 for c in item.name) or not path.parts
                or path.parts[0] != "LightTable" or str(path) != item.name.rstrip("/")):
            raise UpdateError("Unsafe update archive path")
        if str(path) in names:
            raise UpdateError("Duplicate update archive path")
        if not (item.isdir() or item.isfile() or item.issym()):
            raise UpdateError("Unsupported archive entry (hard links and special files are forbidden)")
        if item.size < 0 or item.size > 2 * 1024**3:
            raise UpdateError("Archive member exceeds the allowed size")
        total += item.size
        if total > MAX_EXTRACTED:
            raise UpdateError("Expanded update exceeds the allowed size")
        members.append(item)
        names[str(path)] = item
    if not members or not names.get("LightTable", tarfile.TarInfo()).isdir():
        raise UpdateError("Update archive has no LightTable root directory")
    for item in members:
        path = PurePosixPath(item.name)
        for parent in path.parents:
            if str(parent) in names and not names[str(parent)].isdir():
                raise UpdateError("Archive writes through a non-directory or symbolic link")
        if item.issym():
            target = PurePosixPath(item.linkname)
            if target.is_absolute() or not item.linkname or "\\" in item.linkname:
                raise UpdateError("Unsafe archive symbolic link")
            parts = list(path.parent.parts)
            for component in target.parts:
                if component == "..":
                    if len(parts) <= 1:
                        raise UpdateError("Archive symbolic link escapes its bundle")
                    parts.pop()
                elif component != ".":
                    parts.append(component)
            # Resolve chains exactly as the filesystem would, including '..'
            # after a symlink component. Bounds catch cycles and bundle escapes.
            pending, resolved, hops = list(path.parts), [], 0
            while pending:
                component = pending.pop(0)
                if component == "..":
                    if len(resolved) <= 1:
                        raise UpdateError("Archive symbolic link escapes its bundle")
                    resolved.pop()
                    continue
                if component == ".":
                    continue
                resolved.append(component)
                current = names.get("/".join(resolved))
                if current and current.issym():
                    hops += 1
                    if hops > 40:
                        raise UpdateError("Archive symbolic link cycle")
                    target = PurePosixPath(current.linkname)
                    if target.is_absolute():
                        raise UpdateError("Unsafe archive symbolic link")
                    resolved.pop()
                    pending = list(target.parts) + pending
    return members


def extract_archive(archive: Path, destination: Path, signed: dict) -> Path:
    verify_archive(archive, signed)
    with tarfile.open(archive, "r:gz") as stream:
        members = archive_members(stream)
        if shutil.disk_usage(destination.parent).free < sum(m.size for m in members) + 64 * 1024**2:
            raise UpdateError("There is not enough free space to install this update")
        destination.mkdir(mode=0o700)
        try:
            # data_filter supplies a second containment check and strips owner,
            # group and privileged modes; no scripts have been executed yet.
            stream.extractall(destination, members=members, filter="data")
            bundle = destination / "LightTable"
            metadata = read_json(bundle / "build-manifest.json")
            for field in ("version", "platform", "architecture", "source_revision"):
                if metadata.get(field) != signed[field]:
                    raise UpdateError(f"Bundled {field} does not match its signed manifest")
            if metadata.get("source_dirty") is not False:
                raise UpdateError("Updates must contain a clean release build")
            for name in ("bin/lighttable-desktop", "bin/lighttable-desktop-shell", "Python/bin/python3"):
                path = bundle / name
                if not path.is_file() or not os.access(path, os.X_OK) or not path.resolve().is_relative_to(bundle):
                    raise UpdateError(f"Update bundle is incomplete: {name}")
            for name in ("desktop-integration.py", "Resources/LightTable/server.py", "update-config.json"):
                if not (bundle / name).is_file():
                    raise UpdateError(f"Update bundle is incomplete: {name}")
            if read_json(bundle / "installation-owner.json").get("owner") != "portable":
                raise UpdateError("The update archive is not a portable installation")
            return bundle
        except BaseException:
            shutil.rmtree(destination)
            raise


def xdg(name: str, fallback: Path) -> Path:
    path = Path(os.environ.get(name, ""))
    return path if path.is_absolute() else fallback


def process_identity(pid: int) -> str | None:
    if type(pid) is not int or pid <= 1:
        raise UpdateError("Invalid process to wait for")
    try:
        # Linux starttime disambiguates PID reuse; zombies have already exited.
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if stat[0] == "Z" else stat[19]
    except FileNotFoundError:
        if sys.platform == "linux":
            return None
        try:
            os.kill(pid, 0)
            return "live"
        except ProcessLookupError:
            return None
    except PermissionError as error:
        raise UpdateError("Cannot verify the running application process") from error


class LinuxUpdater:
    def __init__(self, bundle: Path, state_dir: Path | None = None):
        self.bundle = Path(bundle).resolve()
        self.state_dir = Path(state_dir) if state_dir else xdg("XDG_STATE_HOME", Path.home() / ".local/state") / "lighttable/updates"
        self.state_file = self.state_dir / "status.json"
        self.envelope_file = self.state_dir / "release.json"
        self.archive_file = self.state_dir / "release.tar.gz"
        self.integration_file = xdg("XDG_STATE_HOME", Path.home() / ".local/state") / "lighttable/desktop-integration.json"

    def _config(self) -> dict:
        config = read_json(self.bundle / "update-config.json")
        key = config.get("public_key", "")
        if not isinstance(key, str) or len(base64.b64decode(key, validate=True)) != 32:
            raise UpdateError("This build has no update verification key")
        feed = https_url(config.get("feed_url"))
        return {**config, "hosts": DOWNLOAD_HOSTS | {urlsplit(feed).hostname}}

    def _metadata(self) -> dict:
        value = read_json(self.bundle / "build-manifest.json")
        if value.get("platform") != "linux" or value.get("source_dirty") is not False:
            raise UpdateError("Automatic updates are disabled for development builds")
        if value.get("architecture") != platform.machine():
            raise UpdateError("This bundle does not match this computer's architecture")
        version_tuple(value.get("version"))
        return value

    def _owner(self) -> str:
        if os.environ.get("SNAP"):
            return "snap"
        if os.environ.get("FLATPAK_ID") or Path("/.flatpak-info").exists():
            return "flatpak"
        marker = self.bundle / "installation-owner.json"
        if marker.exists():
            owner = read_json(marker).get("owner")
            if owner not in {"portable", "arch", "snap", "flatpak"}:
                raise UpdateError("Unknown installation owner")
            return owner
        if self.bundle == Path("/opt/lighttable") or self.bundle.is_relative_to("/usr"):
            return "package-manager"
        return "development"

    def _integration(self) -> dict:
        record = read_json(self.integration_file)
        if record.get("bundle") != str(self.bundle):
            raise UpdateError("Run this bundle's install.sh before using automatic updates")
        files = record.get("files", {})
        command = next((Path(p) for p, v in files.items()
                        if p.endswith("/lighttable-desktop") and v == {"link": str(self.bundle / "bin/lighttable-desktop")}), None)
        desktop = next((Path(p) for p in files if p.endswith("/applications/app.lighttable.LightTable.desktop")), None)
        if command is None or desktop is None:
            raise UpdateError("The portable installation record is incomplete")
        expected = {str(command), str(command.parent / "lighttable"), str(desktop),
                    str(desktop.parent.parent / "icons/hicolor/1024x1024/apps/app.lighttable.LightTable.png")}
        if set(files) != expected:
            raise UpdateError("The portable installation record contains unexpected paths")
        for filename, recorded in files.items():
            path = Path(filename)
            if path.is_symlink():
                actual = {"link": os.readlink(path)}
            elif path.is_file() and path.stat().st_size <= MAX_MANIFEST:
                actual = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            else:
                actual = None
            if actual != recorded:
                raise UpdateError("An installed launcher was changed; rerun install.sh before updating")
        return {"bin_dir": command.parent, "data_dir": desktop.parent.parent,
                "state_dir": self.integration_file.parent.parent}

    def status(self) -> dict:
        result = {"supported": False, "owner": "development", "current_version": "", "state": "disabled"}
        try:
            if sys.platform != "linux":
                raise UpdateError("The portable updater is only available on Linux")
            result["owner"] = self._owner()
            if result["owner"] != "portable":
                raise UpdateError("Updates are managed by " + result["owner"])
            metadata = self._metadata()
            result["current_version"] = metadata["version"]
            self._config()
            self._integration()
            saved = read_json(self.state_file) if self.state_file.exists() else {}
            if saved.get("bundle") != str(self.bundle):
                saved = {}
            result.update(saved)
            result.update(supported=True, owner="portable", current_version=metadata["version"], state=saved.get("state", "idle"))
        except (OSError, ValueError, KeyError, TypeError) as error:
            result["error"] = str(error)
        return result

    def _require_supported(self) -> dict:
        status = self.status()
        if not status["supported"]:
            raise UpdateError(status.get("error", "Automatic updates are unavailable"))
        return status

    def _save(self, state: str, **values) -> dict:
        previous = read_json(self.state_file) if self.state_file.exists() else {}
        if previous.get("bundle") != str(self.bundle):
            previous = {}
        previous.pop("error", None)
        saved = {**previous, "bundle": str(self.bundle), "state": state, **values}
        atomic_json(self.state_file, saved)
        result = self.status()
        result.pop("error", None)
        return {**result, **saved}

    @contextlib.contextmanager
    def _lock(self):
        import fcntl
        if self.state_dir.is_symlink():
            raise UpdateError("Update state directory must not be a symbolic link")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.state_dir.stat().st_uid != os.getuid() or self.state_dir.stat().st_mode & 0o022:
            raise UpdateError("Update state must be private to the current user")
        descriptor = os.open(self.state_dir / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise UpdateError("Another update operation is running") from error
            yield
        finally:
            os.close(descriptor)

    def _verify(self, envelope: dict) -> dict:
        config, metadata = self._config(), self._metadata()
        return verify_manifest(envelope, config["public_key"], architecture=metadata["architecture"],
                               current_version=metadata["version"], hosts=config["hosts"])

    def check(self) -> dict:
        self._require_supported()
        with self._lock():
            self._save("checking")
            try:
                config = self._config()
                envelope = json.loads(fetch(config["feed_url"], limit=MAX_MANIFEST, hosts=config["hosts"]))
                signed = self._verify(envelope)
                previous = read_json(self.state_file)
                newest = previous.get("newest_version", self._metadata()["version"])
                if version_tuple(signed["version"]) < version_tuple(newest):
                    raise UpdateError("The update feed returned an older release than previously seen")
                if signed["issued_at"] < previous.get("newest_timestamp", 0):
                    raise UpdateError("The update feed returned older signed metadata")
                atomic_json(self.envelope_file, envelope)
                available = version_tuple(signed["version"]) > version_tuple(self._metadata()["version"])
                ready = False
                if available and self.archive_file.exists():
                    try:
                        verify_archive(self.archive_file, signed)
                        ready = True
                    except UpdateError:
                        pass
                return self._save("ready" if ready else "available" if available else "idle",
                                  available_version=signed["version"] if available else None,
                                  newest_version=signed["version"], newest_timestamp=signed["issued_at"],
                                  release_notes_url=signed["release_notes_url"],
                                  last_checked=time.time())
            except Exception as error:
                self._save("error", error=str(error), last_checked=time.time())
                raise

    def download(self) -> dict:
        self._require_supported()
        with self._lock():
            self._save("downloading")
            temporary = self.state_dir / "release.partial"
            try:
                signed = self._verify(read_json(self.envelope_file))
                if version_tuple(signed["version"]) <= version_tuple(self._metadata()["version"]):
                    raise UpdateError("There is no newer release to download")
                temporary.unlink(missing_ok=True)
                fetch(signed["url"], temporary, limit=signed["size"], hosts=self._config()["hosts"])
                verify_archive(temporary, signed)
                with tarfile.open(temporary, "r:gz") as archive:
                    archive_members(archive)
                os.replace(temporary, self.archive_file)
                return self._save("ready", available_version=signed["version"], release_notes_url=signed["release_notes_url"])
            except Exception as error:
                self._save("error", error=str(error))
                raise
            finally:
                temporary.unlink(missing_ok=True)

    def cancel_apply(self) -> dict:
        """Restore retry state after the caller reaps the detached worker.

        The original server must still be alive. This never changes installed
        files or contacts the network, and failures never prevent editing from
        reopening. A missing/bad archive remains available to download again.
        """
        try:
            self._require_supported()
            with self._lock():
                try:
                    signed = self._verify(read_json(self.envelope_file))
                    if version_tuple(signed["version"]) <= version_tuple(self._metadata()["version"]):
                        return self._save("idle", available_version=None)
                    try:
                        verify_archive(self.archive_file, signed)
                    except (OSError, ValueError) as error:
                        return self._save("available", available_version=signed["version"],
                                          release_notes_url=signed["release_notes_url"], error=str(error))
                    return self._save("ready", available_version=signed["version"],
                                      release_notes_url=signed["release_notes_url"])
                except Exception as error:
                    return self._save("error", error=str(error))
        except Exception as error:
            result = self.status()
            result.update(state="error", error=str(error))
            return result

    def _integrate(self, bundle: Path, settings: dict) -> None:
        # Use the currently trusted install helper, not a helper before its
        # archive signature has been authenticated. No shell string evaluation.
        specification = importlib.util.spec_from_file_location("lighttable_update_integration", self.bundle / "desktop-integration.py")
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        original = {key: os.environ.get(key) for key in ("XDG_DATA_HOME", "XDG_STATE_HOME")}
        try:
            os.environ.update(XDG_DATA_HOME=str(settings["data_dir"]), XDG_STATE_HOME=str(settings["state_dir"]))
            with contextlib.redirect_stdout(sys.stderr):
                module.integrate("install", bundle, settings["bin_dir"])
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _restart(self, bundle: Path, launch_env: dict, launch_args: list[str], ready: Path) -> subprocess.Popen:
        environment = {key: value for key, value in os.environ.items()
                       if ((not key.startswith("LIGHTTABLE_") and not key.startswith("PYTHON"))
                           or key in PERSISTENT_ENV)}
        environment.update(launch_env)
        environment["LIGHTTABLE_UPDATE_READY_FILE"] = str(ready)
        return subprocess.Popen([str(bundle / "bin/lighttable-desktop"), *launch_args], env=environment,
                                cwd=bundle, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)

    def _wait_ready(self, child: subprocess.Popen, ready: Path) -> None:
        deadline = time.monotonic() + READY_TIMEOUT
        # Proxies have no place in a local server health check.
        opener = build_opener(ProxyHandler({}))
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise UpdateError("The updated application exited before it was ready")
            if ready.exists():
                try:
                    record = read_json(ready)
                    port, pid = record["port"], record["pid"]
                    if type(port) is not int or not 1 <= port <= 65535 or process_identity(pid) is None:
                        raise UpdateError("Invalid application readiness record")
                    with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
                        if response.status == 200:
                            return
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            time.sleep(0.1)
        raise UpdateError("The updated application did not become ready in time")

    def _authorization_directory(self, directory: Path | None) -> Path:
        if directory is None:
            raise UpdateError("Native shutdown authorization is required to install an update")
        directory = Path(directory)
        cache_override = Path(os.environ.get("LIGHTTABLE_CACHE_DIR", ""))
        cache = cache_override if cache_override.is_absolute() else xdg("XDG_CACHE_HOME", Path.home() / ".cache") / "lighttable"
        if (not directory.is_absolute() or directory.is_symlink() or not directory.is_dir()
                or directory.parent.resolve() != (cache / "updates").resolve()
                or not re.fullmatch(r"pending-[0-9a-f-]{16,64}", directory.name)
                or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077):
            raise UpdateError("Invalid private update authorization directory")
        return directory

    @staticmethod
    def _authorized(directory: Path) -> bool:
        if os.path.lexists(directory / "cancelled"):
            raise UpdateError("The update was cancelled; no application files were replaced")
        flag = directory / "authorized"
        if os.path.lexists(flag):
            if flag.is_symlink() or not flag.is_file() or flag.stat().st_uid != os.getuid():
                raise UpdateError("Invalid update authorization")
            return True
        return False

    def apply(self, *, wait_pids: list[int], launch_env: dict | None = None,
              launch_args: list[str] | None = None, armed_file: Path | None = None,
              authorization_directory: Path | None = None) -> dict:
        self._require_supported()
        authorization = self._authorization_directory(authorization_directory)
        launch_env, launch_args = launch_env or {}, launch_args or []
        if (not isinstance(launch_env, dict) or not set(launch_env) <= LAUNCH_ENV
                or any(not isinstance(v, str) or "\0" in v for v in launch_env.values())
                or not isinstance(launch_args, list) or launch_args):
            raise UpdateError("Invalid update relaunch arguments; restore the catalog through the launch environment")
        with self._lock():
            child, switched, restored = None, False, False
            try:
                signed = self._verify(read_json(self.envelope_file))
                if version_tuple(signed["version"]) <= version_tuple(self._metadata()["version"]):
                    raise UpdateError("Refusing to reinstall or downgrade this release")
                # The downloader already authenticated the bytes. Arm quickly
                # after cheap checks; hash the entire archive again after the
                # app exits, before extraction, even on a slow multi-GB disk.
                if (self.archive_file.is_symlink() or not self.archive_file.is_file()
                        or self.archive_file.stat().st_size != signed["size"]):
                    raise UpdateError("Update archive size does not match its signed manifest")
                settings = self._integration()
                # Installation uses its recorded XDG/bin paths. The app's
                # runtime XDG paths may differ and must survive the restart.
                identities = {pid: process_identity(pid) for pid in wait_pids}
                if not wait_pids or os.getpid() in wait_pids:
                    raise UpdateError("The updater must wait for the application and server processes")
                self._save("applying", available_version=signed["version"])
                if armed_file:
                    if armed_file.parent.resolve() != self.state_dir.resolve() or armed_file.is_symlink():
                        raise UpdateError("The update acknowledgment must be in the update state directory")
                    atomic_json(armed_file, {"armed": True, "pid": os.getpid()})
                deadline = time.monotonic() + EXIT_TIMEOUT
                while time.monotonic() < deadline:
                    authorized = self._authorized(authorization)
                    live = any(identity is not None and process_identity(pid) == identity for pid, identity in identities.items())
                    if authorized and not live:
                        break
                    time.sleep(0.1)
                else:
                    raise UpdateError("The application is still running or shutdown was not authorized; no files were replaced")
                # Recheck ownership and bytes after the wait; don't trust the
                # stage merely because an earlier process checked it.
                self._integration()
                signed = self._verify(read_json(self.envelope_file))
                if not self._authorized(authorization):
                    raise UpdateError("Native shutdown authorization was withdrawn")
                versions = settings["data_dir"] / "lighttable/versions"
                if versions.is_symlink():
                    raise UpdateError("Version directory must not be a symbolic link")
                versions.mkdir(mode=0o700, parents=True, exist_ok=True)
                if versions.stat().st_uid != os.getuid() or versions.stat().st_mode & 0o022:
                    raise UpdateError("Installed versions must be private to the current user")
                # A failed restart remains available for diagnosis. A retry
                # gets a fresh directory and re-verifies/extracts every byte.
                destination = versions / f"{signed['version']}-{signed['sha256'][:16]}-{os.urandom(4).hex()}"
                bundle = extract_archive(self.archive_file, destination, signed)
                # The next build must retain the trusted key, not silently
                # disable authentication or introduce an unapproved new key.
                if read_json(bundle / "update-config.json").get("public_key") != self._config()["public_key"]:
                    raise UpdateError("The updated bundle changes the trusted update key")
                if not self._authorized(authorization):
                    raise UpdateError("Native shutdown authorization was withdrawn")
                self._integrate(bundle, settings)
                switched = True
                ready = self.state_dir / "restart-ready.json"
                ready.unlink(missing_ok=True)
                child = self._restart(bundle, launch_env, launch_args, ready)
                self._wait_ready(child, ready)
                atomic_json(self.state_dir / "previous-installation.json", {"bundle": str(self.bundle), "current_bundle": str(bundle)})
                return self._save("installed", installed_bundle=str(bundle), installed_version=signed["version"])
            except Exception as error:
                if child is not None and child.poll() is None:
                    # Only the task-created process group, never an unrelated
                    # user's instance. Server gets a chance to checkpoint.
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(child.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        child.wait(timeout=5)
                if switched:
                    try:
                        self._integrate(self.bundle, settings)
                        restored = True
                    except Exception as rollback_error:
                        error = UpdateError(f"{error}; launcher recovery failed: {rollback_error}")
                self._save("error", error=str(error), launchers_restored=restored,
                           recovery_required=child is not None)
                # Never automatically reopen an older app against a catalog a
                # newer app may already have migrated. Server-owned recovery
                # backups remain available and data is never overwritten here.
                raise error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "check", "download", "apply"))
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--wait-pid", type=int, action="append", default=[])
    parser.add_argument("--launch-env-json", default="{}")
    parser.add_argument("--launch-args-json", default="[]")
    parser.add_argument("--armed-file", type=Path)
    parser.add_argument("--authorization-directory", type=Path)
    args = parser.parse_args()
    updater = LinuxUpdater(args.bundle, args.state_dir)
    try:
        if args.action == "apply":
            result = updater.apply(wait_pids=args.wait_pid, launch_env=json.loads(args.launch_env_json),
                                   launch_args=json.loads(args.launch_args_json), armed_file=args.armed_file,
                                   authorization_directory=args.authorization_directory)
        else:
            result = getattr(updater, args.action)()
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({"state": "error", "error": str(error)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
