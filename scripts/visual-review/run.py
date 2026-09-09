#!/usr/bin/env python3
"""Record bounded native journeys and retain honest, reviewable evidence."""
from __future__ import annotations
import argparse
import hashlib
import html
import importlib.util
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('native_smoke', ROOT / 'scripts/native-app-smoke.py')
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()


def checked(args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, capture_output=True,
                          text=True, timeout=kwargs.pop('timeout', 120), **kwargs).stdout


def stop(process):
    if process is None: return
    # Each child gets a new session. Kill only the owned group, including servers.
    try: os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError: return
    try: process.wait(timeout=5)
    except subprocess.TimeoutExpired: pass
    try: os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError: pass
    process.wait(timeout=5)


def recorder_binary():
    source = HERE / 'record-window.swift'
    folder = ROOT / 'build/visual-review'
    folder.mkdir(parents=True, exist_ok=True)
    binary = folder / 'record-window'
    stamp = folder / 'recorder.sha256'
    if not binary.is_file() or not stamp.is_file() or stamp.read_text() != digest(source):
        checked(['swiftc', '-parse-as-library', '-O', source, '-o', binary])
        stamp.write_text(digest(source))
    return binary


def preflight(app):
    checks = []
    for tool in ['swiftc', 'ffmpeg', 'ffprobe']:
        checks.append({'check': tool, 'ok': bool(shutil.which(tool))})
    recorder = None
    if platform.system() != 'Darwin':
        checks.append({'check': 'macOS required', 'ok': False})
        return checks, recorder
    try:
        recorder = recorder_binary()
        result = subprocess.run([str(recorder), '--preflight'], capture_output=True, text=True, timeout=10)
        permission = json.loads(result.stdout)
        checks.append({'check': 'screen-recording-permission', 'ok': permission['screenRecordingAllowed'],
                       'detail': 'Run outside the filesystem sandbox if the host already has permission'})
        checks.append({'check': 'unlocked-graphical-session', 'ok': not permission['sessionLocked'],
                       'detail': 'Unlock the Mac before native capture'})
    except Exception as error:
        checks.append({'check': 'recorder-build', 'ok': False, 'detail': str(error)})
    try:
        smoke.require_bundle(app)
        info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        if Path(info.get('LightTableProjectDir', '')).resolve() != ROOT:
            raise ValueError('Use a developer bundle built from this review checkout')
        if info.get('CFBundleIdentifier') in {'com.reville.lighttable', 'org.lighttable.LightTable'}:
            raise ValueError('Build with a distinct recorded-review bundle identifier')
        native_sources = b''.join((ROOT / path).read_bytes() for path in
            ['app/main.swift', 'app/NativePreview.swift', 'app/DiagnosticReports.swift'])
        if info.get('LightTableNativeSourceDigest') != hashlib.sha256(native_sources).hexdigest():
            raise ValueError('Native sources changed after this bundle was built; rebuild before recording')
        checks.append({'check': 'isolated-review-bundle', 'ok': True})
    except Exception as error:
        checks.append({'check': 'isolated-review-bundle', 'ok': False, 'detail': str(error)})
    return checks, recorder


def progress_records(folder, stage):
    log = folder / 'native-perf.jsonl'
    records = []
    if log.is_file():
        for line in log.read_text().splitlines():
            try:
                entry = json.loads(line)
                if entry.get('stage') == stage: records.append(entry)
            except json.JSONDecodeError: continue
    return records


def completed_steps(folder):
    return progress_records(folder, 'visual-step-end')


def capture_coverage(metadata, steps):
    start = metadata.get('firstFrameEpochMs', 0)
    end = start + metadata.get('durationSeconds', 0) * 1000
    return bool(steps and metadata.get('frames', 0) > 0 and not metadata.get('error')
                and start <= min(s['startedEpochMs'] for s in steps)
                and end >= max(s['endedEpochMs'] for s in steps))


