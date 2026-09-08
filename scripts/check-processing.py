#!/usr/bin/env python3
"""Fail-closed processing correctness gate with independent reference artifacts."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
MODULES = {'film': 'processing_reference', 'export': 'processing_export',
           'webgl': 'processing_webgl', 'app': 'processing_app',
           'native': 'native_processing_preview'}


def passed(suite):
    records = suite.get('records')
    return (suite.get('status', 'pass') == 'pass' and isinstance(records, list)
            and bool(records) and all(record.get('status') == 'pass' for record in records))


def provenance():
    def git(*args):
        result = subprocess.run(['git', *args], cwd=ROOT, capture_output=True,
                                text=True, timeout=15, check=True)
        return result.stdout.strip()
    paths = ['film_pipeline.py', 'grade.py', 'edits.py', 'render_cli.py',
             'gpu_compute.py', 'merge_acceleration.py', 'merge_workflow.py',
             'color_pipeline.py', 'web/gl.js', 'app/NativePreview.swift', 'app/NativePreview.metal']
    paths += [str(path.relative_to(ROOT)) for path in (ROOT / 'tests').glob('*processing*') if path.is_file()]
    paths += [str(path.relative_to(ROOT)) for path in (ROOT / 'rust-engine').rglob('*')
              if path.suffix in {'.rs', '.wgsl'} and 'target' not in path.parts]
    paths += ['scripts/check-processing.py', 'server.py', 'web/app.js']
    return {'revision': git('rev-parse', 'HEAD'), 'workingTree': git('status', '--short'),
            'host': platform.platform(), 'python': sys.version,
            'sha256': {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in paths}}


def write_reports(report, output):
    (output / 'report.json').write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    suite_xml = ET.Element('testsuite', name='LightTable processing correctness')
    count = failures = 0
    lines = [f"# Processing correctness: {report['status'].upper()}", '',
             f"Revision: `{report['provenance']['revision']}`", '',
             'A pass establishes agreement with the named software references for these fixtures.',
             'It does not establish a match to physical film, every RAW decoder, or an untested display.', '',
             '| Suite | Result | Checks |', '| --- | --- | ---: |']
    for name, suite in report['suites'].items():
        records = suite.get('records') or []
        status = 'pass' if passed(suite) else suite.get('status', 'fail')
        if status == 'pass' and not passed(suite):
            status = 'fail'
        lines.append(f'| {name} | {status} | {len(records)} |')
        for record in records:
            count += 1
            element = ET.SubElement(suite_xml, 'testcase', classname=name,
                                    name=record.get('name', 'unnamed'))
            if record.get('status') != 'pass':
                failures += 1
                ET.SubElement(element, 'failure', message=record.get('error') or
                              record.get('reason') or 'Pixel/reference mismatch').text = json.dumps(record, indent=2)
        if not passed(suite) and (not records or all(r.get('status') == 'pass' for r in records)):
            count += 1
            failures += 1
            case = ET.SubElement(suite_xml, 'testcase', classname=name, name='required-suite')
            ET.SubElement(case, 'failure', message=suite.get('error') or suite.get('reason') or
                          'Suite unavailable or incomplete').text = json.dumps(suite, indent=2)
    suite_xml.set('tests', str(count))
    suite_xml.set('failures', str(failures))
    ET.ElementTree(suite_xml).write(output / 'junit.xml', encoding='utf-8', xml_declaration=True)
    lines.extend(['', '## Failures', ''])
    for name, suite in report['suites'].items():
        if passed(suite):
            continue
        lines.append(f"- **{name}**: {suite.get('error') or suite.get('reason') or 'numerical mismatches'}")
        for record in suite.get('records', []):
            if record.get('status') != 'pass':
                lines.append(f"  - `{record.get('name')}`: {record.get('error') or record.get('reason') or record.get('metrics')}")
    if not failures:
        lines.append('None.')
    lines.extend(['', 'Per-case references, actual outputs, tolerances, and difference images are linked in report.json.', ''])
    (output / 'report.md').write_text('\n'.join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suites', default=','.join(MODULES), help='Comma separated: film,export,webgl,app,native')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/processing-correctness')
    parser.add_argument('--film-gpu', action='store_true', help='Also require film CPU/GPU parity')
    parser.add_argument('--timeout', type=int, default=1200, help='Hard timeout in seconds per suite')
    parser.add_argument('--worker', choices=MODULES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if args.worker:
        try:
            module = importlib.import_module(MODULES[args.worker])
            result = module.run(output, **({'gpu': args.film_gpu} if args.worker == 'film' else {}))
            if isinstance(result, list):
                result = {'records': result}
            if not passed(result):
                result['status'] = result.get('status') if result.get('status') in ('blocked', 'fail') else 'fail'
            else:
                result['status'] = 'pass'
        except Exception as error:
            traceback.print_exc()
            result = {'status': 'fail', 'error': str(error), 'records': []}
        (output / 'suite.json').write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
        return 0 if passed(result) else 1
    selected = args.suites.split(',')
    if not selected or len(set(selected)) != len(selected) or any(s not in MODULES for s in selected):
        parser.error('Select each required suite at most once: ' + ','.join(MODULES))
    if not 1 <= args.timeout <= 7200:
        parser.error('--timeout must be 1..7200')
    report = {'schema': 1, 'startedAt': datetime.now(timezone.utc).isoformat(),
              'provenance': provenance(), 'requestedSuites': selected,
              'unrequestedSuites': [s for s in MODULES if s not in selected],
              'filmGpuRequired': args.film_gpu,
              'suites': {name: {'status':'pending', 'records':[], 'error':'Required suite has not completed'}
                         for name in selected}, 'status':'incomplete'}
    started = time.monotonic()
    write_reports(report, output)
    for name in selected:
        print(f'Checking {name}...', flush=True)
        directory = output / name
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'suite.json'
        # An old report must never stand in for a crashed worker.
        path.unlink(missing_ok=True)
        command = [sys.executable, str(Path(__file__).resolve()), '--worker', name, '--output', str(directory)]
        if args.film_gpu:
            command.append('--film-gpu')
        try:
            with (directory / 'runner.log').open('w') as log:
                child = subprocess.Popen(command, cwd=ROOT, env=dict(os.environ, MPLBACKEND='Agg'),
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=(os.name == 'posix'))
                try:
                    child.wait(timeout=args.timeout)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    # Stop the entire test process group, including its server,
                    # compiler/browser/renderer children, on the hard bound.
                    if os.name == 'posix':
                        os.killpg(child.pid, signal.SIGTERM)
                    else:
                        child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        if os.name == 'posix':
                            os.killpg(child.pid, signal.SIGKILL)
                        else:
                            child.kill()
                        child.wait(timeout=5)
                    raise
            suite = json.loads(path.read_text()) if path.is_file() else {
                'status': 'fail', 'error': f'Worker exited {child.returncode} without a report', 'records': []}
            if child.returncode and passed(suite):
                suite.update(status='fail', error=f'Worker exited {child.returncode}')
        except (subprocess.TimeoutExpired, ValueError, OSError) as error:
            suite = {'status': 'fail', 'error': str(error), 'records': []}
        report['suites'][name] = suite
        report['status'] = 'pass' if all(passed(s) for s in report['suites'].values()) else 'fail'
        report['seconds'] = round(time.monotonic() - started, 3)
        write_reports(report, output)
        print(f"{name}: {suite.get('status')} ({len(suite.get('records', []))} checks)", flush=True)
    print(f"{report['status'].upper()}: {output / 'report.md'}", flush=True)
    return 0 if report['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
