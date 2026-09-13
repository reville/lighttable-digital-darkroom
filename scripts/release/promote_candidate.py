#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Promote one already-built platform; dry-run unless --apply is explicit.

The preparation artifact contains the immutable platform release manifest and its
assets at its root. Original binaries and native/signing evidence are downloaded
independently from the selected build/test runs. No compilation or signing occurs.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
import zipfile

import release_process as release
from prepare_candidate import build_run

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SPARKLE = 'http://www.andymatuschak.org/xml-namespaces/sparkle'
SPARKLE_KEY = 'mrBmSL8f0FRN8j/imZxCWdCt0L4N3zP9kgOleH46eXA='
BUILD_ARTIFACTS = {'linux-x86_64': 'LightTable-linux-x86_64',
                   'windows-x64': 'LightTable-windows-x64', 'macos-arm64': 'LightTable-macos-arm64'}
require = release.require


def workflow_run(run_id, paths=None, source=None):
    require(re.fullmatch(r'[1-9][0-9]{0,14}', str(run_id)), 'Invalid workflow run ID')
    run = json.loads(release.gh('api', f'repos/{release.REPOSITORY}/actions/runs/{run_id}'))
    require(run.get('conclusion') == 'success' and run.get('status') == 'completed'
            and run.get('head_repository', {}).get('full_name') == release.REPOSITORY
            and run.get('event') not in ('pull_request', 'pull_request_target'),
            'Select a successful completed non-PR run in the public repository')
    require(not paths or run.get('path') in paths, 'Unexpected producer workflow')
    require(not source or run.get('head_sha') == source, 'Build run source differs from selected release source')
    return run


def download_artifact(run_id, name, directory):
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name), 'Unsafe artifact name')
    directory.mkdir(parents=True, exist_ok=False)
    release.gh('run', 'download', str(run_id), '--repo', release.REPOSITORY,
               '--name', name, '--dir', str(directory))
    return directory


def check_identity(actual, expected, message):
    require(actual.get('sha256') == expected['sha256'] and actual.get('bytes') == expected['bytes'], message)


def primary_names(manifest, platform):
    stem = f"LightTable-{manifest['version']}-{platform}"
    if platform == 'linux-x86_64':
        return [stem + '.tar.gz']
    if platform == 'windows-x64':
        return [stem + '.zip', stem + '-setup.exe']
    names = [stem + '.dmg']
    if manifest['platforms'][platform]['update_owner'] == 'app':
        names.append(stem + '.zip')
    return names


def verify_original_binaries(manifest, platform, original):
    assets = {a['name']: a for a in manifest['platforms'][platform]['artifacts']}
    for name in primary_names(manifest, platform):
        require(name in assets, f'Missing platform binary: {name}')
        check_identity(release.file_identity(original / name), assets[name], 'Candidate differs from the original build artifact')


def verify_native_report(report, manifest, platform):
    require(report.get('ok') is True and report.get('export'), 'Native edit/export acceptance did not pass')
    release.check_build_manifest(report.get('build', {}), manifest, platform)
    if platform == 'windows-x64':
        require(report.get('edit_persistence', {}).get('server_restarted') is True,
                'Native Windows quit/reopen persistence is unproven')
    elif platform == 'linux-x86_64':
        require(report.get('http') == 200 and report.get('persistence') and report.get('final_close'),
                'Native Linux quit/reopen persistence is unproven')


def verify_windows_runtime_case(host, windows):
    """Require explicit, observed prerequisite coverage for each client OS."""
    require(windows in ('10', '11'), 'Unsupported Windows client runtime case')
    if windows == '10':
        require(host.get('webview2_test_case') == 'absent'
                and host.get('webview2_absent_before_offline_install') is True,
                'Windows 10 offline WebView2-absent coverage is unproven')
    else:
        require(host.get('webview2_test_case') == 'preinstalled'
                and host.get('webview2_initially_present') is True
                and host.get('webview2_absent_before_offline_install') is False,
                'Windows 11 preinstalled WebView2 coverage is unproven')