def transient_candidates(frames, fps=60):
    """A-B-A transient changes; only review candidates, never confirmed defects."""
    import numpy as np
    candidates = []
    for i in range(1, len(frames) - 1):
        a, b, c = (np.asarray(frame, dtype=np.float32) for frame in frames[i-1:i+2])
        incoming = float(np.mean(np.abs(b - a)))
        outgoing = float(np.mean(np.abs(c - b)))
        returned = float(np.mean(np.abs(c - a)))
        if min(incoming, outgoing) > 12 and returned < min(incoming, outgoing) * .35:
            candidates.append({'seconds': i / fps, 'meanChange': round(incoming, 2),
                               'classification': 'unreviewed-transient-candidate'})
    return candidates


def analyze(folder, steps):
    import numpy as np
    movie = folder / 'window.mov'
    metadata = json.loads((folder / 'capture.json').read_text())
    origin = metadata['firstFrameEpochMs']
    probe = json.loads(checked(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', movie]))
    write_json(folder / 'video-probe.json', probe)
    metadata['encodedDurationSeconds'] = float(probe['format']['duration'])
    metadata['durationSeconds'] = min(metadata['durationSeconds'], metadata['encodedDurationSeconds'])
    # Decode continuously at 60fps; reduced grayscale is for flags, not visual proof.
    # The run cap bounds this allocation to approximately 132 MB at 600 seconds.
    decoded = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(movie), '-vf',
        'fps=60,scale=80:46,format=gray', '-f', 'rawvideo', '-'],
        check=True, capture_output=True, timeout=120).stdout
    frames = np.frombuffer(decoded, dtype=np.uint8).reshape(-1, 46, 80)
    candidates = transient_candidates(frames)
    write_json(folder / 'frame-analysis.json', {'method': 'A-B-A mean absolute grayscale difference; threshold 12/255, return ratio <0.35',
        'limitations': 'Candidates only. Resampling cannot reveal uncaptured frames. Not a stretching detector or a visual pass.',
        'decodedFrames': len(frames), 'candidates': candidates})
    assets = folder / 'evidence'; assets.mkdir(exist_ok=True)
    for journey in dict.fromkeys(s['journey'] for s in steps):
        group = [s for s in steps if s['journey'] == journey]
        start = max(0, (group[0]['startedEpochMs'] - origin) / 1000 - .3)
        end = (group[-1]['endedEpochMs'] - origin) / 1000 + .3
        checked(['ffmpeg', '-v', 'error', '-y', '-ss', start, '-i', movie, '-t', end-start,
                 '-an', '-c:v', 'libx264', '-crf', '18', '-pix_fmt', 'yuv420p', '-movflags', '+faststart',
                 assets / f'{journey}.mp4'])
    for i, step in enumerate(steps):
        start = (step['startedEpochMs'] - origin) / 1000
        duration = step['durationMs'] / 1000
        step['videoStartSeconds'] = round(start, 3)
        for label, offset in [('start', .05), ('middle', duration / 2), ('end', max(0, duration-.05))]:
            checked(['ffmpeg', '-v', 'error', '-y', '-ss', max(0, start+offset), '-i', movie,
                     '-frames:v', '1', assets / f'{i:02d}-{label}.png'])
    for i, candidate in enumerate(candidates[:20]):
        checked(['ffmpeg', '-v', 'error', '-y', '-ss', max(0, candidate['seconds']-.2), '-i', movie,
                 '-t', '.5', '-an', '-c:v', 'libx264', '-crf', '16', assets / f'candidate-{i:02d}.mp4'])
    write_json(folder / 'steps.json', steps)
    return {'coverageComplete': capture_coverage(metadata, steps), 'capture': metadata,
            'transientCandidates': len(candidates), 'reviewStatus': 'not-reviewed'}


