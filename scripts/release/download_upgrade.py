#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Download exact public Linux packages, pinned to GitHub's published SHA-256."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

REPO = 'reville/lighttable-digital-darkroom'
VERSION = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')

def download(version, directory):
    if not VERSION.fullmatch(version): raise ValueError('Expected a stable version')
    release = json.loads(subprocess.check_output(['gh', 'api', f'repos/{REPO}/releases/tags/v{version}']))
    if release['draft'] or release['prerelease']: raise ValueError('Expected a public stable release')
    name = f'LightTable-{version}-linux-x86_64.tar.gz'
    assets = [a for a in release['assets'] if a['name'] == name]
    if len(assets) != 1: raise ValueError('Expected one Linux archive')
    item = assets[0]
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', item.get('digest', '')): raise ValueError('Published asset has no immutable SHA-256')
    url = f'https://github.com/{REPO}/releases/download/v{version}/{name}'
    if item['browser_download_url'] != url: raise ValueError('Unexpected release asset URL')
    directory.mkdir(exist_ok=True)
    path = directory/name
    subprocess.run(['curl', '--fail', '--location', '--proto', '=https', '--proto-redir', '=https', '--retry', '2', '--max-time', '240', '--output', str(path), url], check=True)
    with path.open('rb') as source: digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if path.stat().st_size != item['size'] or 'sha256:'+digest != item['digest']: raise ValueError('Downloaded public bytes do not match GitHub digest/size')
    return dict(version=version, name=name, url=url, bytes=item['size'], sha256=digest)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',required=True);parser.add_argument('--target',required=True)
    args=parser.parse_args()
    for v in (args.baseline,args.target):
        if not VERSION.fullmatch(v): parser.error('Stable versions required')
    if tuple(map(int,args.baseline.split('.'))) >= tuple(map(int,args.target.split('.'))): parser.error('Target must be newer than baseline')
    records=[download(args.baseline,Path('baseline')),download(args.target,Path('candidate'))]
    evidence=Path('evidence');evidence.mkdir(exist_ok=True)
    (evidence/'public-downloads.json').write_text(json.dumps(records,indent=2)+'\n')
    subprocess.run(['curl','--fail','--location','--proto','=https','--proto-redir','=https','--retry','2','--max-time','30','--output',str(evidence/'linux-x86_64.json'),f'https://github.com/{REPO}/releases/download/desktop-updates/linux-x86_64.json'],check=True)

if __name__=='__main__':main()
