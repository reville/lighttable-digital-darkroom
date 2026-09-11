#!/usr/bin/env python3
"""Verify and install native Rust notices for the immutable Linux 0.5.0 release.

No network, Cargo invocation, or source substitution occurs during packaging.
The retained objects are verbatim upstream files; provenance records their
archive checksums, source revisions, and Linux dependency graphs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[2]
NOTICES = ROOT / "packaging/snap/native-licenses"
VERSION = "0.5.0"
SOURCE_REVISION = "be537f2f3e2e431ae6b42af716c2a8b365f57bab"
UPSTREAM_REVISION = "9dd59b0380194b93686aaa230a8bb9680aa270a4"
RELEASE_SHA256 = "eaf1fb58d038d7260473343ab3bb64a3101ab5d180c09020af80b08929343f01"
LOCKS = {
    "desktop-shell": "34d62d9c9738e63a6deaf90e0d7454e2467d6ba185ce31703569699fc73bfeae",
    "resident-engine": "dc8490c31be6c282a767022ca3e357619fd4771565a42e13895c022e4611b47d",
    "film-cli": "5a89f1e09a241edf070dbd0b716809feee7b23c401810d3075ea44c008b200b8",
}
DIGEST = re.compile(r"[0-9a-f]{64}")


def regular_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Notice must be a regular file: {path}")
    return path.read_bytes()


def verify(source: Path = NOTICES, *, version: str = VERSION,
           source_revision: str = SOURCE_REVISION) -> dict:
    """Validate every retained object and its exact-release Cargo provenance."""
    if version != VERSION or source_revision != SOURCE_REVISION:
        raise ValueError("Native notice inventory is pinned to the exact Linux 0.5.0 source revision")
    if source.is_symlink() or (source / "objects").is_symlink():
        raise ValueError("Notice inventory directories cannot be symlinks")
    metadata = json.loads(regular_bytes(source / "provenance.json"))
    if (metadata.get("format") != 1 or metadata.get("version") != version
            or metadata.get("source_revision") != source_revision
            or metadata.get("upstream_film_revision") != UPSTREAM_REVISION
            or metadata.get("release_archive_sha256") != RELEASE_SHA256
            or metadata.get("target") != "x86_64-unknown-linux-gnu"
            or metadata.get("unresolved") != []):
        raise ValueError("Native notice inventory has an unsupported source identity or unresolved coverage")
    objects = metadata["objects"]
    content = {}
    for digest, info in objects.items():
        if not DIGEST.fullmatch(digest):
            raise ValueError("Invalid notice object digest")
        data = regular_bytes(source / "objects" / digest)
        if len(data) != info["size"] or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"Native notice checksum mismatch: {digest}")
        content[digest] = data
    if {p.name for p in (source / "objects").iterdir()} != set(objects):
        raise ValueError("Unexpected or missing native notice objects")
    retained = {}
    for row in metadata["files"]:
        path = row["path"]
        relative = PurePosixPath(path)
        if (not path or relative.is_absolute() or ".." in relative.parts
                or "\\" in path or str(relative) != path or path in retained
                or row["sha256"] not in content or not row.get("source")):
            raise ValueError("Invalid or duplicate native notice path/provenance")
        retained[path] = row
    if {r["sha256"] for r in retained.values()} != set(objects):
        raise ValueError("Unreferenced native notice object")
    packages = {}
    for row in metadata["packages"]:
        key = row["name"] + "-" + row["version"]
        if key in packages or not DIGEST.fullmatch(row["archive_sha256"]):
            raise ValueError("Invalid or duplicate native crate identity")
        files = row["files"]
        if (not row.get("declared_license") or not files or len(files) != len(set(files))
                or any(p not in retained or not p.startswith("crates/" + key + "/") for p in files)):
            raise ValueError(f"Missing retained notices for native crate {key}")
        packages[key] = row
    graphs = {g["name"]: g for g in metadata["graphs"]}
    if len(metadata["graphs"]) != len(LOCKS) or set(graphs) != set(LOCKS):
        raise ValueError("Missing exact-release native dependency graph")
    reached = {key: set() for key in packages}
    for name, expected in LOCKS.items():
        graph = graphs[name]
        lock = retained[graph["lock"]]
        if graph["lock"] != f"locks/{name}-Cargo.lock" or lock["sha256"] != expected:
            raise ValueError(f"Native dependency graph has the wrong release lockfile: {name}")
        rows = tomllib.loads(content[expected].decode())["package"]
        locked = {r["name"] + "-" + r["version"]: r for r in rows if r.get("source")}
        if len(graph["packages"]) != len(set(graph["packages"])):
            raise ValueError("Duplicate native dependency graph member")
        for key in graph["packages"]:
            package = packages[key]
            if (key not in locked or locked[key].get("checksum") != package["archive_sha256"]
                    or locked[key]["source"] != "registry+https://github.com/rust-lang/crates.io-index"):
                raise ValueError(f"Native crate checksum does not match the release lockfile: {key}")
            reached[key].add(name)
    if not packages or any(not names or names != set(packages[key]["used_by"])
                           for key, names in reached.items()):
        raise ValueError("Native package inventory differs from the recorded dependency graphs")
    return metadata


def install(bundle: Path, *, version: str, source_revision: str,
            source: Path = NOTICES) -> Path:
    """Verify the complete inventory before creating a new native notice folder."""
    metadata = verify(source, version=version, source_revision=source_revision)
    manifest = json.loads(regular_bytes(bundle / "build-manifest.json"))
    if (manifest.get("version") != version or manifest.get("source_revision") != source_revision
            or manifest.get("source_dirty") is not False
            or manifest.get("platform") != "linux" or manifest.get("architecture") != "x86_64"):
        raise ValueError("Bundle identity differs from the native notice source inventory")
    destination = bundle / "Resources/LightTable/licenses/native-rust"
    if destination.exists() or destination.is_symlink():
        raise ValueError("Native notice destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".native-notices-", dir=destination.parent) as temporary:
        staged = Path(temporary) / "native-rust"
        staged.mkdir()
        for row in metadata["files"]:
            data = regular_bytes(source / "objects" / row["sha256"])
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError("Native notice changed after verification")
            target = staged / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o644)
        (staged / "provenance.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
        staged.rename(destination)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, help="Install verified notices into this extracted release bundle")
    parser.add_argument("--version", default=VERSION)
    parser.add_argument("--source-revision", default=SOURCE_REVISION)
    args = parser.parse_args()
    try:
        metadata = verify(version=args.version, source_revision=args.source_revision)
        if args.bundle:
            print(install(args.bundle, version=args.version, source_revision=args.source_revision))
        print(f"Verified {len(metadata['packages'])} native crates and {len(metadata['files'])} retained files")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"LightTable: {error}\n")


if __name__ == "__main__":
    main()
