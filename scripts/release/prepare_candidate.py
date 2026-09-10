#!/usr/bin/env python3
"""Prepare an existing successful build for independent promotion; never rebuild it."""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

import release_process as release

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = {"linux-x86_64": {".github/workflows/linux-build.yml", ".github/workflows/release.yml"},
             "windows-x64": {".github/workflows/windows-build.yml", ".github/workflows/release.yml"},
             "macos-arm64": {".github/workflows/release.yml"}}
MINIMUM_OS = {"linux-x86_64": "Ubuntu 24.04+; current Arch Linux/Omarchy", "windows-x64": "Windows 10/11 x64", "macos-arm64": "macOS 14 Sonoma or later"}


def build_run(run_id, platform):
    release.require(str(run_id).isdigit() and int(run_id) > 0, "build_run_id must be a positive GitHub run ID")
    run = json.loads(release.gh("api", f"repos/{release.REPOSITORY}/actions/runs/{run_id}"))
    release.require(run.get("status") == "completed", "original build run must have completed")
    release.require(run.get("head_repository", {}).get("full_name") == release.REPOSITORY, "build run must originate in the canonical repository")
    release.require(run.get("event") in ("push", "workflow_dispatch"), "pull-request or untrusted build events cannot supply release candidates")
    release.require(run.get("path", "").split("@")[0] in WORKFLOWS[platform], "selected run is not an allowed original build workflow")
    release.require(release.REVISION.fullmatch(run.get("head_sha", "")), "original build run has no immutable source SHA")
    pages = json.loads(release.gh("api", f"repos/{release.REPOSITORY}/actions/runs/{run_id}/jobs?per_page=100", "--paginate", "--slurp"))
    jobs = [job for page in pages for job in page["jobs"]]
    prefix = {"linux-x86_64": "linux", "windows-x64": "windows", "macos-arm64": "macos"}[platform]
    allowed = {prefix, prefix + "/package"} if platform == "macos-arm64" else {"package", prefix + "/package"}
    matches = [job for job in jobs if "".join(job["name"].split()) in allowed]
    release.require(len(matches) == 1 and matches[0].get("conclusion") == "success", "selected platform package job must have succeeded independently")
    expected_step = {"linux-x86_64": "Require X11 native edit, RGB16 export and normal reopen", "windows-x64": "Require native edit, export and restart", "macos-arm64": "Bind final Mac artifacts to native and signing proof"}[platform]
    release.require(any(step.get("name") == expected_step and step.get("conclusion") == "success" for step in matches[0].get("steps", [])), "selected platform job lacks successful native/package proof step")
    run["selected_job_id"] = matches[0]["id"]
    return run


def deterministic_archive(files, destination):
    with Path(destination).open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for name, content in sorted(files.items()):
                    payload = content.encode("utf-8") if isinstance(content, str) else content
                    member = tarfile.TarInfo(name)
                    member.size = len(payload)
                    member.mode = 0o644
                    archive.addfile(member, io.BytesIO(payload))


def installer_definitions(version, platform, directory, revision, tag):
    specification = importlib.util.spec_from_file_location("release_installers", ROOT / "scripts/generate-installers.py")
    installers = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(installers)
    if "-beta." in version:
        release.require(platform == "macos-arm64", "only manual macOS beta preparation is supported")
        dmg = Path(directory) / f"LightTable-{version}-macos-arm64.dmg"
        url = f"https://github.com/{release.REPOSITORY}/releases/download/{tag}/{dmg.name}"
        files = installers.homebrew(version, url, release.file_identity(dmg)["sha256"])
        files = {name.replace("lighttable.rb", "lighttable@beta.rb"): content.replace('cask "lighttable"', 'cask "lighttable@beta"').replace("auto_updates true", "auto_updates false").replace(":ventura", ":sonoma") for name, content in files.items()}
        return files
    channels = {"linux-x86_64": ["aur"], "windows-x64": ["scoop", "winget", "chocolatey"], "macos-arm64": ["homebrew"]}[platform]
    files = installers.generate(version, Path(directory), channels, "GPL-3.0-only", f"https://github.com/{release.REPOSITORY}/blob/{tag}/LICENSE", False, revision)
    if platform == "macos-arm64":
        files = {name: content.replace(":ventura", ":sonoma") for name, content in files.items()}
    return files


def reuse_public_feed(tag, name, output):
    """Reuse an already published feed verbatim; never re-sign its validity dates."""
    try:
        existing = json.loads(release.gh("api", f"repos/{release.REPOSITORY}/releases/tags/{tag}"))
    except subprocess.CalledProcessError as error:
        if "HTTP 404" in (error.stderr or ""):
            return False
        raise
    if existing.get("draft"):
        return False
    matches = [asset for asset in existing.get("assets", []) if asset.get("name") == name]
    if not matches:
        return False
    release.require(len(matches) == 1, "duplicate immutable feed assets")
    asset = matches[0]
    digest = asset.get("digest", "")
    release.require(isinstance(digest, str) and digest.startswith("sha256:") and release.SHA.fullmatch(digest[7:]), "existing immutable feed requires an authoritative GitHub SHA-256 digest")
    with tempfile.TemporaryDirectory(prefix="lighttable-existing-feed-") as temporary:
        release.gh("release", "download", tag, "--repo", release.REPOSITORY, "--pattern", name, "--dir", temporary)
        identity = release.file_identity(Path(temporary) / name)
        release.require(identity["bytes"] == asset["size"] and identity["sha256"] == digest[7:], "existing immutable feed download identity mismatch")
        shutil.copyfile(Path(temporary) / name, output)
    return True


