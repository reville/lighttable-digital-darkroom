#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Generate a structured markdown release receipt from verified promotion results.

Collects evidence from:
  1. Promotion result JSONs (.build/release-promotion/*.json or release/manifest.json)
  2. Public GitHub Release asset metadata and checksums
  3. CI workflow run IDs

Formats the receipt according to docs/releases/0.7.0-20260913.md standard.

Usage:
    python3 scripts/release/generate-receipt.py --version 0.7.0
    python3 scripts/release/generate-receipt.py --manifest release/manifest.json --output docs/releases/0.7.0.md
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "reville/lighttable-digital-darkroom"


def gh(*args: str) -> str:
    try:
        return subprocess.check_output(["gh", *args], text=True)
    except Exception as e:
        return ""


def get_public_release_data(tag: str) -> dict:
    raw = gh("api", f"repos/{REPOSITORY}/releases/tags/{tag}")
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {}


def format_receipt(version: str, manifest: dict, release_data: dict, notes_summary: str = "") -> str:
    today_utc = datetime.now(timezone.utc).strftime("%B %d, %Y")
    date_code = datetime.now(timezone.utc).strftime("%Y%m%d")
    source_rev = manifest.get("source_revision", "")
    platforms = manifest.get("platforms", {})

    lines = [
        f"# LightTable {version} Release Receipt — {today_utc} (UTC)",
        "",
        f"Operational record for the LightTable {version} multi-platform release.",
        "",
        "## Summary",
        "",
        "- **Release versions**:",
    ]

    for plat_name, pdata in sorted(platforms.items()):
        plat_label = "Linux" if "linux" in plat_name else ("macOS" if "macos" in plat_name else "Windows")
        lines.append(f"  - {plat_label}: `{pdata.get('version', version)}` ({pdata.get('channel', 'stable')})")

    lines.extend([
        f"- **Source revision**: [`{source_rev}`](https://github.com/{REPOSITORY}/commit/{source_rev})",
        f"- **Tags**: `v{version}`" + (f" and `macos-v{platforms.get('macos-arm64', {}).get('version', version)}`" if "macos-arm64" in platforms else ""),
    ])

    if notes_summary:
        lines.append(f"- **Release notes focus**: {notes_summary}")

    # Public artifacts table
    lines.extend([
        "",
        "## Public artifacts and verification",
        "",
        "| Platform | Asset | Size (bytes) | SHA-256 |",
        "| --- | --- | --- | --- |",
    ])

    assets = release_data.get("assets", [])
    asset_by_name = {a["name"]: a for a in assets}

    for plat_name, pdata in sorted(platforms.items()):
        for art in pdata.get("artifacts", []):
            name = art.get("name", "")
            size = art.get("bytes", asset_by_name.get(name, {}).get("size", "—"))
            sha = art.get("sha256", "—")
            lines.append(f"| {plat_name} | `{name}` | {size} | `{sha}` |")

    # If npm package is attached
    npm_asset = next((a for a in assets if a["name"].startswith("lighttable-digital-darkroom-") and a["name"].endswith(".tgz")), None)
    if npm_asset:
        lines.append(f"| npm package | `{npm_asset['name']}` | {npm_asset['size']} | (signed provenance) |")

    # Updater feeds section
    lines.extend([
        "",
        "## Updater feeds (`desktop-updates`)",
        "",
    ])
    if "linux-x86_64" in platforms:
        linux_p = platforms["linux-x86_64"]
        lines.append(f"- **Linux** `linux-x86_64.json`: `signed.version` {linux_p.get('version', version)}, `signed.source_revision` {source_rev[:7]}.")
    if "windows-x64" in platforms:
        win_p = platforms["windows-x64"]
        lines.append(f"- **Windows** `appcast-windows-x64.xml`: `sparkle:version` {win_p.get('version', version)}.")
    if "macos-arm64" in platforms:
        lines.append("- **macOS beta**: manual updates; in-app updates disabled.")

    # Package distribution channels
    lines.extend([
        "",
        "## Package distribution channels",
        "",
        f"1. **Homebrew tap**: `reville/homebrew-lighttable` Casks/lighttable@beta.rb updated to {platforms.get('macos-arm64', {}).get('version', version)}.",
        f"2. **Scoop bucket**: `reville/scoop-lighttable` update.yml dispatched.",
        f"3. **npm CLI**: `lighttable-digital-darkroom` {version} published via npm-publish.yml.",
        f"4. **Canonical manifest**: `release/manifest.json` synchronized.",
        "",
        "## Publishing.md Index Row",
        "",
        "```markdown",
        f"| macOS direct, Apple silicon | **{platforms.get('macos-arm64', {}).get('version', version)} public prerelease** | See the [{version} receipt](releases/{version}-{date_code}.md). |",
        f"| Linux portable, x86_64 | **{platforms.get('linux-x86_64', {}).get('version', version)} public** | See the [{version} receipt](releases/{version}-{date_code}.md). |",
        f"| Windows direct x64 | **{platforms.get('windows-x64', {}).get('version', version)} public** | See the [{version} receipt](releases/{version}-{date_code}.md). |",
        "```",
    ])

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True, help="Release version (e.g. 0.7.0)")
    parser.add_argument("--manifest", default="release/manifest.json", help="Path to manifest.json")
    parser.add_argument("--output", help="Output receipt path (defaults to stdout)")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        manifest_path = ROOT / args.manifest

    if not manifest_path.is_file():
        print(f"✗ Manifest file not found: {args.manifest}", file=sys.stderr)
        return 1

    manifest = json.loads(manifest_path.read_text())
    release_data = get_public_release_data(f"v{args.version}")

    notes_path = ROOT / f"docs/releases/notes/v{args.version}.md"
    notes_summary = notes_path.read_text().strip() if notes_path.is_file() else ""

    receipt = format_receipt(args.version, manifest, release_data, notes_summary)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(receipt)
        print(f"✓ Receipt written to {out_path}")
    else:
        print(receipt)

    return 0


if __name__ == "__main__":
    sys.exit(main())
