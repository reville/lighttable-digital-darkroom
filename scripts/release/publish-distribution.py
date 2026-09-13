#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Synchronize downstream package distribution channels after release promotion.

Automates external distribution updates for:
  1. Homebrew Beta Cask (reville/homebrew-lighttable)
  2. Scoop Bucket (reville/scoop-lighttable)
  3. npm CLI Package (reville/lighttable-digital-darkroom)

Usage:
    python3 scripts/release/publish-distribution.py --version 0.7.0 --all --dry-run
    python3 scripts/release/publish-distribution.py --version 0.7.0 --homebrew
    python3 scripts/release/publish-distribution.py --version 0.7.0 --scoop
    python3 scripts/release/publish-distribution.py --version 0.7.0 --npm
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "reville/lighttable-digital-darkroom"
SCOOP_REPO = "reville/scoop-lighttable"
HOMEBREW_REPO = "reville/homebrew-lighttable"


def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def find_homebrew_tap() -> Path | None:
    candidates = [
        ROOT.parent / "homebrew-lighttable",
        ROOT.parent.parent / "homebrew-lighttable",
        Path.home() / "CODING/lighttable-dev/homebrew-lighttable",
        Path.home() / "CODING/homebrew-lighttable",
    ]
    for c in candidates:
        if (c / "Casks").is_dir():
            return c.resolve()
    return None


def get_release_asset_info(tag: str, asset_name: str) -> dict | None:
    try:
        output = gh("release", "view", tag, "--repo", REPOSITORY, "--json", "assets")
        data = json.loads(output)
        for asset in data.get("assets", []):
            if asset.get("name") == asset_name:
                return asset
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------
# Homebrew Beta Cask Sync
# ----------------------------------------------------------------------
def sync_homebrew(version: str, dry_run: bool = False, push: bool = False) -> bool:
    print(f"\n--- [1/3] Homebrew Beta Cask Sync (Version: {version}) ---")
    tap_dir = find_homebrew_tap()
    if not tap_dir:
        print(f"✗ Homebrew tap directory 'homebrew-lighttable' not found.")
        return False

    cask_path = tap_dir / "Casks/lighttable@beta.rb"
    if not cask_path.is_file():
        print(f"✗ Cask file {cask_path} not found.")
        return False

    # Find DMG asset on macOS beta tag
    mac_tag = f"macos-v{version}" if "-beta." in version else f"v{version}"
    dmg_name = f"LightTable-{version}-macos-arm64.dmg"
    asset_info = get_release_asset_info(mac_tag, dmg_name)

    if not asset_info:
        print(f"! Asset '{dmg_name}' not found on release tag '{mac_tag}'.")
        print("  Checking if DMG is available locally or on v{version}...")
        asset_info = get_release_asset_info(f"v{version}", dmg_name)

    # If we have the release, download or query hash
    dmg_hash = None
    if asset_info and asset_info.get("digest"):
        digest = asset_info["digest"]
        if digest.startswith("sha256:"):
            dmg_hash = digest[7:]

    if not dmg_hash:
        # Download candidate DMG to temporary directory to compute hash
        if dry_run:
            dmg_hash = "<verified-dmg-sha256>"
        else:
            with tempfile.TemporaryDirectory() as tmp:
                tmp_dmg = Path(tmp) / dmg_name
                try:
                    print(f"Downloading {dmg_name} from {mac_tag} to verify SHA-256...")
                    gh("release", "download", mac_tag, "--repo", REPOSITORY, "--pattern", dmg_name, "--dir", tmp)
                    if tmp_dmg.is_file():
                        dmg_hash = sha256_file(tmp_dmg)
                except Exception as e:
                    print(f"✗ Could not download {dmg_name}: {e}")
                    return False

    print(f"Verified DMG SHA-256: {dmg_hash}")

    # Generate cask content
    cask_content = f"""cask "lighttable@beta" do
  version "{version}"
  sha256 "{dmg_hash}"

  url "https://github.com/reville/lighttable-digital-darkroom/releases/download/{mac_tag}/LightTable-#{{version}}-macos-arm64.dmg"
  name "LightTable Beta"
  desc "Digital darkroom for RAW photography and physical film simulation"
  homepage "https://lighttable.app/"

  livecheck do
    skip "Beta versions are updated after release verification"
  end

  conflicts_with cask: "reville/lighttable/lighttable"
  depends_on arch: :arm64
  depends_on macos: :sonoma

  app "LightTable.app"
  binary "#{{appdir}}/LightTable.app/Contents/MacOS/lighttable-cli", target: "lighttable"

  caveats <<~EOS
    This beta is not signed with an Apple Developer ID or notarized by Apple.
    macOS may block the first launch. After trying to open LightTable, use
    System Settings > Privacy & Security > Open Anyway for this app.
    See Apple's instructions: https://support.apple.com/en-us/102445

    This cask preserves macOS quarantine and does not disable Gatekeeper.
    Managed Macs may prevent opening this beta. In-app updates are not enabled;
    use Homebrew to upgrade when a verified beta is added to this tap.
  EOS
end
"""

    if dry_run:
        print(f"[DRY RUN] Would write to {cask_path}:\n")
        print(cask_content)
        print("[DRY RUN] Would validate with 'ruby -c' and commit.")
        return True

    cask_path.write_text(cask_content)
    # Validate with ruby -c
    try:
        subprocess.check_call(["ruby", "-c", str(cask_path)])
        print(f"✓ Cask syntax verified with 'ruby -c'")
    except Exception as e:
        print(f"✗ Ruby syntax check failed: {e}")
        return False

    if push:
        try:
            subprocess.check_call(["git", "add", "Casks/lighttable@beta.rb"], cwd=tap_dir)
            subprocess.check_call(["git", "commit", "-m", f"Update LightTable Beta to {version}"], cwd=tap_dir)
            subprocess.check_call(["git", "push", "origin", "main"], cwd=tap_dir)
            print("✓ Committed and pushed cask update to homebrew-lighttable")
        except Exception as e:
            print(f"! Failed to commit/push to homebrew-lighttable: {e}")
            return False
    else:
        print(f"✓ Updated {cask_path}. Pass --push to commit and push automatically.")

    return True


