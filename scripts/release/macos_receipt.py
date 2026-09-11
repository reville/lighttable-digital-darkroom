#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Record native/signing evidence for the final Mac candidate, without publishing."""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import subprocess


def checked(*args):
    subprocess.run(args, check=True, capture_output=True)


def create(app, native, artifacts, channel):
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    assert info.get('LightTableSourceDirty') is False, 'Dirty source cannot be released'
    assert native.get('ok') is True and native.get('layer') == 'package' and native.get('fixtureCount', 0) > 0, 'Real native package acceptance required'
    assert native.get('machine', {}).get('machine') == 'arm64', 'Apple silicon proof required'
    checked('codesign', '--verify', '--deep', '--strict', str(app))
    stable = channel == 'stable'
    version = info['CFBundleShortVersionString']
    pattern = r'\d+\.\d+\.\d+' if stable else r'\d+\.\d+\.\d+-beta\.[1-9]\d*'
    assert re.fullmatch(pattern, version), 'Channel does not match embedded version'
    assert re.fullmatch(r'[a-f0-9]{40}', info['LightTableSourceRevision']), 'Exact source required'
    if stable:
        checked('xcrun', 'stapler', 'validate', str(app))
        checked('spctl', '--assess', '--type', 'execute', str(app))
    else:
        assert info.get('SUEnableAutomaticChecks') is False, 'Manual beta must disable automatic update checks'
    records = []
    for path in artifacts:
        assert path.is_file() and not path.is_symlink(), 'Missing artifact'
        if stable and path.suffix == '.dmg':
            checked('codesign', '--verify', '--strict', str(path))
            checked('xcrun', 'stapler', 'validate', str(path))
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
        records.append(dict(name=path.name, bytes=path.stat().st_size, sha256=digest))
    assert any(item['name'].endswith('.dmg') for item in records), 'DMG required'
    return dict(schema_version=1, source_revision=info['LightTableSourceRevision'], source_dirty=False,
                version=info['CFBundleShortVersionString'], channel=channel, architecture='arm64',
                minimum_os=info['LSMinimumSystemVersion'], update_public_key=info.get('SUPublicEDKey'), signing='developer-id-notarized' if stable else 'ad-hoc',
                native=native, artifacts=records, code_signature_verified=True, notarization_verified=stable)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', required=True, type=Path)
    parser.add_argument('--native', required=True, type=Path)
    parser.add_argument('--artifact', required=True, type=Path, action='append')
    parser.add_argument('--channel', choices=['stable', 'beta'], required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = create(args.app, json.loads(args.native.read_text()), args.artifact, args.channel)
    args.output.write_text(json.dumps(result, indent=2) + '\n')

if __name__ == '__main__': main()
