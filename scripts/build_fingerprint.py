#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Deterministic fingerprints for the incremental personal build.

The updater needs to answer two different questions: did an input source file
change, and did the toolchain used to compile that source change?  This module
keeps both questions in one process and hashes path names and file contents in
a canonical order.  It deliberately uses only the Python standard library so
it can run before the bundled application runtime has been staged.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import subprocess
import shutil
import sys
from typing import Any, Iterable, Mapping


FINGERPRINT_FORMAT = "build-fingerprint-v2"
IGNORED_DIRECTORY_NAMES = frozenset({".git", "target", "__pycache__", "xcuserdata"})


class FingerprintError(ValueError):
    """A required input cannot be fingerprinted safely."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _files_under(root: Path, path: Path) -> Iterable[Path]:
    """Yield files below *path*, excluding generated/cache directories."""
    if path.is_symlink():
        raise FingerprintError(f"symlink input is not allowed: {path}")
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        raise FingerprintError(f"missing input: {path}")
    for directory, directories, filenames in os.walk(path):
        for name in directories:
            if name not in IGNORED_DIRECTORY_NAMES and (Path(directory) / name).is_symlink():
                raise FingerprintError(f"symlink input is not allowed: {Path(directory) / name}")
        directories[:] = sorted(
            name
            for name in directories
            if name not in IGNORED_DIRECTORY_NAMES
        )
        for name in sorted(filenames):
            candidate = Path(directory) / name
            if candidate.is_symlink():
                raise FingerprintError(f"symlink input is not allowed: {candidate}")
            if candidate.is_file():
                yield candidate


def collect_files(root: Path, inputs: Iterable[str | os.PathLike[str]]) -> list[Path]:
    """Resolve input files/directories and return unique files in path order."""
    root = root.resolve()
    found: dict[str, Path] = {}
    for raw_input in inputs:
        candidate = Path(raw_input)
        if not candidate.is_absolute():
            candidate = root / candidate
        if glob.has_magic(os.fspath(candidate)):
            candidates = [Path(value) for value in glob.glob(os.fspath(candidate))]
            if not candidates:
                raise FingerprintError(f"missing input: {candidate}")
        else:
            candidates = [candidate]
        for matched in candidates:
            if matched.is_symlink():
                raise FingerprintError(f"symlink input is not allowed: {matched}")
            path_root = matched.resolve()
            for path in _files_under(root, path_root):
                relative = _relative_path(root, path)
                if any(part in IGNORED_DIRECTORY_NAMES for part in Path(relative).parts):
                    continue
                found[relative] = path
    return [found[relative] for relative in sorted(found)]


def fingerprint(
    root: Path,
    inputs: Iterable[str | os.PathLike[str]],
    *,
    component: str,
    toolchain: Mapping[str, Any] | None = None,
) -> str:
    """Return a content/path/toolchain fingerprint for a component.

    File metadata such as mtime, mode, and inode is intentionally excluded.
    Each path and content length is framed before the content, avoiding
    ambiguous concatenations while retaining deterministic behavior across
    checkouts and machines.
    """
    root = root.resolve()
    digest = hashlib.sha256()
    digest.update(FINGERPRINT_FORMAT.encode("ascii"))
    digest.update(b"\0component\0")
    digest.update(component.encode("utf-8"))
    digest.update(b"\0toolchain\0")
    digest.update(_canonical_json(toolchain or {}))
    for path in collect_files(root, inputs):
        relative = _relative_path(root, path).encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def legacy_fingerprint(root: Path, inputs: Iterable[str | os.PathLike[str]]) -> str:
    """Reproduce the former shell hash for one-time manifest migration."""
    lines = []
    for path in collect_files(root, inputs):
        relative = _relative_path(root.resolve(), path)
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}")
    payload = ("\n".join(sorted(lines)) + "\n").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()



PACKAGING_INPUTS = (
    "app/Info.plist",
    "LightTable.xcodeproj/project.pbxproj",
    "LightTable.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved",
)


def version_only_change(root: Path, revision: str) -> bool:
    """Allow only bundle/project version changes against a clean base revision.

    Parse plist values by key: a numeric minimum OS or other packaging value
    must never be mistaken for a bundle version. Unknown/missing bases fail
    closed. The caller must establish that its base was built from clean Git.
    """
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
        return False
    try:
        for relative in PACKAGING_INPUTS:
            old = subprocess.run(
                ["git", "-C", str(root), "show", f"{revision}:{relative}"],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
            ).stdout
            new = (root / relative).read_bytes()
            if relative.endswith(".plist"):
                old, new = plistlib.loads(old), plistlib.loads(new)
                if not isinstance(old, dict) or not isinstance(new, dict):
                    return False
                for key in ("CFBundleVersion", "CFBundleShortVersionString", "SUFeedURL"):
                    old.pop(key, None)
                    new.pop(key, None)
            elif relative.endswith(".pbxproj"):
                # The project serializer keeps one setting per line. Strip
                # only complete assignments of the two known version keys.
                pattern = rb"(?m)^[ \t]*(?:MARKETING_VERSION|CURRENT_PROJECT_VERSION)[ \t]*=[^;\n]*;[ \t]*\r?\n"
                old, new = re.sub(pattern, b"", old), re.sub(pattern, b"", new)
            if old != new:
                return False
    except (OSError, ValueError, plistlib.InvalidFileException, subprocess.SubprocessError):
        return False
    return True


def cargo_configuration(root: Path) -> dict[str, str]:
    """Include inherited Cargo configuration without emitting its contents."""
    directories = [root, *root.parents]
    paths = [directory / ".cargo" / name for directory in directories
             for name in ("config", "config.toml")]
    cargo_home = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
    paths.extend(cargo_home / name for name in ("config", "config.toml"))
    result = {}
    for path in paths:
        if path.is_symlink() and not path.exists():
            raise FingerprintError(f"missing Cargo configuration target: {path}")
        if path.is_file():
            result[f"{len(result)}:{path.name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _run_capture(command: list[str]) -> str:
    if shutil.which(command[0]) is None:
        raise FingerprintError(f"toolchain command unavailable: {command[0]}")
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise FingerprintError(f"toolchain query failed: {command[0]}") from error
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        raise FingerprintError(f"toolchain query failed: {command[0]}")
    return output


def detected_toolchains(root: Path) -> dict[str, dict[str, Any]]:
    """Describe compiler/SDK settings used by the personal updater.

    The exact command outputs are part of the identity.  This function only
    queries tool versions and paths; it never compiles or starts the app.
    """
    native_options = [
        "swiftc",
        "-O",
        "-whole-module-optimization",
        "-swift-version",
        "5",
        "-target",
        "arm64-apple-macos13.0",
        "-module-name",
        "LightTable",
        "-module-cache-path",
        "<temporary-module-cache>",
        "-F",
        "<base-frameworks>",
        "-framework",
        "Sparkle",
        "-Xlinker",
        "-rpath",
        "-Xlinker",
        "@executable_path/../Frameworks",
    ]
    helper_options = [
        "swiftc",
        "-O",
        "-swift-version",
        "5",
        "-target",
        "arm64-apple-macos13.0",
        "-module-cache-path",
        "<temporary-module-cache>",
    ]
    engine_options = ["cargo", "build", "--release", "--locked", "--manifest-path", "rust-engine/Cargo.toml", "CARGO_TARGET_DIR=<incremental-cache>"]
    swift = {
        "compiler": _run_capture(["swiftc", "--version"]),
        "compiler_path": shutil.which("swiftc") or "unavailable",
        "sdk_path": _run_capture(["xcrun", "--sdk", "macosx", "--show-sdk-path"]),
        "sdk_version": _run_capture(["xcrun", "--sdk", "macosx", "--show-sdk-version"]),
        "SDKROOT": os.environ.get("SDKROOT", "<unset>"),
        "DEVELOPER_DIR": os.environ.get("DEVELOPER_DIR", "<unset>"),
        "platform": platform.platform(),
    }
    rust = {
        "rustc": _run_capture([os.environ.get("RUSTC", "rustc"), "--version", "--verbose"]),
        "rustc_path": shutil.which("rustc") or "unavailable",
        "cargo": _run_capture(["cargo", "--version"]),
        "cargo_path": shutil.which("cargo") or "unavailable",
        "RUSTFLAGS": os.environ.get("RUSTFLAGS", "<unset>"),
        "CARGO_BUILD_TARGET": os.environ.get("CARGO_BUILD_TARGET", "<unset>"),
        "build_environment": {key: value for key, value in os.environ.items()
                              if key in ("RUSTC", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER",
                                         "CARGO_ENCODED_RUSTFLAGS", "CARGO_BUILD_RUSTFLAGS",
                                         "SDKROOT", "DEVELOPER_DIR", "MACOSX_DEPLOYMENT_TARGET")
                              or key.startswith(("CARGO_PROFILE_RELEASE_", "CARGO_TARGET_"))},
        "cargo_configuration": cargo_configuration(root),
        "platform": platform.platform(),
    }
    return {
        "native": {"version": 1, "options": native_options, "toolchain": swift},
        "helper": {"version": 1, "options": helper_options, "toolchain": swift},
        "engine": {"version": 1, "options": engine_options, "toolchain": rust},
    }


def _load_toolchains(raw: str | None, root: Path) -> dict[str, Mapping[str, Any]]:
    if raw is None:
        return detected_toolchains(root)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("toolchain data must be a JSON object")
    return value


def build_fingerprints(root: Path, toolchains: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """Build all updater keys from one source walk per component."""
    workflow = ["scripts/update-personal-app.sh", "scripts/build_fingerprint.py"]
    native = [
        "app/main.swift",
        "app/NativePreview.swift",
        "app/DiagnosticReports.swift",
        *workflow,
    ]
    xcode_config = [
        "app/Info.plist",
        "LightTable.xcodeproj/project.pbxproj",
        "LightTable.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved",
    ]
    helper = ["film_lab_ai/vision_helper.swift", "film_lab_ai/enhance_helper.swift", *workflow]
    engine = ["rust-engine"]
    engine.extend(name for name in ("rust-toolchain", "rust-toolchain.toml")
                  if (root / name).exists())
    if (root / ".cargo").exists():
        engine.append(".cargo")
    models = [
        "scripts/models/denoise.mlpackage",
        "scripts/models/models.json",
        "scripts/models/SCUNet-CODE-LICENSE.txt",
        "scripts/models/SCUNet-WEIGHTS-LICENSE.txt",
        "scripts/models/selfie_multiclass_256x256.tflite",
        "scripts/models/HairSegmentation-APACHE-2.0.txt",
        "scripts/models/hair-model.json",
    ]
    source_tree = [
        "*.py",
        "lighttable",
        "lighttable_cli",
        "media-formats.json",
        "web",
        "profiles",
        "presets",
        "film_lab_ai",
        "app/main.swift",
        "app/NativePreview.swift",
        "app/DiagnosticReports.swift",
        "app/NativePreview.metal",
        "rust-engine",
        *workflow,
    ]
    if (root / ".cargo").exists():
        source_tree.append(".cargo")

    # The old manifest format did not encode toolchain identity.  Including a
    # versioned format marker in every key makes an old manifest miss safely.
    result: dict[str, str] = {"FINGERPRINT_FORMAT": FINGERPRINT_FORMAT}
    result["LEGACY_XCODE_CONFIG_HASH"] = legacy_fingerprint(root, xcode_config)
    result["NATIVE_HASH"] = fingerprint(root, native, component="native", toolchain=toolchains.get("native", {}))
    result["NATIVE_TOOLCHAIN_HASH"] = fingerprint(
        root, [], component="native-toolchain", toolchain=toolchains.get("native", {})
    )
    result["XCODE_CONFIG_HASH"] = fingerprint(root, xcode_config, component="xcode-config")
    result["HELPER_HASH"] = fingerprint(root, helper, component="helper", toolchain=toolchains.get("helper", {}))
    result["HELPER_TOOLCHAIN_HASH"] = fingerprint(
        root, [], component="helper-toolchain", toolchain=toolchains.get("helper", {})
    )
    result["ENGINE_HASH"] = fingerprint(root, engine, component="engine", toolchain=toolchains.get("engine", {}))
    result["ENGINE_TOOLCHAIN_HASH"] = fingerprint(
        root, [], component="engine-toolchain", toolchain=toolchains.get("engine", {})
    )
    result["MODEL_HASH"] = fingerprint(root, models, component="models")
    result["SOURCE_TREE_HASH"] = fingerprint(root, source_tree, component="source-tree")
    return result


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--toolchain-data",
        help="JSON object containing native/helper/engine identities; skips tool discovery",
    )
    parser.add_argument("--version-only-since", help="check packaging against this clean base revision")
    parser.add_argument("--packaging-only", action="store_true", help="emit only the legacy-compatible Xcode packaging provenance")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a JSON object instead of KEY=value lines",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    try:
        xcode_config = [
            "app/Info.plist",
            "LightTable.xcodeproj/project.pbxproj",
            "LightTable.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved",
        ]
        if args.version_only_since:
            return 0 if version_only_change(args.root, args.version_only_since) else 1
        if args.packaging_only:
            values = {"LEGACY_XCODE_CONFIG_HASH": legacy_fingerprint(args.root, xcode_config)}
        else:
            values = build_fingerprints(args.root, _load_toolchains(args.toolchain_data, args.root))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"build fingerprint failed: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(values, sort_keys=True))
    else:
        for key, value in values.items():
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
