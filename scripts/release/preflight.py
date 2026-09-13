#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Pre-release preflight verification for LightTable.

Verifies that the repository is in a valid, complete state before tagging a release:
  1. Git working tree state (clean or identified).
  2. Complete translations and help content localization.
  3. Version consistency across app_version.py, Xcode, and manifests.
  4. RENDER_CACHE_VERSION policy (warns if render cache changed but bump is only a patch).
  5. Staged human release notes file exists at docs/releases/notes/v<version>.md.

Usage:
    python3 scripts/release/preflight.py                # Check current repository state
    python3 scripts/release/preflight.py 0.7.1          # Validate readiness for 0.7.1
    python3 scripts/release/preflight.py 0.7.1 --apply  # Bump version and verify all checks
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STABLE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
RENDER_CACHE_RE = re.compile(r"^RENDER_CACHE_VERSION = (\d+)", re.MULTILINE)

# Import set-version module
SET_VERSION_SPEC = importlib.util.spec_from_file_location("set_version", ROOT / "scripts/release/set-version.py")
set_version_mod = importlib.util.module_from_spec(SET_VERSION_SPEC)
SET_VERSION_SPEC.loader.exec_module(set_version_mod)


def get_current_version() -> str:
    return set_version_mod.source_version(ROOT)


def get_latest_release_tag() -> str | None:
    try:
        tags = subprocess.check_output(
            ["git", "tag", "--list", "v*", "--sort=-version:refname"],
            cwd=ROOT, text=True
        ).splitlines()
        for tag in tags:
            clean = tag.lstrip("v")
            if STABLE.fullmatch(clean):
                return tag
    except Exception:
        pass
    return None


def get_render_cache_version(ref: str | None = None) -> int | None:
    try:
        if ref:
            content = subprocess.check_output(["git", "show", f"{ref}:server.py"], cwd=ROOT, text=True)
        else:
            content = (ROOT / "server.py").read_text()
        match = RENDER_CACHE_RE.search(content)
        if match:
            return int(match.group(1))
    except Exception:
        pass
    return None


def check_git_clean() -> tuple[bool, str]:
    try:
        status = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip()
        if not status:
            return True, "Working tree clean"
        return False, f"Working tree has uncommitted changes:\n{status}"
    except Exception as e:
        return False, f"Git status failed: {e}"


def check_localizations() -> tuple[bool, list[str]]:
    problems = []
    # Check UI localizations
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/localization.py"), "check"],
            cwd=ROOT, capture_output=True, text=True
        )
        if result.returncode != 0:
            problems.append(f"UI localization check failed:\n{result.stderr or result.stdout}")
    except Exception as e:
        problems.append(f"Unable to run localization check: {e}")

    # Check help content localizations
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/help_content.py"), "localization-check"],
            cwd=ROOT, capture_output=True, text=True
        )
        if result.returncode != 0:
            problems.append(f"Help content localization check failed:\n{result.stderr or result.stdout}")
    except Exception as e:
        problems.append(f"Unable to run help content check: {e}")

    return len(problems) == 0, problems


def check_release_notes(version: str) -> tuple[bool, str]:
    notes_file = ROOT / f"docs/releases/notes/v{version}.md"
    alt_file = ROOT / f"release/notes/v{version}.md"
    if notes_file.is_file():
        return True, str(notes_file)
    if alt_file.is_file():
        return True, str(alt_file)
    return False, (
        f"Missing staged release notes file at docs/releases/notes/v{version}.md.\n"
        f"Create this file to provide a user-facing overview (e.g. key highlights or 'Existing photos will look different')."
    )