def prepare(args):
    release.require(release.VERSION.fullmatch(args.version), "invalid release version")
    run = build_run(args.build_run_id, args.platform)
    source = args.source_revision or run["head_sha"]
    release.require(release.REVISION.fullmatch(source), "expected source must be a full Git commit")
    output = Path(args.output_dir)
    release.require(not output.exists() or not any(output.iterdir()), "output directory must be empty; reuse the completed preparation artifact for retries")
    output.mkdir(parents=True, exist_ok=True)
    platform, version = args.platform, args.version
    stem = f"LightTable-{version}-{platform}"
    beta = "-beta." in version
    release.require(not beta or platform == "macos-arm64", "only macOS beta is supported")
    tag = ("macos-v" if beta else "v") + version
    tag_source = release.gh("api", f"repos/{release.REPOSITORY}/commits/{tag}", "--jq", ".sha").strip()
    release.require(tag_source == source, "immutable platform tag does not match the expected build source")
    receipts = [f"https://github.com/{release.REPOSITORY}/actions/runs/{args.build_run_id}"]
    if args.native_run_id:
        release.require(str(args.native_run_id).isdigit() and int(args.native_run_id) > 0, "native_run_id must be a positive GitHub run ID")
        receipts.append(f"https://github.com/{release.REPOSITORY}/actions/runs/{args.native_run_id}")
    with tempfile.TemporaryDirectory(prefix="lighttable-build-download-") as temporary:
        release.gh("run", "download", str(args.build_run_id), "--repo", release.REPOSITORY, "--name", f"LightTable-{platform}", "--dir", temporary)
        downloaded = Path(temporary)
        expected = {"linux-x86_64": [stem + ".tar.gz"], "windows-x64": [stem + ".zip", stem + "-setup.exe", release.FEEDS[platform]], "macos-arm64": [stem + ".dmg"]}[platform]
        if platform == "macos-arm64" and not beta:
            expected.extend([stem + ".zip", release.FEEDS[platform]])
        for name in expected:
            release.file_identity(downloaded / name)
        for path in sorted(downloaded.iterdir()):
            release.require(path.is_file() and not path.is_symlink(), "original artifact must contain only regular top-level files")
            # Keep installer/archive/feed bytes unchanged. Namespace proof files so
            # platforms may join an existing public version without collisions.
            name = path.name if path.name.startswith(stem) or path.name == release.FEEDS[platform] else stem + "-" + path.name
            release.require(release.NAME.fullmatch(name), "unsafe downloaded artifact filename")
            shutil.copyfile(path, output / name)
    # Establish binary identity before generating or signing any derived metadata.
    verification = {"version": version, "source_revision": source, "platforms": {platform: {"version": version, "signing": "authenticode" if platform == "windows-x64" else "ed25519"}}}
    for path in output.iterdir():
        release.verify_embedded(path, verification, platform)
    if platform == "linux-x86_64" and not (output / release.FEEDS[platform]).exists() and not reuse_public_feed(tag, release.FEEDS[platform], output / release.FEEDS[platform]):
        subprocess.run([sys.executable, str(ROOT / "scripts/generate-linux-update.py"), "--archive", str(output / (stem + ".tar.gz")), "--output", str(output / release.FEEDS[platform]), "--download-url", f"https://github.com/{release.REPOSITORY}/releases/download/{tag}/{stem}.tar.gz", "--release-notes-url", f"https://github.com/{release.REPOSITORY}/releases/tag/{tag}"], check=True)
    if platform == "macos-arm64":
        proof = release.read_json(output / (stem + "-macos-release-proof.json"))
        release.require(proof.get("source_revision") == source and proof.get("version") == version and proof.get("source_dirty") is False, "Mac build proof source/version/clean identity mismatch")
    files = installer_definitions(version, platform, output, source, tag)
    names = release.platform_names(version, platform)
    deterministic_archive(files, output / names["installers"])
    identities = [release.file_identity(path) for path in sorted(output.iterdir())]
    (output / names["checksums"]).write_text("".join(f"{item['sha256']}  {item['name']}\n" for item in identities), encoding="utf-8")
    identity = {"schema_version": 1, "version": version, "repository": release.REPOSITORY, "tag": tag, "source_revision": source,
                "platforms": {platform: {"version": version, "channel": "beta" if beta else "stable", "minimum_os": MINIMUM_OS[platform],
                                         "update_owner": "manual" if beta else "app", "state": "candidate", "signing": "ad-hoc" if beta else {"linux-x86_64": "ed25519", "windows-x64": "authenticode", "macos-arm64": "developer-id-notarized"}[platform],
                                         "build_run_id": str(args.build_run_id), "build_workflow_revision": run["head_sha"], "gates": [],
                                         "validation": {"status": "pending", "receipts": receipts}, "artifacts": []}}}
    entry = identity["platforms"][platform]
    if args.native_run_id:
        entry["native_run_id"] = str(args.native_run_id)
    entry["artifacts"] = [{**release.file_identity(path), "url": release.asset_url(identity, path.name, platform)} for path in sorted(output.iterdir())]
    release.verify_artifacts(identity, output)
    release.write_json(output / names["manifest"], identity)
    return {"manifest": str(output / names["manifest"]), "source_revision": source, "build_run_id": str(args.build_run_id), "state": "candidate", "artifact": f"LightTable-{platform}-promotion"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=release.PLATFORMS)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-run-id", required=True)
    parser.add_argument("--native-run-id")
    parser.add_argument("--source-revision")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args), indent=2))
        return 0
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print("candidate preparation failed: " + ("required build, download or signing command failed" if isinstance(error, subprocess.CalledProcessError) else str(error)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
