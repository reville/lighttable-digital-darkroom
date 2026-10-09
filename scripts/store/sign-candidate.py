# SPDX-License-Identifier: GPL-3.0-only
"""Ad-hoc sandbox seal for local Store acceptance, never Store distribution.

Requires a fully assembled Store bundle. Creates no credentials or provisioning.
"""
from pathlib import Path
import plistlib
import subprocess
import sys
ROOT = Path(__file__).resolve().parents[2]
def seal(app):
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if info.get('LightTableDistribution') != 'mac-app-store':
        raise ValueError('Refusing to modify a direct-download bundle')
    if any((app / 'Contents').rglob('Sparkle.framework')):
        raise ValueError('Store candidate must not contain Sparkle')
    if not (app / 'Contents/Resources/Python/bin/python3.13').is_file():
        raise ValueError('Assemble the bundled runtime before native acceptance')
    # Remove only build-copy Finder metadata, never quarantine/security attributes.
    for key in ('com.apple.FinderInfo', 'com.apple.ResourceFork'):
        subprocess.run(['xattr', '-dr', key, str(app)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for path in sorted((app / 'Contents').rglob('*'), reverse=True):
        if not path.is_file() or path.is_symlink() or path == app / 'Contents/MacOS/LightTable':
            continue
        with path.open('rb') as stream:
            magic = stream.read(4)
        if magic not in {b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xfe\xed\xfa\xce', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca', b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'}:
            continue
        kind = subprocess.check_output(['file', '-b', str(path)], text=True, errors='replace')
        if 'Mach-O' not in kind:
            continue
        args = ['codesign', '--force', '--sign', '-']
        if 'executable' in kind:
            entitlement_file = ('python.entitlements' if path == app / 'Contents/Resources/Python/bin/python3.13'
                                else 'helper.entitlements')
            args += ['--entitlements', str(ROOT / 'release/store' / entitlement_file)]
        subprocess.run(args + [str(path)], check=True)
    cli = app / 'Contents/MacOS/lighttable-cli'
    if cli.is_file():
        subprocess.run(['codesign', '--force', '--sign', '-', str(cli)], check=True)
    subprocess.run(['codesign', '--force', '--sign', '-', '--entitlements', str(ROOT / 'release/store/main.entitlements'), str(app)], check=True)
    subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)], check=True)
if __name__ == '__main__':
    seal(Path(sys.argv[1]).resolve())
