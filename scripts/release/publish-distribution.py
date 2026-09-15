#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Stage verified Homebrew/npm metadata or dispatch downstream publication.

Requires completed promotion result JSON, records channel outcomes, and never
claims npm/Scoop publication merely because a workflow was dispatched.
"""
from __future__ import annotations
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/release'))
from release_evidence import load_promotions, distribution_outcomes
REPOSITORY = 'reville/lighttable-digital-darkroom'
SCOOP_REPO = 'reville/scoop-lighttable'
HOMEBREW_REPO = 'reville/homebrew-lighttable'
VERSION_RE = re.compile(r'^(\d+\.\d+\.\d+)(-beta\.\d+)?$')


def split_version(version):
    match = VERSION_RE.fullmatch(version)
    if not match:
        raise ValueError('Invalid release version')
    return match.group(1), bool(match.group(2))


def gh(*args):
    return subprocess.check_output(['gh', *map(str, args)], text=True)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def asset_digest(asset):
    digest = (asset or {}).get('digest', '')
    return digest[7:] if re.fullmatch(r'sha256:[a-f0-9]{64}', digest or '') else None


def release_assets(tag):
    data = json.loads(gh('release', 'view', tag, '--repo', REPOSITORY, '--json', 'assets,isDraft'))
    if data.get('isDraft') is not False:
        raise ValueError('Distribution requires a public release')
    return {a['name']: a for a in data['assets']}


def required_asset(promotion, suffix):
    assets = [a for a in promotion['manifest']['platforms'][promotion['platform']]['artifacts'] if a['name'].endswith(suffix)]
    if len(assets) != 1:
        raise ValueError(f'Expected exactly one {suffix} artifact')
    asset = assets[0]
    remote = release_assets(promotion['tag']).get(asset['name'])
    if not remote or remote.get('size') != asset['bytes'] or asset_digest(remote) != asset['sha256']:
        raise ValueError('Public asset identity differs from promotion evidence')
    return asset


def upload_if_needed(tag, path, assets, dry_run=False):
    existing = assets.get(path.name)
    if existing:
        if existing.get('size') != path.stat().st_size or asset_digest(existing) != sha256_file(path):
            raise ValueError(f'immutable asset mismatch: {path.name}')
        return 'reused'
    if dry_run:
        return 'staged'
    gh('release', 'upload', tag, path, '--repo', REPOSITORY)
    verified = release_assets(tag).get(path.name)
    if not verified or verified.get('size') != path.stat().st_size or asset_digest(verified) != sha256_file(path):
        raise ValueError(f'Uploaded asset readback mismatch: {path.name}')
    return 'uploaded'


def dispatch(repo, workflow, *inputs):
    output = gh('workflow', 'run', workflow, '--repo', repo, *inputs)
    match = re.search(r'https://github\.com/' + re.escape(repo) + r'/actions/runs/(\d+)', output)
    if not match:
        raise RuntimeError('Dispatch may have succeeded but returned no run URL; inspect GitHub before retrying')
    return {'channel_run_id': int(match.group(1)), 'run_url': match.group(0)}


def sync_homebrew(version, promotion, directory, push=False):
    base, beta = split_version(promotion['version'])
    if base != version or not beta:
        raise ValueError('This helper supports the explicitly selected macOS beta cask only')
    asset = required_asset(promotion, '.dmg')
    with tempfile.TemporaryDirectory(prefix='lighttable-tap-') as temp:
        tap = Path(temp) / 'tap'
        subprocess.run(['gh', 'repo', 'clone', HOMEBREW_REPO, str(tap), '--', '--depth=1', '--branch=main'], check=True, stdout=subprocess.DEVNULL)
        cask = tap / 'Casks/lighttable@beta.rb'
        original = cask.read_text()
        content, count = re.subn(r'(?m)^  version "[^"]+"$', f'  version "{promotion["version"]}"', original)
        if count != 1:
            raise ValueError('Unexpected cask version declaration')
        content, count = re.subn(r'(?m)^  sha256 "[^"]+"$', f'  sha256 "{asset["sha256"]}"', content)
        if count != 1:
            raise ValueError('Unexpected cask hash declaration')
        url = f'https://github.com/{REPOSITORY}/releases/download/{promotion["tag"]}/{asset["name"]}'
        content, count = re.subn(r'(?m)^  url "[^"]+"$', f'  url "{url}"', content)
        if count != 1:
            raise ValueError('Unexpected cask URL declaration')
        staged = directory / cask.name
        staged.write_text(content)
        subprocess.run(['ruby', '-c', str(staged)], check=True, stdout=subprocess.DEVNULL)
        evidence = {'version': promotion['version'], 'artifact_sha256': asset['sha256'], 'staged_path': str(staged)}
        if not push:
            return {'channel': 'homebrew', 'state': 'staged', 'evidence': evidence}
        if content != original:
            cask.write_text(content)
            for args in (['add', 'Casks/lighttable@beta.rb'], ['commit', '-m', f'Update LightTable beta to {promotion["version"]}'], ['push', 'origin', 'HEAD:main']):
                subprocess.run(['git', *args], cwd=tap, check=True, stdout=subprocess.DEVNULL)
        # Read the remote branch again, including the already-published resume case.
        subprocess.run(['git', 'fetch', 'origin', 'main'], cwd=tap, check=True, stdout=subprocess.DEVNULL)
        remote = subprocess.check_output(['git', 'show', 'origin/main:Casks/lighttable@beta.rb'], cwd=tap, text=True)
        if remote != content:
            raise ValueError('Remote cask readback mismatch')
        evidence['commit'] = subprocess.check_output(['git', 'rev-parse', 'origin/main'], cwd=tap, text=True).strip()
        return {'channel': 'homebrew', 'state': 'published', 'verified': True, 'evidence': evidence}


def stage_npm(version, promotion, directory):
    asset = required_asset(promotion, '-windows-x64-setup.exe')
    setup = directory / asset['name']
    if not setup.exists():
        gh('release', 'download', promotion['tag'], '--repo', REPOSITORY, '--pattern', asset['name'], '--dir', directory)
    if setup.stat().st_size != asset['bytes'] or sha256_file(setup) != asset['sha256']:
        raise ValueError('Downloaded Windows installer identity mismatch')
    with tempfile.TemporaryDirectory(prefix='lighttable-npm-') as temp:
        root = Path(temp)
        package = root / 'packaging/npm'
        shutil.copytree(ROOT / 'packaging/npm', package, ignore=shutil.ignore_patterns('node_modules', '*.tgz'))
        shutil.copy2(ROOT / 'LICENSE', root / 'LICENSE')
        subprocess.run(['node', 'scripts/prepare-release.mjs', version, str(directory)], cwd=package, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['npm', 'test'], cwd=package, check=True, stdout=subprocess.DEVNULL)
        subprocess.run(['npm', 'pack', '--pack-destination', str(root)], cwd=package, check=True, stdout=subprocess.DEVNULL)
        packed = root / f'lighttable-digital-darkroom-{version}.tgz'
        target = directory / packed.name
        if target.exists() and sha256_file(target) != sha256_file(packed):
            raise ValueError('Staged npm tarball differs; inspect before replacing it')
        shutil.copy2(packed, target)
    return target


def sync_npm(version, promotion, directory, apply=False, dispatch_npm=False):
    tarball = stage_npm(version, promotion, directory)
    tag = promotion['tag']
    assets = release_assets(tag)
    sums = directory / 'SHA256SUMS'
    expected = {tarball.name: sha256_file(tarball)}
    expected.update({a['name']: a['sha256'] for a in promotion['manifest']['platforms']['windows-x64']['artifacts']})
    if 'SHA256SUMS' in assets:
        if not sums.exists():
            gh('release', 'download', tag, '--repo', REPOSITORY, '--pattern', 'SHA256SUMS', '--dir', directory)
        if sha256_file(sums) != asset_digest(assets['SHA256SUMS']):
            raise ValueError('Public checksums identity mismatch')
        pairs = [line.split('  ', 1) for line in sums.read_text().splitlines() if line]
        if any(len(pair) != 2 or not re.fullmatch('[a-f0-9]{64}', pair[0]) for pair in pairs):
            raise ValueError('Malformed public checksums')
        existing = {name: digest for digest, name in pairs}
        if len(existing) != len(pairs) or any(existing.get(name) != digest for name, digest in expected.items()):
            raise ValueError('Existing immutable checksums do not match this package')
    else:
        hashes = {name: asset_digest(info) for name, info in assets.items() if name not in ('SHA256SUMS', tarball.name)}
        if not all(hashes.values()):
            raise ValueError('Public assets lack SHA256 metadata')
        hashes.update(expected)
        sums.write_text(''.join(f'{digest}  {name}\n' for name, digest in sorted(hashes.items())))
    uploads = [upload_if_needed(tag, path, assets, dry_run=not apply) for path in (tarball, sums)]
    evidence = {'tarball': str(tarball), 'sha256': sha256_file(tarball), 'uploads': uploads}
    result = {'channel': 'npm', 'state': 'staged', 'evidence': evidence}
    if apply and dispatch_npm:
        evidence.update(dispatch(REPOSITORY, 'npm-publish.yml', '--ref', tag, '-f', f'version={version}'))
        result['state'] = 'dispatched'
    return result


def public_bytes(url):
    """Bound each public read; verification never starts a watcher or retries."""
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = response.read(10 * 1024 * 1024 + 1)
    if len(payload) > 10 * 1024 * 1024:
        raise ValueError('Public package metadata/tarball exceeds the verification limit')
    return payload


def verify_published(channel, version, promotion, previous, directory):
    evidence = dict(previous.get('evidence', {}))
    if channel == 'scoop':
        asset = required_asset(promotion, '-windows-x64.zip')
        commit = json.loads(gh('api', f'repos/{SCOOP_REPO}/commits/main'))['sha']
        if not re.fullmatch(r'[a-f0-9]{40}', commit):
            raise ValueError('Scoop commit identity missing')
        response = json.loads(gh('api', f'repos/{SCOOP_REPO}/contents/bucket/lighttable.json?ref={commit}'))
        manifest = json.loads(base64.b64decode(response['content']))
        architecture = manifest.get('architecture', {}).get('64bit', {})
        if (manifest.get('version'), architecture.get('url'), architecture.get('hash')) != (version, asset['url'], asset['sha256']):
            raise ValueError('Scoop version/URL/hash differs from the promoted ZIP')
        evidence.update(commit=commit, version=version, url=asset['url'], sha256=asset['sha256'],
                        remote_manifest_verified=True)
    elif channel == 'npm':
        # Reuse the staged bytes, rather than repacking or downloading the 500 MB
        # desktop installer again. Match those bytes to the immutable public asset.
        tarball = directory / f'lighttable-digital-darkroom-{version}.tgz'
        digest = sha256_file(tarball)
        asset = release_assets(promotion['tag']).get(tarball.name)
        if not asset or asset.get('size') != tarball.stat().st_size or asset_digest(asset) != digest:
            raise ValueError('Staged npm tarball differs from the public release asset')
        if evidence.get('sha256') != digest:
            raise ValueError('Staged npm tarball differs from the dispatch receipt')
        registry = json.loads(public_bytes('https://registry.npmjs.org/lighttable-digital-darkroom'))
        package = registry.get('versions', {}).get(version, {})
        if package.get('name') != 'lighttable-digital-darkroom' or package.get('version') != version:
            raise ValueError('Exact npm version is not public')
        dist = package.get('dist', {})
        url = f'https://registry.npmjs.org/lighttable-digital-darkroom/-/{tarball.name}'
        if dist.get('tarball') != url:
            raise ValueError('Unexpected registry tarball URL')
        payload = public_bytes(url)
        integrity = 'sha512-' + base64.b64encode(hashlib.sha512(payload).digest()).decode('ascii')
        if (hashlib.sha256(payload).hexdigest() != digest
                or dist.get('shasum') != hashlib.sha1(payload).hexdigest()
                or dist.get('integrity') != integrity):
            raise ValueError('Registry tarball bytes or integrity differ from the staged package')
        evidence.update(tarball=str(tarball), registry_version=version,
                        registry_latest=registry.get('dist-tags', {}).get('latest'),
                        registry_sha1=dist['shasum'], registry_integrity=integrity,
                        registry_bytes_verified=True)
    else:
        raise ValueError('--verify-published supports Scoop and npm; Homebrew verifies during push')
    evidence['checked_at'] = datetime.now(timezone.utc).isoformat()
    return dict(channel=channel, state='published', verified=True, evidence=evidence)


def save(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as handle:
        json.dump(document, handle, indent=2)
        handle.write('\n')
        temporary = Path(handle.name)
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--macos-version')
    for flag in ('homebrew', 'scoop', 'npm', 'all', 'push', 'dispatch-npm', 'apply', 'dry-run', 'verify-published'):
        parser.add_argument('--' + flag, action='store_true')
    parser.add_argument('--output', type=Path, default=Path('.build/distribution/result.json'))
    parser.add_argument('--promotion-result', action='append', type=Path, required=True)
    args = parser.parse_args()
    try:
        if split_version(args.version)[1] or not any((args.homebrew, args.scoop, args.npm, args.all)):
            raise ValueError('Select channels and a stable desktop version')
        if args.apply and args.dry_run:
            raise ValueError('--apply and --dry-run are mutually exclusive')
        if args.verify_published and (args.apply or args.push or args.dispatch_npm or args.dry_run):
            raise ValueError('--verify-published is read-only remotely; do not combine it with publishing flags')
        promotions = load_promotions(args.promotion_result, args.version, require_public=True)
        source = next(iter(promotions.values()))['source_revision']
        selected = [c for c in ('homebrew', 'scoop', 'npm') if args.all or getattr(args, c)]
        for channel in selected:
            if ('macos-arm64' if channel == 'homebrew' else 'windows-x64') not in promotions:
                raise ValueError(f'Missing promotion evidence for {channel}')
        if 'homebrew' in selected and args.macos_version != promotions['macos-arm64']['version']:
            raise ValueError('--macos-version must explicitly match the Mac promotion')
        document = {'schema': 1, 'version': args.version, 'source_revision': source, 'outcomes': []}
        if args.output.exists():
            document = json.loads(args.output.read_text())
        outcomes = distribution_outcomes(document, args.version, source)
        directory = args.output.resolve().parent
        directory.mkdir(parents=True, exist_ok=True)
        if args.verify_published:
            if 'homebrew' in selected:
                raise ValueError('--verify-published supports --scoop and --npm only')
            for channel in selected:
                previous = outcomes.get(channel, {})
                if previous.get('state') not in ('dispatched', 'published'):
                    raise ValueError(f'{channel} needs a recorded dispatch/publication before verification')
                # On failure retain the prior receipt; no false success or lost
                # dispatch identity. A later explicit invocation can try again.
                outcomes[channel] = verify_published(channel, args.version, promotions['windows-x64'], previous, directory)
                document['outcomes'] = list(outcomes.values())
                save(args.output, document)
            print(json.dumps(document, indent=2))
            return 0
        # Same output path is the single-operator resume record. No automatic retry
        # after an uncertain dispatch: inspect its recorded intent first.
        for channel in selected:
            previous = outcomes.get(channel, {})
            if previous.get('state') in ('dispatched', 'published'):
                continue
            if previous.get('dispatch_pending'):
                raise ValueError(f'{channel} has an uncertain dispatch; reconcile its run before retrying')
            will_dispatch = args.apply and (channel == 'scoop' or channel == 'npm' and args.dispatch_npm)
            if will_dispatch:
                outcomes[channel] = {'channel': channel, 'state': 'unverified', 'dispatch_pending': True}
                document['outcomes'] = list(outcomes.values())
                save(args.output, document)
            try:
                if channel == 'homebrew':
                    result = sync_homebrew(args.version, promotions['macos-arm64'], directory, push=args.apply and args.push)
                elif channel == 'npm':
                    result = sync_npm(args.version, promotions['windows-x64'], directory, args.apply, args.dispatch_npm)
                else:
                    required_asset(promotions['windows-x64'], '-windows-x64-setup.exe')
                    result = {'channel': 'scoop', 'state': 'staged'}
                    if args.apply:
                        result.update(state='dispatched', evidence=dispatch(SCOOP_REPO, 'update.yml'))
                outcomes[channel] = result
            except (ValueError, OSError, subprocess.SubprocessError, RuntimeError) as error:
                outcomes[channel] = {'channel': channel, 'state': 'failed', 'error': str(error), 'dispatch_pending': will_dispatch}
            document['outcomes'] = list(outcomes.values())
            save(args.output, document)
        print(json.dumps(document, indent=2))
        return int(any(item['state'] in ('failed', 'blocked', 'unverified') for item in outcomes.values()))
    except (ValueError, OSError, StopIteration, KeyError, TypeError, subprocess.SubprocessError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
