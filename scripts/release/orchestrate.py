#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Coordinate the LightTable multi-platform release pipeline across workflows.

Inspects, coordinates, and automates parameter plumbing between:
  1. Build run (release.yml)
  2. Windows VM offline client acceptance (windows-client-vm.yml)
  3. Candidate preparation (release-prepare.yml x 3 platforms)
  4. Promotion dry runs and live apply (release-promote.yml)
  5. Downstream distribution sync and receipt generation

Usage:
    python3 scripts/release/orchestrate.py --version 0.7.0 --status
    python3 scripts/release/orchestrate.py --version 0.7.0 --plan
    python3 scripts/release/orchestrate.py --version 0.7.0 --prepare
    python3 scripts/release/orchestrate.py --version 0.7.0 --promote-dry-run
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "reville/lighttable-digital-darkroom"
PLATFORMS = ["linux-x86_64", "windows-x64", "macos-arm64"]


def gh(*args: str) -> str:
    try:
        return subprocess.check_output(["gh", *args], text=True)
    except Exception as e:
        return ""


def get_workflow_runs(workflow_file: str, limit: int = 20) -> list[dict]:
    raw = gh("api", f"repos/{REPOSITORY}/actions/workflows/{workflow_file}/runs?per_page={limit}")
    if raw:
        try:
            return json.loads(raw).get("workflow_runs", [])
        except Exception:
            pass
    return []


def find_build_run(version: str) -> dict | None:
    runs = get_workflow_runs("release.yml", limit=25)
    tag = f"v{version}"
    for run in runs:
        # Check by head_branch/tag or display title
        if run.get("head_branch") == tag or f"v{version}" in run.get("display_title", ""):
            return run
        # Check inputs
        display = run.get("display_title", "")
        if version in display:
            return run
    return None


def find_vm_acceptance_run(source_revision: str) -> dict | None:
    runs = get_workflow_runs("windows-client-vm.yml", limit=20)
    for run in runs:
        if run.get("head_sha") == source_revision:
            return run
    return None


def find_prepare_runs(version: str, build_run_id: str) -> dict[str, dict]:
    runs = get_workflow_runs("release-prepare.yml", limit=30)
    found = {}
    for run in runs:
        # Look for artifacts of the form LightTable-<platform>-promotion
        raw_artifacts = gh("api", f"repos/{REPOSITORY}/actions/runs/{run['id']}/artifacts")
        if raw_artifacts:
            try:
                artifacts = json.loads(raw_artifacts).get("artifacts", [])
                for art in artifacts:
                    name = art.get("name", "")
                    for plat in PLATFORMS:
                        if name == f"LightTable-{plat}-promotion" and plat not in found:
                            found[plat] = run
            except Exception:
                pass
    return found


def print_status_table(version: str) -> dict:
    print(f"=== LightTable Release Orchestration Status (v{version}) ===\n")

    # 1. Build run
    build_run = find_build_run(version)
    if build_run:
        b_id = build_run["id"]
        b_status = build_run.get("status", "")
        b_conclusion = build_run.get("conclusion", "")
        source_sha = build_run.get("head_sha", "")
        print(f"1. Build Run (release.yml):")
        print(f"   ID:         {b_id}")
        print(f"   Status:     {b_status} ({b_conclusion or 'in progress'})")
        print(f"   Source SHA: {source_sha}")
        print(f"   URL:        https://github.com/{REPOSITORY}/actions/runs/{b_id}")
    else:
        print(f"1. Build Run (release.yml): NOT FOUND for tag v{version}")
        source_sha = ""
        b_id = None

    # 2. Windows VM run
    vm_run = find_vm_acceptance_run(source_sha) if source_sha else None
    print(f"\n2. Windows Client VM (windows-client-vm.yml):")
    if vm_run:
        vm_id = vm_run["id"]
        print(f"   ID:         {vm_id}")
        print(f"   Status:     {vm_run.get('status')} ({vm_run.get('conclusion') or 'in progress'})")
        print(f"   URL:        https://github.com/{REPOSITORY}/actions/runs/{vm_id}")
    else:
        print(f"   Status:     NOT DISPATCHED or not found for {source_sha[:7] if source_sha else 'unknown'}")
        vm_id = None

    # 3. Preparation runs
    prep_runs = find_prepare_runs(version, str(b_id)) if b_id else {}
    print(f"\n3. Candidate Preparation (release-prepare.yml):")
    for plat in PLATFORMS:
        if plat in prep_runs:
            p_run = prep_runs[plat]
            print(f"   - {plat:15} Run {p_run['id']} ({p_run.get('status')}, {p_run.get('conclusion')})")
        else:
            print(f"   - {plat:15} PENDING")

    state = {
        "version": version,
        "build_run": build_run,
        "source_sha": source_sha,
        "vm_run": vm_run,
        "prepare_runs": prep_runs,
    }
    return state