def verify_windows_client(directory, windows, manifest, installer_hash):
    host = release.read_json(directory / 'host.json')
    result = release.read_json(directory / 'result.json')
    build = int(host.get('build', 0))
    require(host.get('ok') is True and host.get('product_type') == 1
            and host.get('architecture') == 'AMD64'
            and ((windows == '11' and build >= 22000) or (windows == '10' and 19041 <= build < 22000)),
            f'Windows {windows} native x64 client identity is unproven')
    verify_windows_runtime_case(host, windows)
    require(host.get('network_adapters_disabled') is True
            and host.get('installer_signature_before_disconnect') == 'Valid'
            and result.get('ok') is True and result.get('offline') is True
            and result.get('account_is_elevated') is False
            and result.get('installer_sha256') == installer_hash,
            f'Windows {windows} exact-installer offline, unelevated acceptance is unproven')
    signatures = release.read_json(directory / 'installed-signatures.json')
    entries = signatures.get('signatures', [])
    require(signatures.get('invalid_count') == 0 and signatures.get('pe_count', 0) > 0
            and len(entries) == signatures['pe_count'] and all(s.get('status') == 'Valid' for s in entries)
            and any(s.get('path', '').replace('\\', '/').split('/')[-1].lower() == 'uninstall.exe' for s in entries),
            'Installed payload signature audit, including uninstaller, did not pass')
    verify_native_report(release.read_json(directory / 'native/report.json'), manifest, 'windows-x64')


def verify_evidence(args, manifest, original, temporary):
    platform = args.platform
    if platform == 'linux-x86_64':
        evidence = download_artifact(args.build_run_id, 'Linux-X11-native-acceptance', temporary / 'native')
        verify_native_report(release.read_json(evidence / 'report.json'), manifest, platform)
        return
    if platform == 'windows-x64':
        require(args.native_run_id, 'Windows promotion requires both native x64 client receipts')
        workflow_run(args.native_run_id, {'.github/workflows/windows-client-vm.yml'})
        signatures = release.read_json(original / 'windows-signatures.json')
        assets = {a['name']: a for a in manifest['platforms'][platform]['artifacts']}
        archive_name, installer_name = primary_names(manifest, platform)
        installer_hash = assets[installer_name]['sha256']
        require(signatures.get('source_revision') == manifest['source_revision']
                and signatures.get('version') == manifest['version']
                and signatures.get('archive_sha256') == assets[archive_name]['sha256'],
                'Authenticode receipt source/archive identity differs')
        rows = signatures.get('signatures', [])
        required_names = {'LightTable.exe', 'WinSparkle.dll', 'lighttable-engine.exe', 'spektrafilm-rs.exe', installer_name}
        require(len(rows) == len(required_names) and {r.get('file') for r in rows} == required_names
                and all(r.get('status') == 'Valid' and r.get('timestamp_authority') for r in rows)
                and any(r.get('file') == installer_name and r.get('sha256') == installer_hash for r in rows),
                'Complete timestamped Authenticode proof is missing')
        for windows in ('10', '11'):
            evidence = download_artifact(args.native_run_id, f'Windows-{windows}-x64-client-acceptance',
                                         temporary / f'windows-{windows}')
            verify_windows_client(evidence, windows, manifest, installer_hash)
        return
    report = release.read_json(original / 'macos-release-proof.json')
    require(report.get('schema_version') == 1 and report.get('source_revision') == manifest['source_revision']
            and report.get('version') == manifest['version'] and report.get('source_dirty') is False
            and report.get('architecture') == 'arm64', 'macOS proof identity mismatch')
    required_assets = {name: release.file_identity(original / name) for name in primary_names(manifest, platform)}
    recorded = {item['name']: item for item in report.get('artifacts', [])}
    for name, identity in required_assets.items():
        check_identity(recorded.get(name, {}), identity, 'macOS proof asset differs')
    require(report.get('code_signature_verified') is True
            and report.get('native', {}).get('ok') is True
            and report.get('native', {}).get('layer') == 'package',
            'macOS signature/package-render acceptance receipt is missing')
    require(report.get('signing') == manifest['platforms'][platform]['signing'], 'macOS signing declaration differs')
    if manifest['platforms'][platform]['update_owner'] == 'app':
        require(report.get('update_public_key') == SPARKLE_KEY,
                'macOS embedded update key differs from the verified signing key')
    if manifest['platforms'][platform]['signing'] == 'developer-id-notarized':
        require(report.get('notarization_verified') is True, 'macOS notarization/Gatekeeper receipt is missing')