def report(folder):
    def load(name, default):
        path = folder / name
        return json.loads(path.read_text()) if path.is_file() else default
    result = load('result.json', {'status': 'NOT DONE'})
    steps = load('steps.json', [])
    review = load('review.json', {'reviewStatus': 'not-reviewed', 'findings': []})
    refs = load('references.json', [])
    analysis = load('frame-analysis.json', {})
    bursts = load('bursts.json', [])
    esc = lambda x: html.escape(str(x), quote=True)
    parts = [f'<p class="label">LightTable · Recorded journey review</p><h1>{esc(result.get("status", "NOT DONE"))}</h1>',
             '<p>Functional assertions, capture coverage and visual inspection are reported separately.</p>',
             f'<p>Visual review: <strong>{esc(review.get("reviewStatus", "not-reviewed"))}</strong></p>']
    if review.get('summary'): parts.append(f'<p>{esc(review["summary"])}</p>')
    if result.get('error'): parts.append(f'<p><strong>Blocker:</strong> {esc(result["error"])}</p>')
    parts.append('<h2>Run evidence</h2><ul>')
    for name in ['setup.json', 'preflight.json', 'provenance.json', 'result.json', 'capture.json', 'frame-analysis.json', 'review.json', 'steps.json', 'bursts.json', 'window.mov']:
        if (folder / name).is_file(): parts.append(f'<li><a href="{name}">{name}</a></li>')
    parts.append('</ul>')
    if bursts:
        parts.append('<h2>Rapid input bursts</h2><p>Measured dispatch time for repeated UI commands or DOM inputs. This measures input dispatch, not completed rendering.</p><table><tr><th>Sequence</th><th>Inputs</th><th>Dispatch time</th></tr>')
        for burst in bursts:
            parts.append(f'<tr><td>{esc(burst["name"])}</td><td>{esc(burst["count"])}</td><td>{esc(burst["dispatchMs"])} ms</td></tr>')
        parts.append('</table>')
    if result.get('previous'):
        previous = Path(result['previous'])
        parts.append(f'<p>Previous run: <a href="{esc(os.path.relpath(previous / "index.html", folder))}">open report</a>. '
                     f'Comparable setup: {esc(result.get("previousComparable", False))}. This does not approve a baseline.</p>')
    for journey in ['browse', 'zoom', 'edit', 'explore']:
        clip = folder / 'evidence' / f'{journey}.mp4'
        parts.append(f'<h2>{journey.title()}</h2>')
        if clip.is_file():
            parts.append(f'<video controls preload="metadata" src="evidence/{journey}.mp4"></video>')
            previous_clip = Path(result.get('previous', '/nonexistent')) / 'evidence' / f'{journey}.mp4'
            if previous_clip.is_file():
                parts.append(f'<p>Previous recording</p><video controls preload="metadata" src="{esc(os.path.relpath(previous_clip, folder))}"></video>')
        else: parts.append('<p>Video: NOT DONE.</p>')
        parts.append('<table><tr><th>Action</th><th>Functional check</th><th>Time in full video</th></tr>')
        for step in steps:
            if step['journey'] == journey:
                parts.append(f'<tr><td>{esc(step["name"])}</td><td>{esc(step["status"])}</td><td>{esc(step.get("videoStartSeconds", "unavailable"))}</td></tr>')
        parts.append('</table>')
        for i, step in enumerate(steps):
            if step['journey'] != journey: continue
            images = [f'evidence/{i:02d}-{label}.png' for label in ['start', 'middle', 'end']]
            if all((folder / name).is_file() for name in images):
                parts.append(f'<details><summary>{esc(step["name"])} — three frame samples</summary><div class="frames">')
                for name, label in zip(images, ['Start', 'Middle', 'End']):
                    parts.append(f'<a href="{name}"><img loading="lazy" src="{name}" alt="{esc(step["name"])}: {label}"><span>{label}</span></a>')
                parts.append('</div></details>')
    parts.append('<h2>Transient candidates</h2><p>Automated flags for inspection, not confirmed product bugs.</p>')
    for i, candidate in enumerate(analysis.get('candidates', [])[:20]):
        clip = f'evidence/candidate-{i:02d}.mp4'
        if (folder / clip).is_file():
            parts.append(f'<p>Full recording: {esc(candidate["seconds"])} seconds</p><video controls preload="metadata" src="{clip}"></video>')
    if not analysis: parts.append('<p>NOT DONE — no analyzable recording.</p>')
    elif not analysis.get('candidates'): parts.append('<p>No transient candidates at the configured threshold. Visual review is still required.</p>')
    parts.append('<h2>Findings</h2>')
    if not review.get('findings'): parts.append('<p>No reviewed findings recorded. This is not a claim that the app has no issues.</p>')
    for finding in review.get('findings', []):
        parts.append(f'<article><h3>{esc(finding.get("title", "Finding"))}</h3><p>{esc(finding.get("classification"))} · {esc(finding.get("confidence"))}</p><p>{esc(finding.get("journey", ""))} · {esc(finding.get("timestampSeconds", ""))} seconds · {esc(finding.get("impact", ""))}</p><p>{esc(finding.get("description", ""))}</p><p>{esc(finding.get("reproduction", ""))}</p>')
        evidence = finding.get('evidence')
        if isinstance(evidence, str):
            target = (folder / evidence).resolve()
            if target.is_relative_to(folder.resolve()) and target.is_file():
                parts.append(f'<p><a href="{esc(evidence)}">Finding evidence</a></p>')
        parts.append('</article>')
    parts.append('<h2>Reference comparisons</h2>')
    if not refs: parts.append('<p>NOT DONE — no reference footage has been attached and reviewed.</p>')
    for ref in refs:
        url = str(ref.get('url', ''))
        if not url.startswith(('https://', 'http://')): continue
        parts.append(f'<p><a href="{esc(url)}">{esc(ref.get("title", "Reference"))}</a> · {esc(ref.get("journey"))} · {esc(ref.get("purpose"))}</p><p>{esc(ref.get("notes", ""))}</p>')
    parts.append('<h2>Coverage limits</h2><p>Only the actions listed in this report ran. Scripted UI commands and DOM events do not prove OS input routing. Preset application, RAW decoding, crop geometry edits, mask creation, window resizing, export and restart recovery are not covered.</p><p>Frame-change flags require review. Sparse extracted frames cannot rule out brief flicker. Capture settings and dropped frames affect what can be concluded.</p>')
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>LightTable journey review</title><style>
:root{--ink:#1a1a1a;--muted:#55575c;--line:#e5e5e5}*{box-sizing:border-box}body{margin:0;background:#fff;color:var(--ink);font:16px/1.6 Inter,system-ui,sans-serif}main{max-width:1120px;margin:auto;padding:56px 24px 96px}h1,h2,h3,.label{font-family:"Space Mono",ui-monospace,monospace}h1{font-size:40px}h2{font-size:22px;margin-top:56px;border-bottom:1px solid var(--line);padding-bottom:16px}.label{font-size:13px;letter-spacing:.22em;text-transform:uppercase;color:var(--muted)}a{color:#1e40af}video{width:100%;background:#111}table{width:100%;border-collapse:collapse;font-size:15px}td,th{text-align:left;border-bottom:1px solid var(--line);padding:12px}article{border:1px solid var(--line);padding:20px;margin:24px 0}p{max-width:76ch}.frames{display:flex;gap:12px;margin:16px 0}.frames a{flex:1;min-width:0;font-size:13px}.frames img{width:100%;height:auto}summary{cursor:pointer;padding:12px 0}@media(max-width:600px){.frames{display:block}h1{font-size:30px}}</style><main>'''+''.join(parts)+'</main></html>'
    (folder / 'index.html').write_text(page)
    return folder / 'index.html'


def run(args):
    folder = args.output.resolve() if args.output else ROOT / 'output/visual-reviews' / time.strftime('%Y%m%d-%H%M%S')
    folder.mkdir(parents=True, exist_ok=False)
    result = {'status': 'NOT DONE', 'functionalStatus': 'not-run', 'visualStatus': 'not-reviewed', 'run': str(folder)}
    write_json(folder / 'setup.json', {'sourceRevision': checked(['git', 'rev-parse', 'HEAD'], cwd=ROOT).strip(),
        'app': str(args.app), 'foregroundAuthorized': args.allow_foreground, 'timeoutSeconds': args.timeout})
    app_process = recording = None
    logs = []
    try:
        checks, recorder = preflight(args.app)
        write_json(folder / 'preflight.json', checks)
        if not all(c['ok'] for c in checks): raise RuntimeError('Preflight failed: ' + '; '.join(c['check']+': '+c.get('detail', 'unavailable') for c in checks if not c['ok']))
        if not args.allow_foreground: raise RuntimeError('Foreground testing is not authorized; pass --allow-foreground only after permission')
        fixtures = [ROOT / 'tests/fixtures/photos' / name for name in ['field.jpg', 'portrait.jpg', 'still-life.jpg']]
        smoke.validate_fixtures(tuple(fixtures), 'pr')
        state = folder / 'isolated'; photos = state / 'photos'; photos.mkdir(parents=True)
        for source in fixtures: shutil.copy2(source, photos / source.name)
        # Enough entries for a real scrolling grid; copies stay in disposable data.
        for i in range(30): shutil.copy2(fixtures[i % 3], photos / f'review-{i:02d}.jpg')
        native = args.app / 'Contents/MacOS/LightTable'
        provenance = {'sourceRevision': checked(['git', 'rev-parse', 'HEAD'], cwd=ROOT).strip(),
            'sourceDiffSha256': hashlib.sha256(checked(['git', 'diff', 'HEAD'], cwd=ROOT).encode()).hexdigest(),
            'journeySha256': digest(ROOT / 'web/visual-journey.js'), 'nativeBinarySha256': digest(native),
            'engineBinarySha256': digest(ROOT / 'rust-engine/target/release/lighttable-engine'),
            'fixtureHashes': {p.name: digest(p) for p in fixtures}, 'requestedFps': 60,
            'captureSize': 'window logical size, even pixels', 'machine': platform.platform(),
            'app': str(args.app), 'sourceFiles': {str(p.relative_to(ROOT)): digest(p) for p in [ROOT/'app/main.swift', ROOT/'web/app.js', ROOT/'web/visual-journey.js', ROOT/'web/render-scheduler.js', ROOT/'web/zoom-motion.js', HERE/'run.py', HERE/'record-window.swift']}}
        write_json(folder / 'provenance.json', provenance)
        if args.previous:
            prior = json.loads((args.previous / 'provenance.json').read_text())
            result['previous'] = str(args.previous.resolve())
            result['previousComparable'] = all(prior.get(k) == provenance[k] for k in ['journeySha256', 'fixtureHashes', 'requestedFps', 'captureSize', 'machine'])
        gate = folder / 'recording.ready'; stop_file = folder / 'recording.stop'
        env = smoke.smoke_environment(state, photos, folder / 'benchmark.json', folder / 'unused.png', 'visual-review')
        env.pop('LIGHTTABLE_NATIVE_JOURNEY_SCREENSHOT', None)
        env['LIGHTTABLE_RECORDING_READY'] = str(gate)
        env['LIGHTTABLE_NATIVE_BENCHMARK_QUIT'] = '0'
        env['LIGHTTABLE_NATIVE_PERF_LOG'] = str(folder / 'native-perf.jsonl')
        env['LIGHTTABLE_SERVER_LOG'] = str(folder / 'server.log')
        env['LIGHTTABLE_PREFS_FILE'] = str(state / 'prefs.json')
        write_json(state / 'prefs.json', {'smoothZoom': True, 'allowAutomation': True, 'autoAdvance': False})
        app_log = (folder / 'app.log').open('w'); logs.append(app_log)
        app_process = subprocess.Popen([str(native)], cwd=args.app.parent, env=env,
            stdout=app_log, stderr=subprocess.STDOUT, start_new_session=True)
        result['appPid'] = app_process.pid
        record_log = (folder / 'recorder.log').open('w'); logs.append(record_log)
        recording = subprocess.Popen([str(recorder), str(app_process.pid), str(folder / 'window.mov'),
            str(gate), str(stop_file), str(folder / 'capture.json'), str(args.timeout)],
            stdout=record_log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline and app_process.poll() is None and not (folder / 'benchmark.json').is_file():
            if recording.poll() is not None: raise RuntimeError('Recorder exited before the native journey finished; inspect recorder.log')
            time.sleep(.2)
        if not (folder / 'benchmark.json').is_file(): raise RuntimeError(f'Native review did not finish within {args.timeout}s (exit={app_process.poll()})')
        # Capture a short tail, then finish the movie before analyzing it.
        time.sleep(.5)
        stop_file.write_text('stop')
        recording.wait(timeout=20)
        if recording.returncode: raise RuntimeError('Recording did not finalize successfully')
        payload = json.loads((folder / 'benchmark.json').read_text())
        steps = completed_steps(folder); write_json(folder / 'steps.json', steps)
        result.update(analyze(folder, steps))
        if payload.get('error'):
            result['functionalStatus'] = 'failed'
            raise RuntimeError(payload['error'])
        if not steps or any(s['status'] != 'passed' for s in steps):
            result['functionalStatus'] = 'failed'
            raise RuntimeError('Functional journey incomplete: ' + '; '.join(s['name'] + ': ' + s.get('error', s['status']) for s in steps if s['status'] != 'passed'))
        if {s['journey'] for s in steps} != {'browse', 'zoom', 'edit', 'explore'}: raise RuntimeError('Required journeys missing')
        if not result['coverageComplete']: raise RuntimeError('Video did not cover every journey step')
        if any(digest(ROOT / name) != sha for name, sha in provenance['sourceFiles'].items()):
            raise RuntimeError('Source files changed during the recorded review')
        result['functionalStatus'] = 'passed'
        result['status'] = 'RECORDED — visual review pending'
    except Exception as error:
        result['error'] = str(error)
    finally:
        if recording and recording.poll() is None:
            (folder / 'recording.stop').write_text('stop')
            try: recording.wait(timeout=15)
            except subprocess.TimeoutExpired: pass
        stop(recording); stop(app_process)
        for log in logs: log.close()
        steps = completed_steps(folder)
        if steps and (folder / 'capture.json').is_file() and (folder / 'window.mov').is_file() and not (folder / 'frame-analysis.json').exists():
            try: result.update(analyze(folder, steps))
            except Exception as error: result['partialAnalysisError'] = str(error)
        if not (folder / 'steps.json').exists(): write_json(folder / 'steps.json', steps)
        write_json(folder / 'bursts.json', progress_records(folder, 'visual-burst'))
        write_json(folder / 'result.json', result)
        write_json(folder / 'review.json', {'reviewStatus': 'not-reviewed', 'findings': []})
        report(folder)
    print(json.dumps(result, indent=2))
    return 0 if result['functionalStatus'] == 'passed' else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=ROOT / 'build/LightTable.app')
    parser.add_argument('--preflight', action='store_true')
    parser.add_argument('--allow-foreground', action='store_true')
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--previous', type=Path)
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    args.app = args.app.expanduser().resolve()
    if not 30 <= args.timeout <= 600: parser.error('--timeout must be between 30 and 600 seconds')
    if args.report: print(report(args.report.resolve())); return
    if args.preflight:
        checks, _ = preflight(args.app); print(json.dumps(checks, indent=2))
        sys.exit(0 if all(c['ok'] for c in checks) else 2)
    sys.exit(run(args))


if __name__ == '__main__': main()
