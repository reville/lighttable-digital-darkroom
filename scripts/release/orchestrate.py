#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Plan or advance one release stage using an immutable, resumable state file.

No polling loop and no speculative workflow search. --apply dispatches exactly
one stage; verify dispatches a read-only promotion, promote additionally requires
--make-public. An uncertain dispatch remains pending until --record-run-id is
validated against the exact workflow, source and run title.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

REPOSITORY = 'reville/lighttable-digital-darkroom'
PLATFORMS = ('linux-x86_64', 'windows-x64', 'macos-arm64')
WORKFLOWS = {'build': 'release.yml', 'vm': 'windows-client-vm.yml',
             'prepare': 'release-prepare.yml', 'verify': 'release-promote.yml',
             'promote': 'release-promote.yml'}


class OrchestrationError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise OrchestrationError(message)


def gh(*arguments):
    try:
        return subprocess.check_output(['gh', *arguments], text=True, stderr=subprocess.PIPE).strip()
    except subprocess.CalledProcessError as error:
        raise OrchestrationError(f'GitHub command failed: {error.stderr.strip()}') from error


def api(resource):
    try:
        return json.loads(gh('api', f'repos/{REPOSITORY}/{resource}'))
    except json.JSONDecodeError as error:
        raise OrchestrationError('GitHub returned malformed JSON') from error


def positive_id(value):
    require(re.fullmatch(r'[1-9][0-9]*', str(value or '')), 'A positive workflow run ID is required')
    return str(value)


def load(path, version, source, tag):
    if not path.exists():
        return dict(schema_version=1, repository=REPOSITORY, version=version,
                    source_revision=source, tag=tag, runs={})
    state = json.loads(path.read_text())
    require(state.get('schema_version') == 1 and isinstance(state.get('runs'), dict), 'Unsupported state format')
    require((state.get('repository'), state.get('version'), state.get('source_revision'), state.get('tag')) ==
            (REPOSITORY, version, source, tag), 'State repository/version/source/tag mismatch')
    return state


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.')
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(data, output, indent=2, sort_keys=True)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def state_lock(path, apply):
    if not apply:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + '.lock')
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise OrchestrationError(f'State is locked: {lock}. Inspect the active operator before removing a stale lock.') from error
    try:
        os.close(descriptor)
        yield
    finally:
        lock.unlink()


def verify_ref(tag, source):
    # Use the tag namespace, never a same-named branch.
    reference = api(f'git/ref/tags/{tag}')['object']
    if reference['type'] == 'tag':
        reference = api(f"git/tags/{reference['sha']}")['object']
    require(reference.get('type') == 'commit' and reference.get('sha') == source,
            'Immutable tag does not select the requested source revision')


def run_json(run_id, workflow, source, *, successful=False, title=None):
    run_id = positive_id(run_id)
    result = api(f'actions/runs/{run_id}')
    require(str(result.get('id')) == run_id, 'Run ID mismatch')
    require(result.get('head_repository', {}).get('full_name') == REPOSITORY, 'Run is from another repository')
    require(result.get('event') == 'workflow_dispatch', 'Run is not a trusted manual release workflow')
    require(result.get('path', '').split('@')[0] == f'.github/workflows/{workflow}', 'Run workflow mismatch')
    require(result.get('head_sha') == source, 'Run source revision mismatch')
    if title is not None:
        require(result.get('display_title', '').strip() == title.strip(), 'Run input/title mismatch')
    if successful:
        require(result.get('status') == 'completed' and result.get('conclusion') == 'success', 'Required run has not succeeded')
    return result


def validate_build(run_id, source, platform):
    run = run_json(run_id, 'release.yml', source)
    require(run.get('status') == 'completed', 'Original build is still running')
    pages = json.loads(gh('api', f'repos/{REPOSITORY}/actions/runs/{run_id}/jobs?per_page=100', '--paginate', '--slurp'))
    prefix = dict(zip(PLATFORMS, ('linux', 'windows', 'macos')))[platform]
    names = {prefix, prefix + '/package'}
    jobs = [job for page in pages for job in page['jobs'] if ''.join(job['name'].split()) in names]
    require(len(jobs) == 1 and jobs[0].get('conclusion') == 'success', 'Selected platform build has not succeeded')
    step = {'linux-x86_64': 'Require X11 native edit, RGB16 export and normal reopen',
            'windows-x64': 'Require native edit, export and restart',
            'macos-arm64': 'Bind final Mac artifacts to native and signing proof'}[platform]
    require(any(item.get('name') == step and item.get('conclusion') == 'success' for item in jobs[0].get('steps', [])),
            'Selected platform lacks successful native acceptance proof')


