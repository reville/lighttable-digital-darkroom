# SPDX-License-Identifier: GPL-3.0-only
"""Shared identity checks for release operator result files (no network calls)."""
from __future__ import annotations

import json
from pathlib import Path
import re
import release_process as release

PROOF_FLAGS = ('applied', 'public_bytes_verified', 'published_release', 'receipts_verified')


def base(version):
    return version.split('-beta.', 1)[0]


def public(result):
    return all(result.get(key) is True for key in PROOF_FLAGS)


def validate_promotion(result, version, source=None, require_public=False):
    manifest = release.validate_manifest(result.get('manifest', {}))
    platform = result.get('platform')
    release.require(platform in release.PLATFORMS and set(manifest['platforms']) == {platform}, 'Promotion must contain exactly its selected platform')
    release.require(result.get('version') == manifest['version'] and base(manifest['version']) == base(version), 'Promotion version mismatch')
    revision = manifest['source_revision']
    release.require(result.get('source_revision') == revision and (source is None or revision == source), 'Promotion source mismatch')
    tag = result.get('tag')
    release.require(isinstance(tag, str) and tag == manifest.get('tag', 'v' + manifest['version']), 'Promotion tag mismatch')
    entry = manifest['platforms'][platform]
    release.require(entry['version'] == manifest['version'], 'Promotion platform version mismatch')
    release.require(str(result.get('build_run_id', '')).isdigit() and int(result['build_run_id']) > 0, 'Promotion build run ID missing')
    release.require(str(entry.get('build_run_id')) == str(result['build_run_id']), 'Promotion build identity mismatch')
    if public(result) or require_public:
        release.require(public(result), 'Completed public promotion evidence required')
        release.require(result.get('published_at') and entry['artifacts'], 'Public promotion timestamp/artifacts missing')
        release.require(not release.promotion_gates(platform, entry), 'Promotion still has validation/signing gates')
    return result


def load_promotions(paths, version, source=None, require_public=False):
    results = {}
    for value in paths:
        path = Path(value)
        result = validate_promotion(json.loads(path.read_text()), version, source, require_public)
        source = source or result['source_revision']
        platform = result['platform']
        release.require(platform not in results, f'Duplicate promotion for {platform}')
        results[platform] = dict(result, _path=str(path))
    return results


def distribution_outcomes(document, version, source):
    if not document:
        return {}
    release.require(document.get('schema') == 1 and document.get('version') == base(version)
                    and document.get('source_revision') == source, 'Distribution evidence identity mismatch')
    outcomes = {}
    for outcome in document.get('outcomes', []):
        channel = outcome.get('channel')
        release.require(channel in ('homebrew', 'scoop', 'npm') and channel not in outcomes, 'Invalid/duplicate distribution channel')
        state = outcome.get('state')
        release.require(state in ('staged', 'dispatched', 'published', 'blocked', 'failed', 'unverified'), 'Invalid distribution state')
        if state == 'published':
            release.require(outcome.get('verified') is True and isinstance(outcome.get('evidence'), dict)
                            and outcome['evidence'], 'Published channel lacks verification evidence')
        outcomes[channel] = outcome
    return outcomes
