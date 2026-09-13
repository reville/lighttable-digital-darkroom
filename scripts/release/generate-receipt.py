#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Generate an offline receipt from exact promotion and distribution results.

A release manifest is descriptive metadata, not proof of publication. Missing
promotion/channel results remain unverified. This command never queries GitHub,
infers a successful dispatch, or claims website deployment from a source merge.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/release'))
import release_process as release
from release_evidence import base, distribution_outcomes, load_promotions, public, validate_promotion


def format_receipt(version, manifest, release_data=None, notes_summary='', distribution=None,
                   checked_at=None, promotions=None):
    release.validate_manifest(manifest)
    release.require(manifest['version'] == base(version), 'Aggregate manifest version mismatch')
    source = manifest['source_revision']
    promotions = promotions or {}
    for platform, result in promotions.items():
        validate_promotion(result, version, source)
        release.require(platform == result['platform'] and platform in manifest['platforms'], 'Promotion platform is absent from aggregate')
        current = manifest['platforms'][platform]
        incoming = result['manifest']['platforms'][platform]
        release.require(current['version'] == result['version']
                        and current.get('source_revision', source) == source
                        and current.get('tag', manifest.get('tag')) == result['tag'], 'Aggregate platform identity differs from promotion')
        current_assets = {asset['name']: asset for asset in current['artifacts']}
        release.require(all(current_assets.get(asset['name']) == asset for asset in incoming['artifacts']), 'Aggregate artifact differs from promotion')
    channels = distribution_outcomes(distribution, version, source)
    checked = datetime.fromisoformat(checked_at.replace('Z', '+00:00')) if checked_at else datetime.now(timezone.utc)
    release.require(checked.tzinfo is not None, 'checked-at must include a timezone')
    checked = checked.astimezone(timezone.utc)
    complete = sum(public(result) for result in promotions.values())
    lines = [f'# LightTable {version} release receipt', '',
             f'Checked: {checked.isoformat()}', f'Source: `{source}`',
             f'Public promotion proof: {complete} of {len(manifest["platforms"])} indexed platforms verified.', '']
    if notes_summary:
        lines += [notes_summary, '']
    lines += ['## Platform evidence', '',
              '| Platform | Version | Tag | State | Build / native runs | Evidence |',
              '| --- | --- | --- | --- | --- | --- |']
    for platform, entry in sorted(manifest['platforms'].items()):
        result = promotions.get(platform)
        tag = result['tag'] if result else entry.get('tag', manifest.get('tag', 'unverified'))
        state = ('published' if public(result) else 'blocked') if result else 'unverified'
        runs = []
        if result:
            runs.append(f"build {result['build_run_id']}")
            native = result['manifest']['platforms'][platform].get('native_run_id')
            if native:
                runs.append(f'native {native}')
            if result.get('promotion_run_id'):
                runs.append(f"promotion {result['promotion_run_id']}")
        lines.append(f"| {platform} | {entry['version']} | {tag} | {state} | {', '.join(runs) or 'unverified'} | {(result or {}).get('_path', 'unverified')} |")
    lines += ['', '## Artifact identities', '', '| Platform | Asset | Bytes | SHA-256 |', '| --- | --- | ---: | --- |']
    for platform, result in sorted(promotions.items()):
        for artifact in result['manifest']['platforms'][platform]['artifacts']:
            lines.append(f"| {platform} | [{artifact['name']}]({artifact['url']}) | {artifact['bytes']} | `{artifact['sha256']}` |")
    lines += ['', '## Update feeds', '']
    for platform in sorted(manifest['platforms']):
        result = promotions.get(platform)
        advanced = result and public(result) and result.get('feed_advanced') is True
        lines.append(f'- {platform}: ' + ('advanced by the verified promotion.' if advanced else 'advancement not verified by this receipt.'))
    lines += ['', '## Distribution', '']
    for channel in ('homebrew', 'scoop', 'npm'):
        outcome = channels.get(channel, {'state': 'unverified'})
        evidence = json.dumps(outcome.get('evidence', {}), sort_keys=True)
        lines.append(f"- {channel}: {outcome['state']}. Evidence: `{evidence}`.")
    lines += ['', 'Website deployment and canonical manifest synchronization: unverified by these input files.', '']
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--manifest', type=Path, default=ROOT / 'release/manifest.json')
    parser.add_argument('--promotion-result', action='append', type=Path, default=[])
    parser.add_argument('--distribution-result', type=Path)
    parser.add_argument('--checked-at')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text())
        promotions = load_promotions(args.promotion_result, args.version, manifest['source_revision'])
        distribution = json.loads(args.distribution_result.read_text()) if args.distribution_result else None
        receipt = format_receipt(args.version, manifest, promotions=promotions,
                                 distribution=distribution, checked_at=args.checked_at)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(receipt)
            print(f'Receipt written: {args.output}')
        else:
            print(receipt)
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f'Release receipt: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