def recorded_id(state, key):
    return state['runs'].get(key, {}).get('run_id')


def inputs(args, state):
    stage = args.stage
    if stage == 'build':
        return dict(version=args.version, platforms=args.platforms, macos_channel=args.macos_channel)
    build = positive_id(args.build_run_id or recorded_id(state, 'build'))
    if stage == 'vm':
        require(re.fullmatch(r'[a-f0-9]{64}', args.installer_sha256 or ''), 'Exact installer SHA-256 is required')
        return dict(build_run_id=build, source_revision=args.source_revision, installer_sha256=args.installer_sha256)
    require(args.platform in PLATFORMS, 'A platform is required')
    version = args.version
    if args.platform == 'macos-arm64' and args.macos_channel == 'beta':
        version = args.macos_version or args.version + '-beta.1'
        require(re.fullmatch(re.escape(args.version) + r'-beta\.[1-9][0-9]*', version), 'macOS beta version must match the stable base version')
    else:
        require(not args.macos_version or args.macos_version == args.version, 'Beta version requires the explicit beta macOS channel')
    native = args.native_run_id or recorded_id(state, 'vm') or ''
    if args.platform == 'windows-x64':
        positive_id(native)
    result = dict(platform=args.platform, version=version, build_run_id=build,
                  source_revision=args.source_revision, native_run_id=native)
    if stage in ('verify', 'promote'):
        result['preparation_run_id'] = positive_id(args.preparation_run_id or recorded_id(state, 'prepare:' + args.platform))
        if stage == 'promote':
            require(args.make_public, 'Live promotion requires --make-public')
        else:
            require(not args.make_public and not args.advance_feed, 'Verification cannot publish or advance feeds')
        require(not args.advance_feed or args.platform != 'macos-arm64', 'The manual macOS beta has no update feed')
        result.update(dry_run=str(stage == 'verify').lower(), make_public=str(args.make_public).lower(),
                      advance_feed=str(args.advance_feed).lower())
    return result


def run_title(stage, values):
    if stage == 'build':
        return f"Release build v{values['version']} ({values['platforms']}, macOS {values['macos_channel']})"
    if stage == 'vm':
        return f"Windows clients for build {values['build_run_id']} source {values['source_revision']} installer {values['installer_sha256']}"
    if stage == 'prepare':
        return f"Prepare {values['platform']} {values['version']} from build {values['build_run_id']} native {values['native_run_id']}".strip()
    return (f"Promote {values['platform']} {values['version']} from preparation {values['preparation_run_id']} "
            f"dry={values['dry_run']} public={values['make_public']} feed={values['advance_feed']}")


def prerequisites(args, values):
    if args.stage == 'build':
        if values['macos_channel'] == 'beta' and values['platforms'] in ('all', 'macos'):
            verify_ref('macos-v' + args.version + '-beta.1', args.source_revision)
        return
    platform = 'windows-x64' if args.stage == 'vm' else args.platform
    validate_build(values['build_run_id'], args.source_revision, platform)
    if platform == 'macos-arm64' and '-beta.' in values.get('version', ''):
        verify_ref('macos-v' + values['version'], args.source_revision)
    if values.get('native_run_id'):
        run_json(values['native_run_id'], 'windows-client-vm.yml', args.source_revision, successful=True)
    if values.get('preparation_run_id'):
        prep = {key: values[key] for key in ('platform', 'version', 'build_run_id', 'source_revision', 'native_run_id')}
        run_json(values['preparation_run_id'], 'release-prepare.yml', args.source_revision,
                 successful=True, title=run_title('prepare', prep))