def preflight(target_version: str | None = None, apply_bump: bool = False) -> int:
    current_version = get_current_version()
    effective_version = target_version or current_version
    print(f"=== LightTable Release Preflight (Target Version: {effective_version}) ===")

    all_passed = True

    # 1. Check Git status
    is_clean, git_msg = check_git_clean()
    if is_clean:
        print("✓ Git working tree: Clean")
    else:
        print(f"! Git working tree: {git_msg}")
        if not apply_bump:
            all_passed = False

    # 2. Version validation
    if target_version:
        if not STABLE.fullmatch(target_version):
            print(f"✗ Target version '{target_version}' is not valid semantic version (MAJOR.MINOR.PATCH)")
            return 1
        curr_key = set_version_mod.version_key(current_version)
        target_key = set_version_mod.version_key(target_version)
        if target_key < curr_key:
            print(f"✗ Refusing to lower version from {current_version} to {target_version}")
            return 1
        if apply_bump and target_version != current_version:
            print(f"Applying version bump: {current_version} -> {target_version}...")
            set_version_mod.set_version(target_version, ROOT)
            effective_version = target_version

    version_problems = set_version_mod.problems(ROOT)
    if version_problems:
        print("✗ Version consistency problems:")
        for p in version_problems:
            print(f"  - {p}")
        all_passed = False
    else:
        print(f"✓ Version consistency: Valid ({effective_version})")

    # 3. RENDER_CACHE_VERSION check
    latest_tag = get_latest_release_tag()
    current_cache = get_render_cache_version()
    if latest_tag and current_cache is not None:
        tag_cache = get_render_cache_version(latest_tag)
        if tag_cache is not None and current_cache != tag_cache:
            # Render cache bumped
            tag_version = latest_tag.lstrip("v")
            tag_key = set_version_mod.version_key(tag_version)
            eff_key = set_version_mod.version_key(effective_version)
            if eff_key[0] == tag_key[0] and eff_key[1] == tag_key[1]:
                print(
                    f"! RENDER_CACHE_VERSION changed ({tag_cache} -> {current_cache}) since {latest_tag}.\n"
                    f"  Per publishing guidelines, changes that alter rendered output require a minor or major\n"
                    f"  version bump (e.g. {tag_key[0]}.{tag_key[1] + 1}.0), not a patch bump ({effective_version})."
                )
                all_passed = False
            else:
                print(f"✓ RENDER_CACHE_VERSION policy: {tag_cache} -> {current_cache} accompanied by minor/major bump")
        else:
            print(f"✓ RENDER_CACHE_VERSION policy: Unchanged ({current_cache})")
    else:
        print(f"✓ RENDER_CACHE_VERSION: {current_cache}")

    # 4. Localization check
    loc_ok, loc_problems = check_localizations()
    if loc_ok:
        print("✓ Localizations: All UI and Help translations complete")
    else:
        print("✗ Localization errors detected:")
        for p in loc_problems:
            print(f"  {p}")
        print("  Hint: Run 'python3 scripts/translate-locales.py' before releasing.")
        all_passed = False

    # 5. Staged release notes
    notes_ok, notes_msg = check_release_notes(effective_version)
    if notes_ok:
        print(f"✓ Staged release notes: Found at {notes_msg}")
    else:
        print(f"! Staged release notes: {notes_msg}")

    print("-" * 50)
    if all_passed:
        print(f"Result: PREFLIGHT PASSED for version {effective_version}")
        print("\nNext steps to proceed with release:")
        print(f"  1. git commit -am 'Release {effective_version}'")
        print(f"  2. git tag -a v{effective_version} -m 'LightTable {effective_version}'")
        if "-beta." in effective_version:
            print(f"  3. git tag -a macos-v{effective_version} -m 'LightTable macOS {effective_version}'")
        print(f"  4. git push origin main --tags")
        print(f"  5. Run scripts/release/orchestrate.py --version {effective_version}")
        return 0
    else:
        print("Result: PREFLIGHT FAILED. Resolve the issues above before building/tagging.")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("version", nargs="?", help="Target release version (e.g. 0.7.1)")
    parser.add_argument("--apply", action="store_true", help="Apply version bump if version is specified")
    args = parser.parse_args()
    return preflight(args.version, args.apply)


if __name__ == "__main__":
    sys.exit(main())