def version_key(value):
    require(isinstance(value, str) and release.VERSION.fullmatch(value), 'Invalid feed version')
    main, separator, beta = value.partition('-beta.')
    return (*map(int, main.split('.')), 0 if separator else 1, int(beta) if separator else 0)


def feed_version(data, platform):
    if platform == 'linux-x86_64':
        return json.loads(data)['signed']['version']
    root = ET.fromstring(data)
    require(root.tag == 'rss' and root.find('channel') is not None, 'Invalid appcast')
    values = [enclosure.get(f'{{{SPARKLE}}}shortVersionString') or enclosure.get(f'{{{SPARKLE}}}version')
              for enclosure in root.findall('./channel/item/enclosure')]
    return max(values, key=version_key) if values else None


def verify_signed_feed(manifest, platform, directory):
    entry = manifest['platforms'][platform]
    if entry['update_owner'] != 'app' and platform != 'linux-x86_64':
        return None
    feed = directory / release.FEEDS[platform]
    require(feed.name in {a['name'] for a in entry['artifacts']}, 'App-owned update channel requires a verified feed asset')
    if platform == 'linux-x86_64':
        import desktop_updater
        archive = directory / primary_names(manifest, platform)[0]
        with tarfile.open(archive, 'r:gz') as stream:
            members = [m for m in stream if m.name == 'LightTable/update-config.json']
            require(len(members) == 1 and members[0].isfile() and members[0].size <= 65536, 'Missing embedded updater configuration')
            config = json.load(stream.extractfile(members[0]))
        public = os.environ.get('LIGHTTABLE_LINUX_UPDATE_PUBLIC_KEY', '')
        require(public and config.get('public_key') == public, 'Linux update key is not the configured production key')
        signed = desktop_updater.verify_manifest(release.read_json(feed), public, architecture='x86_64',
                                                current_version=manifest['version'], hosts=desktop_updater.DOWNLOAD_HOSTS)
        identity = release.file_identity(archive)
        require(signed['version'] == manifest['version'] and signed['source_revision'] == manifest['source_revision']
                and signed['url'] == release.asset_url(manifest, archive.name)
                and signed['sha256'] == identity['sha256'] and signed['size'] == identity['bytes'],
                'Signed Linux feed does not describe this exact release')
    else:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        if platform == 'windows-x64':
            archive = directory / primary_names(manifest, platform)[0]
            with zipfile.ZipFile(archive) as bundle:
                entries = [e for e in bundle.infolist() if e.filename == 'LightTable/LightTable.exe']
                require(len(entries) == 1 and entries[0].file_size <= 128 * 1024 * 1024,
                        'Windows updater executable is missing or ambiguous')
                require(SPARKLE_KEY.encode('ascii') in bundle.read(entries[0]),
                        'Windows executable does not contain the verified update public key')
        root = ET.fromstring(feed.read_bytes())
        entries = root.findall('./channel/item/enclosure')
        require(len(entries) == 1, 'Candidate appcast must contain exactly this release')
        enclosure = entries[0]
        binary_name = primary_names(manifest, platform)[-1]
        binary = directory / binary_name
        require(enclosure.get('url') == release.asset_url(manifest, binary_name)
                and enclosure.get('length') == str(binary.stat().st_size)
                and feed_version(feed.read_bytes(), platform) == manifest['version'],
                'Appcast version, URL or length differs from candidate')
        signature = base64.b64decode(enclosure.get(f'{{{SPARKLE}}}edSignature', ''), validate=True)
        Ed25519PublicKey.from_public_bytes(base64.b64decode(SPARKLE_KEY)).verify(signature, binary.read_bytes())
    return feed


