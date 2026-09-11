#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Download optional RAW test inputs from their recorded public sources."""
from __future__ import annotations

import argparse
import csv
import hashlib
from pathlib import Path
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'demo-assets/cc0-raw'
CURATED = ('01-', '19-', '03-', '09-', '05-', '17-', '11-', '14-')


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(row: dict, destination: Path) -> Path:
    name = row['filename']
    if Path(name).name != name or name in ('.', '..'):
        raise ValueError('Invalid fixture filename')
    url = urllib.parse.urlparse(row['source_url'])
    if url.scheme != 'https' or url.netloc != 'raw.pixls.us' or not url.path.startswith('/getfile.php/'):
        raise ValueError(f'{name} requires retrieval from its documented source page; no direct URL is recorded')
    target = destination / name
    if target.is_file() and sha256(target) == row['sha256']:
        return target
    destination.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination, delete=False) as output:
            staged = Path(output.name)
            with urllib.request.urlopen(row['source_url'], timeout=120) as response:
                size = 0
                for chunk in iter(lambda: response.read(1024 * 1024), b''):
                    size += len(chunk)
                    if size > int(row['bytes']):
                        raise ValueError(f'{name}: source exceeds recorded size')
                    output.write(chunk)
        if staged.stat().st_size != int(row['bytes']) or sha256(staged) != row['sha256']:
            raise ValueError(f'{name}: source checksum or size changed')
        staged.replace(target)
        return target
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--all-direct', action='store_true', help='download all 20 camera samples with recorded direct URLs')
    parser.add_argument('--file', action='append', default=[], help='exact filename from manifest.tsv; repeat as needed')
    args = parser.parse_args()
    with (CORPUS/'manifest.tsv').open() as stream:
        rows = list(csv.DictReader(stream, delimiter='\t'))
    names = {row['filename'] for row in rows}
    unknown = set(args.file) - names
    if unknown:
        parser.error('Unknown fixtures: ' + ', '.join(sorted(unknown)))
    chosen = [row for row in rows if row['filename'] in args.file] if args.file else [row for row in rows if (row['source_url'].startswith('https://raw.pixls.us/getfile.php/') if args.all_direct else row['filename'].startswith(CURATED))]
    for row in chosen:
        print(fetch(row, CORPUS/'files').name)


if __name__ == '__main__':
    main()
