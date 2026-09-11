#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Produce a reviewable website/index update from a verified promotion result."""
import argparse
import copy
import json
from pathlib import Path
import release_process as release


def merge(index, result):
    release.validate_manifest(index)
    release.require(result.get('applied') is True and result.get('public_bytes_verified') is True
                    and result.get('published_release') is True and result.get('receipts_verified') is True,
                    'Only completed, independently verified public promotion may update the index')
    incoming = release.validate_manifest(result['manifest'])
    platform = result['platform']
    release.require(set(incoming['platforms']) == {platform}, 'Expected one promoted platform')
    release.require(result['version'] == incoming['version'] and result['source_revision'] == incoming['source_revision'], 'Promotion receipt identity differs')
    output = copy.deepcopy(index)
    for entry in output['platforms'].values():
        entry.setdefault('source_revision', output['source_revision'])
        entry.setdefault('tag', output.get('tag', 'v'+entry['version']))
    entry = copy.deepcopy(incoming['platforms'][platform])
    entry['source_revision'] = incoming['source_revision']
    entry['tag'] = incoming.get('tag', 'v'+incoming['version'])
    entry['state'] = 'published'
    release.require(result.get('published_at'), 'Public release timestamp required')
    entry['published_at'] = result['published_at']
    if platform in output['platforms']:
        old = output['platforms'][platform]
        def key(v):
            base, _, beta = v.partition('-beta.')
            return (*map(int,base.split('.')), 0 if beta else 1, int(beta or 0))
        release.require(key(entry['version']) >= key(old['version']), 'Do not roll back an indexed platform')
        # Retain already-published additional formats (e.g. Arch) on same-source/version resumes.
        if entry['version'] == old['version'] and entry['source_revision'] == old['source_revision']:
            names = {a['name']: a for a in entry['artifacts']}
            for asset in old['artifacts']:
                if asset['name'] in names:
                    release.require(names[asset['name']] == asset, 'Indexed public asset identity differs')
                else:
                    entry['artifacts'].append(copy.deepcopy(asset))
    output['platforms'][platform] = entry
    if tuple(map(int,incoming['version'].split('-')[0].split('.'))) >= tuple(map(int,output['version'].split('-')[0].split('.'))):
        output['version'] = incoming['version'].split('-')[0]
        output['source_revision'] = incoming['source_revision']
    output.pop('tag', None)
    release.validate_manifest(output)
    return output


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--index',required=True,type=Path);p.add_argument('--promotion-result',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    a=p.parse_args();result=merge(release.read_json(a.index),release.read_json(a.promotion_result));release.write_json(a.output,result)
    print('Verified platform index prepared; review/merge it, then synchronize and verify the website deployment.')

if __name__=='__main__':main()