def dispatch(stage, fields, ref):
    command = ['workflow', 'run', WORKFLOWS[stage], '--repo', REPOSITORY, '--ref', ref]
    for key, value in fields.items():
        if value != '':
            command.extend(('-f', f'{key}={value}'))
    response = gh(*command)
    match = re.search(r'https://github\.com/' + re.escape(REPOSITORY) + r'/actions/runs/([1-9][0-9]*)\b', response)
    require(match is not None, 'Dispatch returned no exact run URL; inspect GitHub and recover with --record-run-id. Intent remains pending.')
    return match.group(1)


def execute(args):
    require(re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', args.version), 'Stable semantic version required')
    require(re.fullmatch(r'[a-f0-9]{40}', args.source_revision), 'Full source SHA required')
    tag = args.tag or 'v' + args.version
    require(tag == 'v' + args.version, 'The stable tag must match the release version')
    require(not args.record_run_id or (args.apply and args.stage != 'status'), '--record-run-id requires --apply and a dispatch stage')
    with state_lock(args.state, args.apply):
        state = load(args.state, args.version, args.source_revision, tag)
        if args.stage == 'status':
            records = {}
            for key, record in state['runs'].items():
                if record.get('run_id'):
                    run = run_json(record['run_id'], record['workflow'], args.source_revision, title=record['title'])
                    records[key] = {name: run.get(name) for name in ('id', 'status', 'conclusion', 'html_url')}
                else:
                    records[key] = dict(status='dispatch-pending')
            print(json.dumps(records, indent=2))
            return
        verify_ref(tag, args.source_revision)
        values = inputs(args, state)
        key = args.stage if args.stage in ('build', 'vm') else args.stage + ':' + args.platform
        intent = dict(workflow=WORKFLOWS[args.stage], ref=tag, inputs=values, title=run_title(args.stage, values))
        previous = state['runs'].get(key)
        if previous:
            require(all(previous.get(name) == value for name, value in intent.items()), 'Resume input fingerprint mismatch')
        if args.record_run_id:
            require(not previous or not previous.get('run_id') or previous['run_id'] == args.record_run_id, 'Cannot replace an already recorded run')
            run = run_json(args.record_run_id, intent['workflow'], args.source_revision, title=intent['title'])
            if previous and previous.get('started_at'):
                require(run.get('created_at', '') >= previous['started_at'], 'Recovery run predates this dispatch intent')
            state['runs'][key] = dict(intent, run_id=positive_id(args.record_run_id))
            atomic(args.state, state)
            print(f'Recorded {key}: {args.record_run_id}')
            return
        if previous:
            require(previous.get('run_id'), 'Dispatch is uncertain; inspect GitHub and recover with --record-run-id')
            run = run_json(previous['run_id'], intent['workflow'], args.source_revision, title=intent['title'])
            print(json.dumps(dict(stage=key, run_id=run['id'], status=run['status'], conclusion=run.get('conclusion')), indent=2))
            return
        prerequisites(args, values)
        if not args.apply:
            print(json.dumps(dict(stage=key, **intent), indent=2))
            return
        state['runs'][key] = dict(intent, started_at=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
        atomic(args.state, state)
        run_id = dispatch(args.stage, values, tag)
        state['runs'][key]['run_id'] = run_id
        atomic(args.state, state)
        print(f'Dispatched {key}: https://github.com/{REPOSITORY}/actions/runs/{run_id}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--source-revision', required=True)
    parser.add_argument('--tag')
    parser.add_argument('--state', type=Path, default=Path('.build/release-orchestration.json'))
    parser.add_argument('--stage', choices=('status', *WORKFLOWS), default='status')
    parser.add_argument('--platform', choices=PLATFORMS)
    parser.add_argument('--platforms', choices=('all', 'linux', 'windows', 'macos'), default='all')
    parser.add_argument('--macos-channel', choices=('stable', 'beta'), default='stable')
    parser.add_argument('--macos-version')
    parser.add_argument('--build-run-id')
    parser.add_argument('--preparation-run-id')
    parser.add_argument('--native-run-id')
    parser.add_argument('--installer-sha256')
    parser.add_argument('--record-run-id')
    parser.add_argument('--make-public', action='store_true')
    parser.add_argument('--advance-feed', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        execute(args)
        return 0
    except (OrchestrationError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f'Release orchestration: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