def print_plan(state: dict) -> None:
    version = state["version"]
    b_run = state.get("build_run")
    if not b_run:
        print(f"\nNext Action: Dispatch release.yml build for v{version}:")
        print(f"  gh workflow run release.yml --repo {REPOSITORY} -f version={version} -f platforms=all")
        return

    b_id = b_run["id"]
    source_sha = state.get("source_sha", "")
    vm_run = state.get("vm_run")
    prep_runs = state.get("prepare_runs", {})

    print("\n--- Recommended Next Steps ---")

    # If VM acceptance not run
    if not vm_run:
        print(f"\n[Step A] Dispatch Windows Client VM acceptance:")
        print(f"  gh workflow run windows-client-vm.yml --repo {REPOSITORY} -f build_run_id={b_id} -f source_revision={source_sha}")

    # Prepare missing platforms
    missing_preps = [p for p in PLATFORMS if p not in prep_runs]
    if missing_preps:
        print(f"\n[Step B] Dispatch candidate preparation for missing platforms:")
        for plat in missing_preps:
            native_arg = f" -f native_run_id={vm_run['id']}" if vm_run and plat == "windows-x64" else ""
            print(f"  gh workflow run release-prepare.yml --repo {REPOSITORY} -f platform={plat} -f version={version} -f build_run_id={b_id} -f source_revision={source_sha}{native_arg}")

    # Promotion dry runs
    ready_preps = [p for p in PLATFORMS if p in prep_runs and prep_runs[p].get("conclusion") == "success"]
    if ready_preps:
        print(f"\n[Step C] Run promotion DRY-RUN for prepared platforms:")
        for plat in ready_preps:
            p_id = prep_runs[plat]["id"]
            native_arg = f" -f native_run_id={vm_run['id']}" if vm_run and plat == "windows-x64" else ""
            print(f"  gh workflow run release-promote.yml --repo {REPOSITORY} -f platform={plat} -f version={version} -f source_revision={source_sha} -f build_run_id={b_id} -f preparation_run_id={p_id}{native_arg} -f dry_run=true")

        print(f"\n[Step D] Once dry runs succeed, run LIVE PROMOTION:")
        for plat in ready_preps:
            p_id = prep_runs[plat]["id"]
            native_arg = f" -f native_run_id={vm_run['id']}" if vm_run and plat == "windows-x64" else ""
            advance_feed = "true" if plat != "macos-arm64" else "false"
            print(f"  gh workflow run release-promote.yml --repo {REPOSITORY} -f platform={plat} -f version={version} -f source_revision={source_sha} -f build_run_id={b_id} -f preparation_run_id={p_id}{native_arg} -f dry_run=false -f make_public=true -f advance_feed={advance_feed}")

        print(f"\n[Step E] Downstream package distribution & receipt generation:")
        print(f"  python3 scripts/release/publish-distribution.py --version {version} --all")
        print(f"  python3 scripts/release/generate-receipt.py --version {version} --output docs/releases/{version}.md")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", required=True, help="Release version (e.g. 0.7.0)")
    parser.add_argument("--status", action="store_true", help="Display status of runs across workflows")
    parser.add_argument("--plan", action="store_true", help="Display recommended next commands")
    args = parser.parse_args()

    state = print_status_table(args.version)
    if args.plan or not args.status:
        print_plan(state)

    return 0


if __name__ == "__main__":
    sys.exit(main())