def public_download(url, destination):
    # Fixed canonical URLs from validated manifests, without authentication.
    require(url.startswith(f'https://github.com/{release.REPOSITORY}/releases/'), 'Unexpected public asset URL')
    with urlopen(Request(url, headers={'User-Agent': 'LightTable-release-verifier'}), timeout=60) as response:
        require(response.status == 200 and response.url.startswith('https://'), 'Public asset is unavailable')
        with destination.open('wb') as target:
            while chunk := response.read(1024 * 1024):
                target.write(chunk)


def check_feed_forward(current, candidate, platform):
    current_version = feed_version(current, platform)
    candidate_version = feed_version(candidate, platform)
    require(candidate_version, 'Candidate feed has no release')
    if current_version is not None:
        require(version_key(candidate_version) >= version_key(current_version), 'Refusing to downgrade the update feed')
        require(version_key(candidate_version) != version_key(current_version) or current == candidate,
                'Same-version feed bytes differ; explicit repair review is required')


def advance_feed(manifest, platform, feed, temporary):
    tag = manifest.get('tag', 'v' + manifest['version'])
    if platform == 'macos-arm64':
        latest = json.loads(release.gh('release', 'view', '--repo', release.REPOSITORY, '--json', 'tagName,assets'))
        if any(a['name'] == feed.name for a in latest['assets']):
            prior = temporary / 'current-feed'
            prior.mkdir()
            release.gh('release', 'download', latest['tagName'], '--repo', release.REPOSITORY,
                       '--pattern', feed.name, '--dir', str(prior))
            check_feed_forward((prior / feed.name).read_bytes(), feed.read_bytes(), platform)
        # Existing macOS apps read releases/latest; no other platform gets to move it.
        release.gh('release', 'edit', tag, '--repo', release.REPOSITORY, '--latest')
        pointer = f'https://github.com/{release.REPOSITORY}/releases/latest/download/{feed.name}'
    else:
        current = json.loads(release.gh('release', 'view', 'desktop-updates', '--repo', release.REPOSITORY,
                                      '--json', 'isDraft,assets'))
        require(current['isDraft'] is False, 'Desktop update channel must already be public')
        previous = next((a for a in current['assets'] if a['name'] == feed.name), None)
        if previous:
            prior = temporary / 'current-feed'
            prior.mkdir()
            release.gh('release', 'download', 'desktop-updates', '--repo', release.REPOSITORY,
                       '--pattern', feed.name, '--dir', str(prior))
            check_feed_forward((prior / feed.name).read_bytes(), feed.read_bytes(), platform)
        # The only replaceable object is this platform's mutable signed pointer.
        release.gh('release', 'upload', 'desktop-updates', str(feed), '--repo', release.REPOSITORY, '--clobber')
        pointer = f'https://github.com/{release.REPOSITORY}/releases/download/desktop-updates/{feed.name}'
    downloaded = temporary / 'public-feed'
    public_download(pointer, downloaded)
    require(downloaded.read_bytes() == feed.read_bytes(), 'Public updater pointer does not match the verified feed')


def release_state(tag):
    """A successful complete listing distinguishes absence from API/auth failure."""
    pages = json.loads(release.gh('api', f'repos/{release.REPOSITORY}/releases?per_page=100',
                                 '--paginate', '--slurp'))
    require(isinstance(pages, list) and pages and all(isinstance(page, list) for page in pages),
            'Release listing did not return complete pages')
    matches = [entry for page in pages for entry in page if entry.get('tag_name') == tag]
    require(len(matches) <= 1, 'Ambiguous release tag in repository listing')
    if not matches:
        return None
    value = matches[0]
    require(type(value.get('draft')) is bool and type(value.get('prerelease')) is bool,
            'Release listing has invalid channel metadata')
    return {'isDraft': value['draft'], 'isPrerelease': value['prerelease'],
            'publishedAt': value.get('published_at'), 'url': value.get('html_url'), 'tagName': tag}


