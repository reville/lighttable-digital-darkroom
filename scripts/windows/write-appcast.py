#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Write a Windows appcast after winsparkle-tool verified the final EXE."""
from __future__ import annotations

import argparse
import base64
from pathlib import Path
import re
import xml.etree.ElementTree as ET

SPARKLE = "http://www.andymatuschak.org/xml-namespaces/sparkle"
REPOSITORY = "https://github.com/reville/lighttable-digital-darkroom"
ET.register_namespace("sparkle", SPARKLE)


def appcast(version: str, installer: Path, signature: str) -> bytes:
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise ValueError("Windows update feeds require a stable semantic version")
    if len(base64.b64decode(signature, validate=True)) != 64:
        raise ValueError("A verified Ed25519 signature is required")
    if installer.name != f"LightTable-{version}-windows-x64-setup.exe":
        raise ValueError("The installer name does not match this update")
    length = installer.stat().st_size
    if length == 0:
        raise ValueError("The signed installer is empty")
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = "LightTable for Windows"
    ET.SubElement(channel, "link").text = REPOSITORY
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = f"LightTable {version}"
    ET.SubElement(item, f"{{{SPARKLE}}}releaseNotesLink").text = f"{REPOSITORY}/releases/tag/v{version}"
    ET.SubElement(item, "enclosure", {
        "url": f"{REPOSITORY}/releases/download/v{version}/{installer.name}",
        "length": str(length),
        "type": "application/octet-stream",
        f"{{{SPARKLE}}}version": version,
        f"{{{SPARKLE}}}shortVersionString": version,
        f"{{{SPARKLE}}}os": "windows",
        f"{{{SPARKLE}}}edSignature": signature,
    })
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("installer", type=Path)
    parser.add_argument("signature")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_bytes(appcast(args.version, args.installer, args.signature))
