#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Offline integrity checks for the generated source-build preparation."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / 'packaging/flatpak'


def main():
    spec = importlib.util.spec_from_file_location('source_manifest', ROOT / 'scripts/flatpak/source-manifest.py')
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    manifest = json.loads((PACKAGING / 'source-candidate.json').read_text())
    app = manifest['modules'][-1]
    provenance = next(s for s in app['sources'] if s.get('dest-filename') == 'source-provenance.json')
    provenance = json.loads(provenance['contents'])
    assert provenance['source_build_verified'] is False
    assert manifest['modules'][0]['name'] == 'source-closure-check'
    assert '--share=network' not in manifest.get('build-options', {}).get('build-args', [])
    count = 0
    def verify(modules):
        nonlocal count
        for module in modules:
            verify(module.get('modules', []))
            for source in module.get('sources', []):
                count += 1
                assert '.whl' not in source.get('url', ''), source
                if 'url' in source:
                    assert source.get('sha256') or source.get('commit'), source
                if source['type'] == 'file' and 'path' in source:
                    import hashlib
                    path = PACKAGING / source['path']
                    assert hashlib.sha256(path.read_bytes()).hexdigest() == source['sha256'], path
    verify(manifest['modules'])
    runtime = json.loads((PACKAGING / 'python-source-audit.json').read_text())
    names = {m['name'] for m in manifest['modules']}
    assert all('python-' + row['name'] in names for row in runtime['packages'])
    codec = next(m for m in manifest['modules'] if m['name'] == 'python-imagecodecs')
    assert any(s.get('dest-filename') == 'imagecodecs_distributor_setup.py' for s in codec['sources'])
    assert all('--offline --locked' in c for c in app['build-commands'] if 'cargo build ' in c)
    checksums = [s for s in app['sources'] if s.get('dest-filename') == '.cargo-checksum.json']
    crates = [s for s in app['sources'] if s.get('url', '').startswith('https://static.crates.io/')]
    assert len(checksums) == len(crates) > 500
    assert {s['dest'] for s in checksums} == {s['dest'] for s in crates}
    for source in checksums:
        archive = next(s for s in crates if s['dest'] == source['dest'])
        assert json.loads(source['contents'])['package'] == archive['sha256']
    # Test invalid identity rejection before a manifest can be emitted.
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / 'rejected.json'
        try:
            generator.generate('main', '0.5.0', target)
        except ValueError:
            pass
        else:
            raise AssertionError('Mutable source identity was accepted')
        assert not target.exists()
    blocked = subprocess.run(['python3', str(ROOT / 'scripts/flatpak/source-preflight.py'),
                              str(PACKAGING / 'source-status.json')], capture_output=True, text=True)
    assert blocked.returncode == 2 and 'imagecodecs-native-closure' in blocked.stdout
    with tempfile.TemporaryDirectory() as directory:
        status = json.loads((PACKAGING / 'source-status.json').read_text())
        status['unresolved'] = [item for item in status['unresolved'] if not item.get('blocks_build', True)]
        target = Path(directory) / 'pending-validation.json'
        target.write_text(json.dumps(status))
        pending = subprocess.run(['python3', str(ROOT / 'scripts/flatpak/source-preflight.py'),
                                  str(target)], capture_output=True, text=True)
        assert pending.returncode == 0 and 'native verification is still required' in pending.stdout
        assert status['source_build_verified'] is False
    print(f'PASS: {len(manifest["modules"])} modules, all 29 runtime pins, {len(crates)} Cargo source archives, '
          f'{count} pinned/inline source entries, and intentional pre-build closure rejection')


if __name__ == '__main__':
    main()
