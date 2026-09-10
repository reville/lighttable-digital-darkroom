"""Checksummed screenshot photographs, separate from the app's RAW test fixtures."""
from __future__ import annotations

import csv
import hashlib
from pathlib import Path

CC0 = "https://creativecommons.org/publicdomain/zero/1.0/"
PEXELS = "https://www.pexels.com/license/"
LICENSES = {CC0, PEXELS}


def collection(plan):
    result = plan.get("sourceCollection", {
        "name": "LightTable CC0 RAW demo collection", "license": CC0})
    if result.get("license") not in LICENSES or not result.get("name"):
        raise ValueError("Screenshot collection needs recorded source licensing")
    return result


def locations(plan, site_root, app_root=None):
    info = collection(plan)
    if "root" in info:
        paths = tuple((site_root / info[key]).resolve() for key in ("root", "manifest"))
        if not all(path.is_relative_to(site_root.resolve()) for path in paths):
            raise ValueError("Photo collection paths must stay inside the website checkout")
        return paths
    if app_root is None:
        raise ValueError("The legacy RAW plan requires an app checkout")
    base = app_root / "demo-assets/cc0-raw"
    return base / "files", base / "manifest.tsv"


def records(manifest):
    with manifest.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    result = {row["filename"]: row for row in rows}
    if len(result) != len(rows) or not result:
        raise ValueError("Photo manifest must contain unique filenames")
    return result


def verify(plan, root, sources):
    license_url = collection(plan)["license"]
    required = {shot["source"] for shot in plan["shots"]}
    if not required <= sources.keys():
        raise ValueError("A screenshot source is missing from the photo manifest")
    # All recorded photos appear in the filmstrip, including the unused B&W ones.
    for name, record in sources.items():
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError("Unsafe source filename")
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Photo source is outside its collection")
        if record.get("license_url") != license_url:
            raise ValueError(f"Source license differs from collection: {name}")
        if license_url == PEXELS and not record.get("source_url", "").startswith("https://www.pexels.com/photo/"):
            raise ValueError(f"Missing Pexels source URL: {name}")
        data = path.read_bytes()
        if (len(data) < 4096 or data.startswith(b"version https://git-lfs.github.com/spec/v1")
                or len(data) != int(record["bytes"])
                or hashlib.sha256(data).hexdigest() != record["sha256"]):
            raise ValueError(f"Source photo checksum differs from provenance: {name}")
    return sources
