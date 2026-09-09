#!/usr/bin/env python3
"""Inventory exact runtime sources; this is not a completed offline build closure."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / "packaging/flatpak/python-source-audit.json"
UPSTREAM = {
    "rawpy": {"type": "git", "url": "https://github.com/letmaik/rawpy.git",
              "commit": "a39c2e7a44911889c3360891012f862f904ba551", "tag": "v0.27.1"},
    "lensfunpy": {"type": "git", "url": "https://github.com/letmaik/lensfunpy.git",
                  "commit": "458a24475ae03ec52f0fac4fea786120ef5a503f", "tag": "v1.18.0"},
    "opencv-python-headless": {"type": "git", "url": "https://github.com/opencv/opencv-python.git",
                               "commit": "b83046cda41133f1bf2e73e99dba16a1248f103a", "tag": "93"},
}


def requirements() -> list[tuple[str, str]]:
    return [tuple(line.split("==")) for line in (ROOT / "requirements-runtime.lock").read_text().splitlines()
            if line.strip() and not line.startswith("#")]


def inspect(requirement: tuple[str, str]) -> dict:
    name, version = requirement
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as response:
        metadata = json.load(response)
    sources = [{"type": "file", "url": item["url"], "sha256": item["digests"]["sha256"]}
               for item in metadata["urls"] if item["packagetype"] == "sdist"]
    row = {"name": name, "version": version, "sources": sources,
           "requires_python": metadata["info"].get("requires_python"),
           "offline_recipe_verified": False}
    if not sources and name in UPSTREAM:
        row["sources"] = [UPSTREAM[name]]
        row["note"] = "No PyPI sdist at this pin; upstream Git source and its submodules require a native build recipe."
    if not row["sources"]:
        row["blocker"] = "No pinned source distribution or upstream source mapping"
    return row


def check(document: dict) -> None:
    assert document["requirements_sha256"] == hashlib.sha256((ROOT / "requirements-runtime.lock").read_bytes()).hexdigest(), "Runtime lock changed; refresh source audit"
    assert [(row["name"], row["version"]) for row in document["packages"]] == requirements()
    assert document["source_build_ready"] is False, "A source inventory alone does not establish a build"
    for row in document["packages"]:
        assert row["sources"], row["name"]
        for source in row["sources"]:
            assert source.get("sha256") or source.get("commit"), source
            assert ".whl" not in source["url"], source


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Query PyPI and update the checked-in source inventory")
    args = parser.parse_args()
    if args.refresh:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as workers:
            packages = list(workers.map(inspect, requirements()))
        document = {"format": 1, "source_build_ready": False,
                    "requirements_sha256": hashlib.sha256((ROOT / "requirements-runtime.lock").read_bytes()).hexdigest(),
                    "packages": packages}
        TARGET.write_text(json.dumps(document, indent=2) + "\n")
    document = json.loads(TARGET.read_text())
    check(document)
    print(f"{len(document['packages'])} exact runtime requirements have pinned source locations; offline build closure remains pending")


if __name__ == "__main__":
    main()