# ----------------------------------------------------------------------
# Scoop Sync
# ----------------------------------------------------------------------
def sync_scoop(dry_run: bool = False) -> bool:
    print(f"\n--- [2/3] Scoop Bucket Sync ---")
    if dry_run:
        print(f"[DRY RUN] Would dispatch workflow: gh workflow run update.yml --repo {SCOOP_REPO}")
        return True

    try:
        gh("workflow", "run", "update.yml", "--repo", SCOOP_REPO)
        print(f"✓ Dispatched 'update.yml' on {SCOOP_REPO}")
        return True
    except Exception as e:
        print(f"✗ Failed to dispatch Scoop update: {e}")
        return False


# ----------------------------------------------------------------------
# npm Release Package & Checksum Sync
# ----------------------------------------------------------------------
def sync_npm(version: str, dry_run: bool = False, dispatch: bool = False) -> bool:
    print(f"\n--- [3/3] npm CLI Package & SHA256SUMS Staging (Version: {version}) ---")
    npm_dir = ROOT / "packaging/npm"
    tag = f"v{version}"

    if dry_run:
        print(f"[DRY RUN] Would download LightTable-{version}-windows-x64-setup.exe for npm metadata")
        print(f"[DRY RUN] Would run prepare-release.mjs for version {version}")
        print(f"[DRY RUN] Would run 'npm pack' to build lighttable-digital-darkroom-{version}.tgz")
        print(f"[DRY RUN] Would construct standard SHA256SUMS and upload both to {tag}")
        if dispatch:
            print(f"[DRY RUN] Would dispatch: gh workflow run npm-publish.yml --repo {REPOSITORY} -f version={version}")
        return True

    with tempfile.TemporaryDirectory(prefix="lighttable-npm-prep-") as tmp:
        tmp_dir = Path(tmp)
        # 1. Download Windows setup to feed prepare-release.mjs
        win_setup = f"LightTable-{version}-windows-x64-setup.exe"
        try:
            print(f"Downloading {win_setup} from {tag} for npm metadata...")
            gh("release", "download", tag, "--repo", REPOSITORY, "--pattern", win_setup, "--dir", str(tmp_dir))
        except Exception as e:
            print(f"✗ Could not download {win_setup} from release {tag}: {e}")
            return False

        # Run prepare-release.mjs
        subprocess.check_call(["node", "scripts/prepare-release.mjs", version, str(tmp_dir)], cwd=npm_dir)
        print("✓ Prepared release.json and package.json for npm")

        # Build tarball
        tarball_name = f"lighttable-digital-darkroom-{version}.tgz"
        subprocess.check_call(["npm", "pack"], cwd=npm_dir)
        source_tarball = npm_dir / tarball_name
        if not source_tarball.is_file():
            print(f"✗ npm pack did not produce {source_tarball}")
            return False
        print(f"✓ Packed {tarball_name} ({source_tarball.stat().st_size} bytes)")

        # Download all release assets to generate comprehensive SHA256SUMS
        print(f"Downloading release assets from {tag} to assemble canonical SHA256SUMS...")
        assets_dir = tmp_dir / "assets"
        assets_dir.mkdir()
        gh("release", "download", tag, "--repo", REPOSITORY, "--dir", str(assets_dir))

        # Copy npm tarball into assets dir
        dest_tarball = assets_dir / tarball_name
        dest_tarball.write_bytes(source_tarball.read_bytes())

        # Generate SHA256SUMS (excluding any existing checksum files)
        sums = []
        for file in sorted(assets_dir.iterdir()):
            if file.is_file() and not file.name.endswith(".sha256") and file.name != "SHA256SUMS":
                h = sha256_file(file)
                sums.append(f"{h}  {file.name}\n")

        sums_file = tmp_dir / "SHA256SUMS"
        sums_file.write_text("".join(sums))
        print(f"✓ Generated SHA256SUMS ({len(sums)} assets)")

        # Upload tarball and SHA256SUMS to release
        print(f"Uploading {tarball_name} and SHA256SUMS to release {tag}...")
        gh("release", "upload", tag, str(dest_tarball), str(sums_file), "--repo", REPOSITORY)
        print(f"✓ Uploaded npm package and SHA256SUMS to {tag}")

        if dispatch:
            print(f"Dispatching npm-publish.yml workflow for {version}...")
            gh("workflow", "run", "npm-publish.yml", "--repo", REPOSITORY, "-f", f"version={version}")
            print(f"✓ Dispatched npm-publish.yml for {version}")

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True, help="Release version (e.g. 0.7.0)")
    parser.add_argument("--homebrew", action="store_true", help="Sync Homebrew beta cask")
    parser.add_argument("--scoop", action="store_true", help="Dispatch Scoop update workflow")
    parser.add_argument("--npm", action="store_true", help="Prepare, pack, and upload npm package and SHA256SUMS")
    parser.add_argument("--all", action="store_true", help="Run Homebrew, Scoop, and npm sync")
    parser.add_argument("--push", action="store_true", help="Commit and push Homebrew cask to tap")
    parser.add_argument("--dispatch-npm", action="store_true", help="Dispatch npm-publish.yml after upload")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without making writes")
    args = parser.parse_args()

    if not (args.homebrew or args.scoop or args.npm or args.all):
        parser.error("Specify at least one channel (--homebrew, --scoop, --npm, or --all)")

    success = True
    if args.homebrew or args.all:
        if not sync_homebrew(args.version, dry_run=args.dry_run, push=args.push):
            success = False
    if args.scoop or args.all:
        if not sync_scoop(dry_run=args.dry_run):
            success = False
    if args.npm or args.all:
        if not sync_npm(args.version, dry_run=args.dry_run, dispatch=args.dispatch_npm):
            success = False

    print("\n" + "=" * 50)
    print("Distribution Sync: " + ("SUCCESS" if success else "FAILED"))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
