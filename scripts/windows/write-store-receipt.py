# SPDX-License-Identifier: GPL-3.0-only
"""Record exact candidate artifacts after signature checks; no publishing or certification claims."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_receipt(output: Path, input_manifest: Path, revision: str) -> Path:
    archives = list(output.glob('LightTable-*-windows-x64.zip'))
    installers = list(output.glob('LightTable-*-windows-x64-setup.exe'))
    if len(archives) != 1 or len(installers) != 1:
        raise ValueError('Store output must contain exactly one archive and one installer')
    archive, installer = archives[0], installers[0]
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read('LightTable/build-manifest.json').decode('utf-8-sig'))
    prerequisite = json.loads(input_manifest.read_text(encoding='utf-8-sig'))
    signatures_path = output / 'windows-signatures.json'
    signatures = json.loads(signatures_path.read_text(encoding='utf-8-sig'))
    pe_path = output / 'windows-store-pe-signatures.json'
    pe = json.loads(pe_path.read_text(encoding='utf-8-sig'))
    if (manifest.get('source_revision') != revision or not manifest.get('store_candidate')
            or not manifest.get('authenticode_signed')
            or manifest.get('webview2_offline_sha256') != prerequisite['sha256']
            or signatures.get('source_revision') != revision
            or signatures.get('archive_sha256') != sha256(archive)):
        raise ValueError('Candidate source, signatures, or pinned prerequisite evidence does not match')
    if (not pe.get('pe_count') or pe.get('invalid_count') != 0
            or len(pe.get('signatures', [])) != pe['pe_count']
            or any(item.get('status') != 'Valid' for item in pe['signatures'])
            or not any(Path(item['path'].replace('\\', '/')).name.lower() == 'uninstall.exe'
                       for item in pe['signatures'])):
        raise ValueError('A passing installed-payload PE report including the uninstaller is required')
    installer_hash = sha256(installer)
    if not any(item.get('file') == installer.name and item.get('sha256') == installer_hash
               and item.get('status') == 'Valid' for item in signatures.get('signatures', [])):
        raise ValueError('Installer signature evidence does not match the final installer')
    version = manifest['version']
    if installer.name != f'LightTable-{version}-windows-x64-setup.exe':
        raise ValueError('Installer filename and package version disagree')
    receipt = {
        'source_revision': revision,
        'version': version,
        'publisher_metadata': 'Chonkers LLC',
        'webview2_input': prerequisite,
        'artifacts': [{'file': item.name, 'bytes': item.stat().st_size, 'sha256': sha256(item)}
                      for item in (archive, installer, signatures_path, pe_path)],
        'installed_pe_count': pe['pe_count'],
        'offline_clean_machine_acceptance': 'NOT DONE: separate Windows proof required',
        'native_acceptance': 'See the Windows-native-acceptance artifact and final job result',
        'store_submission': 'Not submitted',
    }
    destination = output / 'store-candidate-receipt.json'
    destination.write_text(json.dumps(receipt, indent=2) + '\n')
    return destination


if __name__ == '__main__':
    source = Path(__file__).resolve().parents[2]
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    print(write_receipt(Path(sys.argv[1]), Path(sys.argv[2]), revision))