# The releases listing can briefly omit a just-created draft (release-promote run
# 34730115761). Only absence is retried; listing errors and metadata mismatches
# still fail immediately. Five reads over at most 15 seconds of waiting.
DRAFT_READ_DELAYS = (1, 2, 4, 8)


def created_draft_state(tag):
    state = release_state(tag)
    for delay in DRAFT_READ_DELAYS:
        if state is not None:
            break
        time.sleep(delay)
        state = release_state(tag)
    return state


def plan_or_publish(manifest_path, candidate, platform, apply):
    """Called only after artifact and external-proof verification succeeds."""
    manifest = release.read_json(manifest_path)
    tag = manifest.get('tag', 'v' + manifest['version'])
    state = release_state(tag)
    create_draft = state is None
    if state is not None:
        require(state['isPrerelease'] is ('-beta.' in manifest['version']),
                'Existing release channel metadata differs from the candidate version')
    if create_draft and not apply:
        assets = release.publication_plan(manifest, [], [platform])
        assets.append({**release.file_identity(manifest_path),
                       'url': release.asset_url(manifest, Path(manifest_path).name), 'action': 'add'})
        result = {'applied': False, 'source_revision': manifest['source_revision'], 'assets': assets}
        state = {'isDraft': True, 'isPrerelease': '-beta.' in manifest['version'],
                 'publishedAt': None, 'url': None, 'tagName': tag}
    else:
        if create_draft:
            options = ['--prerelease'] if '-beta.' in manifest['version'] else []
            # --verify-tag forbids synthesizing a source tag. A concurrent creation
            # fails safely; a later retry observes it and checks immutable assets.
            release.gh('release', 'create', tag, '--repo', release.REPOSITORY,
                       '--verify-tag', '--draft', '--latest=false', '--generate-notes',
                       '--title', 'LightTable ' + manifest['version'], *options)
            state = created_draft_state(tag)
            require(state is not None and state['isDraft']
                    and state['isPrerelease'] is ('-beta.' in manifest['version']),
                    'New draft metadata did not match the verified candidate')
        result = release.publish(manifest_path, candidate, [platform], apply=apply)
    result['create_draft'] = create_draft
    result['draft_created'] = create_draft and apply
    return result, state


