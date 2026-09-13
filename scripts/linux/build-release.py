#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build a Linux bundle on the oldest supported Linux distribution."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PINS = json.loads((ROOT / "packaging/linux/runtime.json").read_text())
APP_VERSION = re.search(r'^VERSION = "([^"]+)"$', (ROOT / "app_version.py").read_text(),
                        re.MULTILINE).group(1)


def run(*arguments: str | Path, **kwargs) -> str:
    print("+ " + " ".join(map(str, arguments)), flush=True)
    return subprocess.check_output(list(map(str, arguments)), text=True, **kwargs).strip()


def checkout(url: str, revision: str, destination: Path) -> None:
    run("git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", url, destination)
    run("git", "-C", destination, "checkout", "--quiet", "--detach", revision)
    if run("git", "-C", destination, "rev-parse", "HEAD") != revision:
        raise RuntimeError("Dependency source revision does not match its pin")


def copy_tree(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))


def stage_resources(project: Path, python_source: Path, rust_source: Path, bundle: Path) -> None:
    resources = bundle / "Resources/LightTable"
    resources.mkdir(parents=True)
    # Match the macOS packager: include every top-level module so new shared
    # imports do not silently disappear from the Linux runtime.
    for source in project.glob("*.py"):
        shutil.copy2(source, resources / source.name)
    shutil.copy2(project / "media-formats.json", resources)
    (resources / "build").mkdir()
    shutil.copy2(project / "build/icon-1024.png", resources / "build/icon-1024.png")
    for name in ("lighttable_cli", "web", "profiles", "presets"):
        copy_tree(project / name, resources / name)
    (resources / "film_lab_ai").mkdir()
    for source in (project / "film_lab_ai").glob("*.py"):
        shutil.copy2(source, resources / "film_lab_ai" / source.name)
    shutil.copytree(project / "film_lab_ai/licenses", resources / "film_lab_ai/licenses")
    copy_tree(python_source / "src", resources / "vendor/spektrafilm/src")
    copy_tree(rust_source / "data", resources / "engine/data")
    for source in (project / "profiles").glob("*.json"):
        shutil.copy2(source, resources / "engine/data/profiles" / source.name)
    (resources / "engine/VERSION.txt").write_text(PINS["rust_source_revision"] + "\n")
    (resources / "vendor/spektrafilm/VERSION.txt").write_text(PINS["python_source_revision"] + "\n")
    licenses = resources / "licenses"
    licenses.mkdir()
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copy2(project / name, bundle)
    # docs/ carries the CLI reference and the Linux guide into the bundle.
    copy_tree(project / "docs", bundle / "docs")
    (bundle / "packaging/linux/arch").mkdir(parents=True)
    shutil.copy2(project / "packaging/linux/arch/README.md", bundle / "packaging/linux/arch/README.md")
    shutil.copy2(python_source / "LICENSE", licenses / "python-render-engine.txt")
    shutil.copy2(rust_source / "LICENSE", licenses / "rust-render-engine.txt")
    shutil.copy2(project / "requirements-runtime.lock", licenses / "runtime-requirements.lock")
    shutil.copy2(project / "packaging/runtime-linux.lock", licenses / "runtime-linux.lock")
    shutil.copy2(project / "packaging/linux/runtime.json", licenses / "runtime-pins.json")
    (bundle / "bin").mkdir()
    shutil.copy2(project / "scripts/linux/lighttable", bundle / "bin/lighttable")
    shutil.copy2(project / "scripts/linux/lighttable-desktop", bundle / "bin/lighttable-desktop")
    for name in ("install.sh", "uninstall.sh", "desktop-integration.py", "runtime-smoke.py"):
        shutil.copy2(project / "scripts/linux" / name, bundle / name)
    (bundle / "share/icons").mkdir(parents=True)
    shutil.copy2(project / "build/icon-1024.png", bundle / "share/icons/lighttable.png")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=os.environ.get("LIGHTTABLE_VERSION", APP_VERSION))
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--experimental-aarch64", action="store_true",
                        help="Build an explicitly experimental native ARM64 validation package")
    parser.add_argument("--cargo-target-dir", type=Path, default=ROOT / ".build/linux/cargo")
    parser.add_argument("--shell-target-dir", type=Path, help="Reuse an existing desktop Cargo build directory")
    parser.add_argument("--engine-target-dir", type=Path, help="Reuse an existing resident-engine Cargo build directory")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[.-][0-9A-Za-z.-]+)?", args.version):
        parser.error("--version must be a semantic version such as 0.1.0")
    architecture = platform.machine()
    if platform.system() != "Linux" or architecture not in ("x86_64", "aarch64"):
        parser.error("Build on x86_64 Linux; Ubuntu 24.04 is the release baseline")
    if architecture == "aarch64" and not args.experimental_aarch64:
        parser.error("ARM64 is experimental; pass --experimental-aarch64 for a validation package")
    if args.experimental_aarch64 and architecture != "aarch64":
        parser.error("--experimental-aarch64 requires a native ARM64 Linux build host")
    for tool in ("cargo", "git", "rustup", "uv", "pkg-config"):
        if not shutil.which(tool):
            parser.error(f"{tool} is required; see docs/platforms/linux.md")
    if run("uv", "--version").split()[1] != PINS["uv_version"]:
        parser.error(f"Install uv=={PINS['uv_version']} for the pinned Python download metadata")
    run("pkg-config", "--exists", "gtk+-3.0", "webkit2gtk-4.1", "openssl", "openblas")
    run("rustup", "run", PINS["rust_version"], "rustc", "--version")
    source_revision = run("git", "-C", ROOT, "rev-parse", "HEAD")
    source_dirty = bool(run("git", "-C", ROOT, "status", "--porcelain", "--untracked-files=normal"))
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    runtime_pins = dict(PINS)
    if architecture == "aarch64":
        runtime_pins["target"] = "aarch64-unknown-linux-gnu"
        runtime_pins["python_key"] = "cpython-3.13.12-linux-aarch64-gnu"
    target = runtime_pins["target"]
    environment = os.environ.copy()
    environment["CARGO_TARGET_DIR"] = str(args.cargo_target_dir.resolve())
    environment["UV_CACHE_DIR"] = str(ROOT / ".build/linux/uv-cache")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix="lighttable-linux-build-") as temporary:
        build = Path(temporary)
        bundle = build / "LightTable"
        python_source, rust_source = build / "python-source", build / "rust-source"
        checkout("https://github.com/andreavolpato/agx-emulsion.git",
                 PINS["python_source_revision"], python_source)
        checkout("https://github.com/turbasvin/spektrafilm-rs.git",
                 PINS["rust_source_revision"], rust_source)
        stage_resources(ROOT, python_source, rust_source, bundle)
        (bundle / "Resources/LightTable/licenses/runtime-pins.json").write_text(
            json.dumps(runtime_pins, indent=2) + "\n")
        run("uv", "--no-config", "python", "install", "--install-dir", build / "python-installs",
            "--no-bin", runtime_pins["python_key"], env=environment)
        copy_tree(build / "python-installs" / runtime_pins["python_key"], bundle / "Python")
        python = bundle / "Python/bin/python3"
        run("uv", "--no-config", "pip", "sync", "--python", python, "--system",
            "--break-system-packages", "--only-binary", ":all:", ROOT / "packaging/runtime-linux.lock",
            env=environment)
        run(python, ROOT / "scripts/fetch-color-profiles.py",
            bundle / "Resources/LightTable/color-profiles", env=environment)
        for manifest, binary, destination in (
            (ROOT / "rust-engine/Cargo.toml", "lighttable-engine", bundle / "Resources/LightTable/engine/lighttable-engine"),
            (ROOT / "windows-shell/Cargo.toml", "lighttable-desktop-shell", bundle / "bin/lighttable-desktop-shell"),
            (rust_source / "Cargo.toml", "spektrafilm", bundle / "Resources/LightTable/engine/spektrafilm-rs"),
        ):
            cargo_environment = dict(environment)
            override = (args.shell_target_dir if binary == "lighttable-desktop-shell" else
                        args.engine_target_dir if binary == "lighttable-engine" else None)
            if override:
                cargo_environment["CARGO_TARGET_DIR"] = str(override.resolve())
            command = ["cargo", "+" + PINS["rust_version"], "build", "--release", "--locked",
                       "--target", target, "--manifest-path", manifest, "--bin", binary]
            if binary == "spektrafilm":
                command.extend(["-p", "spektrafilm-cli"])
            run(*command, env=cargo_environment)
            shutil.copy2(Path(cargo_environment["CARGO_TARGET_DIR"]) / target / "release" / binary, destination)
        manifest = {
            "version": args.version,
            "source_revision": source_revision,
            "source_dirty": source_dirty,
            "architecture": architecture,
            "experimental_architecture": architecture != "x86_64",
            "platform": "linux",
            "build_distribution": platform.freedesktop_os_release(),
            "runtime": runtime_pins,
            "runtime_requirements_sha256": hashlib.sha256((ROOT / "requirements-runtime.lock").read_bytes()).hexdigest(),
            "runtime_linux_requirements_sha256": hashlib.sha256((ROOT / "packaging/runtime-linux.lock").read_bytes()).hexdigest(),
        }
        (bundle / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (bundle / "installation-owner.json").write_text('{"owner":"portable"}\n')
        key = os.environ.get("LIGHTTABLE_LINUX_UPDATE_PUBLIC_KEY", "").strip()
        if key and len(base64.b64decode(key, validate=True)) != 32:
            parser.error("LIGHTTABLE_LINUX_UPDATE_PUBLIC_KEY must be a base64 Ed25519 public key")
        feed = os.environ.get("LIGHTTABLE_LINUX_UPDATE_FEED_URL", "").strip() or (
            "https://github.com/reville/lighttable-digital-darkroom/releases/download/desktop-updates/"
            f"linux-{architecture}.json")
        (bundle / "update-config.json").write_text(json.dumps({"public_key": key, "feed_url": feed}, indent=2) + "\n")
        if not key:
            print("Automatic updates are disabled: no Linux update public key was configured.")
        # Actually relocate before smoke testing; a successful build-directory
        # launch does not demonstrate that the downloaded Python is portable.
        relocated = build / "moved bundle with spaces" / "LightTable"
        relocated.parent.mkdir()
        bundle.rename(relocated)
        run(relocated / "Python/bin/python3", "-B", relocated / "runtime-smoke.py", relocated,
            env=environment)
        filename = f"LightTable-{args.version}-linux-{architecture}.tar.gz"
        archive = output / filename
        with tarfile.open(build / filename, "w:gz", dereference=False) as stream:
            stream.add(relocated, arcname="LightTable")
        shutil.move(build / filename, archive)
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        (output / (filename + ".sha256")).write_text(f"{checksum}  {filename}\n")
        print(f"Built and runtime-smoke-tested {archive}")


if __name__ == "__main__":
    main()
