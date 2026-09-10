#!/usr/bin/env python3
"""Small, fail-closed release metadata and additive publication tools (stdlib only)."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import quote
import zipfile


REPOSITORY = "reville/lighttable-digital-darkroom"
PLATFORMS = ("linux-x86_64", "windows-x64", "macos-arm64")
STATES = ("candidate", "blocked", "ready", "published")
SIGNING = ("ed25519", "authenticode", "developer-id-notarized", "ad-hoc", "unsigned")
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-beta\.[1-9][0-9]*)?\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
FEEDS = {"linux-x86_64": "linux-x86_64.json", "windows-x64": "appcast-windows-x64.xml", "macos-arm64": "appcast.xml"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    with Path(path).open(encoding="utf-8-sig") as source:
        return json.load(source)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def file_identity(path):
    path = Path(path)
    require(not path.is_symlink() and path.is_file(), f"not a regular artifact: {path.name}")
    before = path.stat()
    require(before.st_size > 0, f"empty artifact: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    require(all(getattr(before, key) == getattr(after, key) for key in keys), f"artifact changed while hashing: {path.name}")
    return {"name": path.name, "bytes": after.st_size, "sha256": digest.hexdigest()}


def asset_url(manifest, name, platform=None):
    entry = manifest["platforms"][platform] if platform else {}
    tag = entry.get("tag", manifest.get("tag", "v" + manifest["version"]))
    return f"https://github.com/{manifest.get('repository', REPOSITORY)}/releases/download/{tag}/{quote(name, safe='')}"


def platform_names(version, platform):
    stem = f"LightTable-{version}-{platform}"
    return {"manifest": stem + "-release.json", "installers": stem + "-installers.tar.gz", "checksums": stem + "-SHA256SUMS"}


def validate_manifest(manifest):
    require(isinstance(manifest, dict) and type(manifest.get("schema_version")) is int and manifest["schema_version"] == 1, "unsupported release schema_version")
    version = manifest.get("version", "")
    require(isinstance(version, str) and VERSION.fullmatch(version), "invalid release version")
    require(isinstance(manifest.get("source_revision"), str) and REVISION.fullmatch(manifest["source_revision"]), "source_revision must be a full lowercase Git commit")
    repository = manifest.get("repository", REPOSITORY)
    require(repository == REPOSITORY, "release repository must be the canonical public repository")
    tag = manifest.get("tag", "v" + version)
    require(tag in ("v" + version, "macos-v" + version), "release tag does not own this version")
    platforms = manifest.get("platforms")
    require(isinstance(platforms, dict) and platforms and set(platforms) <= set(PLATFORMS), "invalid release platforms")
    if tag.startswith("macos-"):
        require(set(platforms) == {"macos-arm64"}, "macOS release tag cannot own other platforms")
    seen = set()
    for platform, entry in platforms.items():
        require(isinstance(entry, dict), "platform entry must be an object")
        platform_version = entry.get("version", "")
        own_source = entry.get("source_revision")
        if own_source is not None:
            require(isinstance(own_source, str) and REVISION.fullmatch(own_source), f"{platform}: invalid source_revision")
        require(isinstance(platform_version, str) and VERSION.fullmatch(platform_version) and
                (platform_version == version or ("-beta." not in version and platform_version.split("-beta.")[0] == version) or (own_source is not None and "tag" in entry)),
                f"{platform}: version differs from release")
        require(entry.get("channel") == ("beta" if "-beta." in platform_version else "stable"), f"{platform}: channel does not match version")
        platform_tag = entry.get("tag", tag)
        require(platform_tag == "v" + platform_version or (platform == "macos-arm64" and platform_tag == "macos-v" + platform_version), f"{platform}: tag does not own platform version")
        if "build_source_revision" in entry:
            require(isinstance(entry["build_source_revision"], str) and REVISION.fullmatch(entry["build_source_revision"]), "invalid build_source_revision")
            require(isinstance(entry.get("source_tree"), str) and REVISION.fullmatch(entry["source_tree"]), "build_source_revision requires source_tree evidence")
        require(entry.get("state") in STATES, f"{platform}: invalid state")
        require(isinstance(entry.get("minimum_os"), str) and entry["minimum_os"].strip(), f"{platform}: minimum_os is required")
        require(entry.get("update_owner") in ("app", "package-manager", "manual"), f"{platform}: invalid update_owner")
        require(entry.get("signing") in SIGNING, f"{platform}: invalid signing declaration")
        gates = entry.get("gates", [])
        require(isinstance(gates, list) and all(isinstance(gate, str) and gate.strip() for gate in gates), f"{platform}: invalid gates")
        validation = entry.get("validation", {})
        require(isinstance(validation, dict) and validation.get("status") in ("pending", "passed", "failed"), f"{platform}: invalid validation status")
        receipts = validation.get("receipts", [])
        require(isinstance(receipts, list) and all(isinstance(item, str) and item.strip() for item in receipts), f"{platform}: invalid validation receipts")
        artifacts = entry.get("artifacts")
        require(isinstance(artifacts, list), f"{platform}: artifacts must be an array")
        if entry["state"] in ("ready", "published"):
            require(artifacts, f"{platform}: ready release needs artifacts")
        for artifact in artifacts:
            require(isinstance(artifact, dict), "artifact must be an object")
            name = artifact.get("name", "")
            require(isinstance(name, str) and NAME.fullmatch(name), "artifact name must be a safe basename")
            require(name not in seen, f"artifact name collision: {name}")
            seen.add(name)
            names = platform_names(platform_version, platform)
            require(name != names["manifest"], "manifest cannot contain its own checksum")
            # Generic SHA256SUMS/installers archives collide when another platform joins.
            canonical = name.startswith(f"LightTable-{platform_version}-{platform}.") or name.startswith(f"LightTable-{platform_version}-{platform}-")
            if platform == "linux-x86_64":
                canonical = canonical or bool(re.fullmatch(r"lighttable-bin-" + re.escape(platform_version) + r"-[1-9][0-9]*-x86_64\.pkg\.tar\.zst(?:\.sha256)?", name))
            require(canonical or name == FEEDS[platform], f"{platform}: artifact name does not own version/platform: {name}")
            require(artifact.get("url") == asset_url(manifest, name, platform), f"artifact URL/tag ownership mismatch: {name}")
            require(type(artifact.get("bytes")) is int and artifact["bytes"] > 0, f"invalid artifact size: {name}")
            require(isinstance(artifact.get("sha256"), str) and SHA.fullmatch(artifact["sha256"]), f"invalid artifact SHA-256: {name}")
    return manifest


def promotion_gates(platform, entry):
    gates = list(entry.get("gates", []))
    if entry["state"] not in ("ready", "published"):
        gates.append("platform is not ready for publication")
    if entry["validation"]["status"] != "passed" or not entry["validation"].get("receipts"):
        gates.append("exact-artifact acceptance receipts have not passed")
    required = {"linux-x86_64": "ed25519", "windows-x64": "authenticode", "macos-arm64": "developer-id-notarized"}[platform]
    beta_manual = platform == "macos-arm64" and entry["channel"] == "beta" and entry["update_owner"] != "app" and entry["signing"] == "ad-hoc"
    if entry["signing"] != required and not beta_manual:
        gates.append(f"required signing is {required}")
    return gates


def preflight(manifest, capabilities, selected, phase="build"):
    validate_manifest(manifest)
    require(isinstance(capabilities, dict), "capabilities must be an object containing presence booleans only")
    common = []
    if capabilities.get("source_revision") != manifest["source_revision"]:
        common.append("source revision mismatch")
    if capabilities.get("source_clean") is not True:
        common.append("source checkout is not verified clean")
    if capabilities.get("tag_revision") != manifest["source_revision"]:
        common.append("tag revision is missing or mismatched")
    matrix = {}
    for platform in sorted(manifest["platforms"]):
        entry = manifest["platforms"][platform]
        present = capabilities.get("platforms", {}).get(platform, {})
        credentials = present.get("credentials", {})
        require(isinstance(credentials, dict) and all(type(value) is bool for value in credentials.values()), "credential metadata must contain booleans, never secret values")
        build_gates = list(common)
        for key in ("release_environment_allowed", "build_runner_ready"):
            if present.get(key) is not True:
                build_gates.append(key.replace("_", " "))
        needed = {"linux-x86_64": ["linux_update_signing"], "windows-x64": ["windows_authenticode", "sparkle_signing"], "macos-arm64": ["macos_developer_id", "macos_notarization", "sparkle_signing"]}[platform]
        if platform == "macos-arm64" and entry["channel"] == "beta" and entry["signing"] == "ad-hoc" and entry["update_owner"] != "app":
            needed = []
        for key in needed:
            if credentials.get(key) is not True:
                build_gates.append(f"missing credential capability: {key}")
        publish_gates = list(build_gates) + promotion_gates(platform, entry)
        if present.get("native_acceptance_ready") is not True:
            publish_gates.append("native acceptance environment unavailable")
        matrix[platform] = {"selected": platform in selected, "build_ready": not build_gates, "promotion_ready": not publish_gates, "build_gates": build_gates, "promotion_gates": publish_gates}
    require(selected and set(selected) <= set(matrix), "explicit platform selection must exist in the manifest")
    return {"phase": phase, "ready": all(matrix[key]["build_ready" if phase == "build" else "promotion_ready"] for key in selected), "platforms": matrix}


def check_build_manifest(value, manifest, platform):
    entry = manifest["platforms"][platform]
    require(value.get("version") == entry["version"], "embedded build manifest version mismatch")
    require(value.get("source_revision") == entry.get("build_source_revision", entry.get("source_revision", manifest["source_revision"])), "embedded build manifest source mismatch")
    require(value.get("source_dirty") is False, "embedded build manifest is not clean")
    os_name, architecture = platform.rsplit("-", 1)
    require(value.get("platform") in (os_name, "darwin" if os_name == "macos" else os_name), "embedded build manifest platform mismatch")
    require(value.get("architecture") in (architecture, "AMD64" if architecture == "x64" else architecture), "embedded build manifest architecture mismatch")
    if platform == "windows-x64" and entry["signing"] == "authenticode":
        require(value.get("authenticode_signed") is True, "embedded Windows build does not declare Authenticode signing")


def verify_embedded(path, manifest, platform):
    """Read only the required metadata member; never extract untrusted archives."""
    canonical = f"LightTable-{manifest['platforms'][platform]['version']}-{platform}"
    value = None
    if path.name == canonical + ".tar.gz" and platform == "linux-x86_64":
        with tarfile.open(path, "r:gz") as archive:
            matches = [member for member in archive if member.name == "LightTable/build-manifest.json"]
            require(len(matches) == 1 and matches[0].isfile() and matches[0].size <= 65536, "missing or ambiguous embedded build manifest")
            value = json.loads(archive.extractfile(matches[0]).read().decode("utf-8-sig"))
    elif path.name == canonical + ".zip" and platform == "windows-x64":
        with zipfile.ZipFile(path) as archive:
            matches = [member for member in archive.infolist() if member.filename == "LightTable/build-manifest.json"]
            require(len(matches) == 1 and matches[0].file_size <= 65536, "missing or ambiguous embedded build manifest")
            value = json.loads(archive.read(matches[0]).decode("utf-8-sig"))
    if value is not None:
        check_build_manifest(value, manifest, platform)


def verify_artifacts(manifest, directory, selected=None):
    validate_manifest(manifest)
    selected = selected or list(manifest["platforms"])
    require(set(selected) <= set(manifest["platforms"]), "unknown selected platform")
    results = []
    for platform in sorted(selected):
        require(manifest["platforms"][platform]["artifacts"], f"{platform}: no artifacts to verify")
        for artifact in manifest["platforms"][platform]["artifacts"]:
            path = Path(directory) / artifact["name"]
            identity = file_identity(path)
            require(identity["bytes"] == artifact["bytes"] and identity["sha256"] == artifact["sha256"], f"artifact bytes/checksum mismatch: {path.name}")
            verify_embedded(path, manifest, platform)
            results.append(identity)
    return results


def publication_plan(manifest, existing, selected):
    validate_manifest(manifest)
    require(selected and set(selected) <= set(manifest["platforms"]), "explicit platform selection must exist in manifest")
    before = {}
    for asset in existing:
        require(asset["name"] not in before, f"duplicate remote asset: {asset['name']}")
        before[asset["name"]] = asset
    result = []
    for platform in sorted(selected):
        gates = promotion_gates(platform, manifest["platforms"][platform])
        require(not gates, f"{platform} blocked: {'; '.join(gates)}")
        for asset in sorted(manifest["platforms"][platform]["artifacts"], key=lambda value: value["name"]):
            previous = before.get(asset["name"])
            if previous is not None:
                require(previous.get("sha256") == asset["sha256"] and previous.get("bytes") == asset["bytes"], f"immutable asset collision: {asset['name']}")
            result.append({**asset, "action": "skip" if previous is not None else "add"})
    return result


def gh(*args):
    return subprocess.run(["gh", *args], text=True, capture_output=True, check=True).stdout


def remote_assets(manifest):
    repository = manifest.get("repository", REPOSITORY)
    tag = manifest.get("tag", "v" + manifest["version"])
    revision = gh("api", f"repos/{repository}/commits/{tag}", "--jq", ".sha").strip()
    require(revision == manifest["source_revision"], "remote release tag source mismatch")
    release = json.loads(gh("release", "view", tag, "--repo", repository, "--json", "assets,tagName"))
    require(release["tagName"] == tag, "remote release tag mismatch")
    result = []
    desired = {asset["name"] for entry in manifest["platforms"].values() for asset in entry["artifacts"]}
    desired.update(platform_names(manifest["version"], key)["manifest"] for key in manifest["platforms"])
    for asset in release["assets"]:
        name = asset["name"]
        if name not in desired:
            continue
        require(NAME.fullmatch(name), "unsafe remote asset name")
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:") and SHA.fullmatch(digest[7:]):
            result.append({"name": name, "bytes": asset["size"], "sha256": digest[7:]})
        else:
            # Older assets have no GitHub digest. Hash actual downloaded bytes.
            with tempfile.TemporaryDirectory(prefix="lighttable-release-verify-") as temporary:
                gh("release", "download", tag, "--repo", repository, "--pattern", name, "--dir", temporary)
                identity = file_identity(Path(temporary) / name)
                require(identity["bytes"] == asset["size"], f"remote download size mismatch: {name}")
                result.append(identity)
    return result


def publish(manifest_path, directory, selected, apply=False):
    manifest = validate_manifest(read_json(manifest_path))
    require(len(selected) == 1 and set(manifest["platforms"]) == set(selected), "publish consumes one immutable platform manifest per invocation")
    platform = selected[0]
    entry = manifest["platforms"][platform]
    require(entry["version"] == manifest["version"] and entry.get("tag", manifest.get("tag", "v" + manifest["version"])) == manifest.get("tag", "v" + manifest["version"]), "publish needs a standalone platform manifest, not aggregate version/tag overrides")
    require(entry.get("source_revision", manifest["source_revision"]) == manifest["source_revision"], "publish platform source must match release source")
    require(entry.get("build_source_revision", manifest["source_revision"]) == manifest["source_revision"], "publication requires exact source revision; historical same-tree records are metadata only")
    name = platform_names(manifest["version"], platform)["manifest"]
    require(Path(manifest_path).name == name, f"manifest must use immutable platform filename: {name}")
    verify_artifacts(manifest, directory, selected)
    existing = remote_assets(manifest)
    plan = publication_plan(manifest, existing, selected)
    identity = file_identity(manifest_path)
    previous = next((asset for asset in existing if asset["name"] == name), None)
    require(previous is None or (previous["sha256"] == identity["sha256"] and previous["bytes"] == identity["bytes"]), f"immutable manifest collision: {name}")
    plan.append({**identity, "url": asset_url(manifest, name), "action": "skip" if previous else "add"})
    if apply:
        for asset in plan:
            if asset["action"] == "add":
                path = Path(manifest_path) if asset["name"] == name else Path(directory) / asset["name"]
                # Recheck immediately before upload; never pass --clobber. A concurrent
                # publisher causes a safe failure and the next invocation resumes.
                current = file_identity(path)
                require(current["sha256"] == asset["sha256"] and current["bytes"] == asset["bytes"], "artifact changed after planning")
                gh("release", "upload", manifest.get("tag", "v" + manifest["version"]), str(path.resolve()), "--repo", manifest.get("repository", REPOSITORY))
        after = {asset["name"]: asset for asset in remote_assets(manifest)}
        require(all(after.get(asset["name"], {}).get("sha256") == asset["sha256"] and after[asset["name"]]["bytes"] == asset["bytes"] for asset in plan), "post-upload asset verification failed")
    return {"applied": apply, "source_revision": manifest["source_revision"], "assets": plan}


def create_manifest(args):
    entry = {"version": args.version, "channel": args.channel, "minimum_os": args.minimum_os, "update_owner": args.update_owner, "state": args.state, "signing": args.signing, "gates": args.gate, "validation": {"status": args.validation_status, "receipts": args.receipt}, "artifacts": []}
    if args.build_run_id:
        entry["build_run_id"] = args.build_run_id
    manifest = {"schema_version": 1, "repository": REPOSITORY, "tag": args.tag or "v" + args.version, "version": args.version, "source_revision": args.source_revision, "platforms": {args.platform: entry}}
    for name in sorted(args.artifact):
        require(NAME.fullmatch(name), "artifact must be a safe basename")
        entry["artifacts"].append({**file_identity(Path(args.artifacts_dir) / name), "url": asset_url(manifest, name)})
    validate_manifest(manifest)
    if entry["artifacts"]:
        verify_artifacts(manifest, args.artifacts_dir)
    write_json(args.output, manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-manifest")
    for key in ("version", "source-revision", "artifacts-dir", "output", "minimum-os"):
        create.add_argument("--" + key, required=True)
    create.add_argument("--platform", choices=PLATFORMS, required=True)
    create.add_argument("--channel", choices=("stable", "beta"), required=True)
    create.add_argument("--update-owner", choices=("app", "package-manager", "manual"), required=True)
    create.add_argument("--state", choices=STATES, default="candidate")
    create.add_argument("--signing", choices=SIGNING, required=True)
    create.add_argument("--validation-status", choices=("pending", "passed", "failed"), default="pending")
    create.add_argument("--artifact", action="append", default=[])
    create.add_argument("--gate", action="append", default=[])
    create.add_argument("--receipt", action="append", default=[])
    create.add_argument("--build-run-id")
    create.add_argument("--tag")
    for command in ("verify", "preflight", "plan", "publish"):
        sub = commands.add_parser(command)
        sub.add_argument("--manifest", required=True)
        if command in ("preflight", "plan", "publish"):
            sub.add_argument("--platform", choices=PLATFORMS, action="append", required=True)
        if command in ("verify", "publish"):
            sub.add_argument("--artifacts-dir", required=True)
        if command == "preflight":
            sub.add_argument("--capabilities", required=True)
            sub.add_argument("--phase", choices=("build", "publish"), default="build")
        if command == "plan":
            sub.add_argument("--existing", required=True, help="JSON array of existing {name, bytes, sha256}")
        if command == "publish":
            sub.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "create-manifest":
            result = create_manifest(args)
        else:
            manifest = validate_manifest(read_json(args.manifest))
            if args.command == "verify":
                result = {"verified": verify_artifacts(manifest, args.artifacts_dir)}
            elif args.command == "preflight":
                result = preflight(manifest, read_json(args.capabilities), args.platform, args.phase)
            elif args.command == "plan":
                result = {"assets": publication_plan(manifest, read_json(args.existing), args.platform)}
            else:
                result = publish(args.manifest, args.artifacts_dir, args.platform, args.apply)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2 if args.command == "preflight" and not result["ready"] else 0
    except (ValueError, OSError, KeyError, TypeError, tarfile.TarError, zipfile.BadZipFile, subprocess.CalledProcessError) as error:
        # Never echo subprocess output; GitHub errors can include credential diagnostics.
        message = "GitHub command failed; no existing assets were replaced" if isinstance(error, subprocess.CalledProcessError) else str(error)
        print(f"release-process: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