def promote(args):
    require(not args.advance_feed or args.make_public, 'Feed advancement requires --make-public')
    with tempfile.TemporaryDirectory(prefix='lighttable-promote-') as temporary_name:
        temporary = Path(temporary_name)
        original_run = build_run(args.build_run_id, args.platform)
        workflow_run(args.manifest_run_id or args.build_run_id)
        candidate = download_artifact(args.manifest_run_id or args.build_run_id, args.manifest_artifact, temporary / 'candidate')
        manifest_name = release.platform_names(args.version, args.platform)['manifest']
        manifest_path = candidate / manifest_name
        manifest = release.validate_manifest(release.read_json(manifest_path))
        require(manifest['version'] == args.version and manifest['source_revision'] == args.source_revision
                and set(manifest['platforms']) == {args.platform}, 'Selected version/source/platform differs from manifest')
        entry = manifest['platforms'][args.platform]
        require(entry.get('build_workflow_revision', original_run['head_sha']) == original_run['head_sha'],
                'Prepared build workflow revision differs from the selected run')
        require(str(entry.get('build_run_id', '')) == str(args.build_run_id), 'Manifest build run does not match selection')
        tag = manifest.get('tag', 'v' + args.version)
        require(release.gh('api', f'repos/{release.REPOSITORY}/commits/{tag}', '--jq', '.sha').strip() == args.source_revision,
                'Immutable tag does not select this source')
        # Use the same CLI contract as release preparation before planning any writes.
        release.verify_artifacts(manifest, candidate, [args.platform])
        require(not entry.get('gates'), '; '.join(entry.get('gates', [])))
        recorded_native = str(entry.get('native_run_id') or '')
        require(not args.native_run_id or not recorded_native or str(args.native_run_id) == recorded_native,
                'Native run input differs from prepared manifest')
        args.native_run_id = args.native_run_id or recorded_native
        original = download_artifact(args.build_run_id, BUILD_ARTIFACTS[args.platform], temporary / 'original')
        verify_original_binaries(manifest, args.platform, original)
        verify_evidence(args, manifest, original, temporary)
        feed = verify_signed_feed(manifest, args.platform, candidate)
        # The preparer records pending candidates. Only verified external receipts
        # can turn them into a ready manifest; identical reruns produce identical bytes.
        entry['state'] = 'ready'
        proof_runs = [str(args.build_run_id)] + ([str(args.native_run_id)] if args.native_run_id else [])
        entry['validation'] = {'status': 'passed', 'receipts': [
            f'https://github.com/{release.REPOSITORY}/actions/runs/{run_id}' for run_id in proof_runs]}
        gates = release.promotion_gates(args.platform, entry)
        require(not gates, '; '.join(gates))
        release.write_json(manifest_path, manifest)
        require(not args.advance_feed or (feed is not None and entry['update_owner'] == 'app'), 'This platform channel does not support app-owned updates')
        result, state = plan_or_publish(manifest_path, candidate, args.platform, args.apply)
        result.update(ok=True, platform=args.platform, version=args.version, tag=tag,
                      source_revision=args.source_revision,
                      release_url=state.get('url'), published_release=not state['isDraft'],
                      published_at=state.get('publishedAt'), manifest_path=manifest_name,
                      manifest_url=release.asset_url(manifest, manifest_name), manifest=manifest,
                      build_run_id=args.build_run_id,
                      build_workflow_revision=original_run['head_sha'], receipts_verified=True,
                      public_bytes_verified=False, feed_advanced=False)
        if args.apply:
            if state['isDraft'] and args.make_public:
                release.gh('release', 'edit', tag, '--repo', release.REPOSITORY, '--draft=false', '--latest=false')
                state['isDraft'] = False
            if not state['isDraft']:
                for asset in result['assets']:
                    destination = temporary / ('public-' + asset['name'])
                    public_download(asset['url'], destination)
                    check_identity(release.file_identity(destination), asset, 'Independent public download checksum differs')
                result['public_bytes_verified'] = True
                if args.advance_feed:
                    advance_feed(manifest, args.platform, feed, temporary)
                    result['feed_advanced'] = True
            final_state = json.loads(release.gh('release', 'view', tag, '--repo', release.REPOSITORY,
                                               '--json', 'isDraft,publishedAt,url'))
            result.update(published_release=not final_state['isDraft'],
                          published_at=final_state.get('publishedAt'), release_url=final_state.get('url'))
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform', required=True, choices=release.PLATFORMS)
    for name in ('version', 'source-revision', 'build-run-id', 'manifest-artifact'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--manifest-run-id')
    parser.add_argument('--native-run-id')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--make-public', action='store_true')
    parser.add_argument('--advance-feed', action='store_true')
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        require(release.VERSION.fullmatch(args.version) and release.REVISION.fullmatch(args.source_revision),
                'Exact version and source SHA are required')
        result = promote(args)
        release.write_json(args.output, result)
        print(json.dumps(result, indent=2))
        return 0
    except Exception as error:
        # Retain a failure receipt; never echo GitHub subprocess diagnostics.
        message = 'GitHub command failed; inspect the selected run and artifact availability' if isinstance(error, subprocess.CalledProcessError) else str(error)
        release.write_json(args.output, {'applied': args.apply, 'ok': False, 'error': message,
                                         'platform': args.platform, 'version': args.version})
        print('release-promotion: ' + message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
