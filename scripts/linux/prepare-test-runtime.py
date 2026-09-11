#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Prepare pinned Python and film fixtures for Linux source tests, without packaging."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PINS = json.loads((ROOT / "packaging/linux/runtime.json").read_text())


def run(*arguments):
    subprocess.run(list(map(str, arguments)), check=True)


def checkout(url: str, revision: str, destination: Path) -> None:
    if destination.exists():
        actual = subprocess.check_output(["git", "-C", str(destination), "rev-parse", "HEAD"], text=True).strip()
        if actual != revision:
            raise RuntimeError(f"Existing test dependency has a different revision: {destination}")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    run("git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", url, destination)
    run("git", "-C", destination, "checkout", "--quiet", "--detach", revision)


def main() -> None:
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "aarch64"):
        raise SystemExit("The Linux test runtime requires x86_64 or ARM64 Linux")
    version = subprocess.check_output(["uv", "--version"], text=True).split()[1]
    if version != PINS["uv_version"]:
        raise SystemExit(f"Install uv=={PINS['uv_version']} first")
    build = ROOT / ".build/linux"
    key = f"cpython-{PINS['python_version']}-linux-{platform.machine()}-gnu"
    run("uv", "--no-config", "python", "install", "--install-dir", build / "python-installs", "--no-bin", key)
    python = build / "python-installs" / key / "bin/python3"
    venv = build / "test-runtime"
    if not venv.exists():
        run("uv", "--no-config", "venv", "--python", python, venv)
    run("uv", "--no-config", "pip", "sync", "--python", venv / "bin/python3", "--only-binary", ":all:",
        ROOT / "packaging/runtime-linux.lock")
    checkout("https://github.com/andreavolpato/agx-emulsion.git", PINS["python_source_revision"],
             ROOT / "vendor/spektrafilm")
    rust_source = build / "test-rust-source"
    checkout("https://github.com/turbasvin/spektrafilm-rs.git", PINS["rust_source_revision"], rust_source)
    shutil.copytree(rust_source / "data", ROOT / "engine/data", dirs_exist_ok=True)
    for source in (ROOT / "profiles").glob("*.json"):
        shutil.copy2(source, ROOT / "engine/data/profiles" / source.name)
    print(f"Linux test runtime ready: {venv / 'bin/python3'}")


if __name__ == "__main__":
    main()
