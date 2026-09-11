#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Optional, bounded headless UI audits. Never imported by the normal test gate."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
MASK_VERSION = 1


def write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def matrix(profile, locales=None, viewports=None, states=None):
    manifest = json.loads((ROOT / 'web/locales/manifest.json').read_text())['locales']
    selected = locales or ([x['code'] for x in manifest] if profile == 'full' else ['en', 'ar', 'de', 'ru'])
    directions = {x['code']: x['dir'] for x in manifest}
    if set(selected) - directions.keys(): raise ValueError('Unknown locale')
    panes = re.findall(r'<section\b[^>]*class="panel-pane[^"\n]*"[^>]*id="(\w+)Pane"', (ROOT / 'web/index.html').read_text())
    if not panes: raise ValueError('No application panes discovered')
    ports = viewports or (['1280x720','1440x900','1728x1117'] if profile == 'full' else ['1280x720'])
    definitions = [{'name':f'pane-{p}-{z}', 'pane':p, 'zoom':z, 'fixture':'photos', 'view':'detail'}
                   for p in panes for z in (['fit','actual','in','out'] if profile == 'full' else ['fit'])]
    definitions += [{'name':v,'view':v,'fixture':'photos'} for v in
                    ['grid','detail','compare','survey','loupe','help','settings','no-results','long-name']]
    definitions += [{'name':v,'view':v,'fixture':v} for v in ['empty','first-run','missing']]
    if states:
        names = set(states)
        if names - {d['name'] for d in definitions}: raise ValueError(f'Unknown state: {names - {d["name"] for d in definitions}}')
        definitions = [d for d in definitions if d['name'] in names]
    cases = []
    for locale in selected:
        for viewport in ports:
            if not re.fullmatch(r'\d{3,4}x\d{3,4}', viewport): raise ValueError('Viewport must be WIDTHxHEIGHT')
            w,h=map(int,viewport.split('x'))
            for d in definitions:
                cases.append({**d,'id':f'{locale}-{viewport}-{d["name"]}', 'locale':locale,
                              'direction':directions[locale],'viewport':{'width':w,'height':h}})
    return cases, panes


def compare(actual, expected, diff, threshold=16, fraction=.001):
    import numpy as np
    from PIL import Image
    a=np.asarray(Image.open(actual).convert('RGB')).astype('int16')
    b=np.asarray(Image.open(expected).convert('RGB')).astype('int16')
    if a.shape != b.shape: return {'status':'different-size','actual':list(a.shape),'expected':list(b.shape)}
    changed=np.max(np.abs(a-b),axis=2)>threshold
    ratio=float(changed.mean())
    view=(a*.25).astype('uint8');view[changed]=[255,30,90]
    Image.fromarray(view).save(diff)
    return {'status':'changed' if ratio>fraction else 'matched','changedFraction':ratio,
            'pixelThreshold':threshold,'allowedFraction':fraction,'diff':diff.name}


def stop(process):
    if process is None: return
    if os.name == 'posix':
        try: os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError: return
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired: pass
        try: os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError: pass
    elif process.poll() is None: process.kill()
    process.wait(timeout=5)


def frames_analysis(folder):
    import numpy as np
    from PIL import Image
    spec=importlib.util.spec_from_file_location('native_review',ROOT/'scripts/visual-review/run.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    result=[]
    for file in folder.glob('*-frames.json'):
        capture=json.loads(file.read_text());records=capture['frames']
        frames=[np.asarray(Image.open(folder/r['file']).convert('L').resize((80,46))) for r in records]
        candidates=module.transient_candidates(frames,fps=1)
        for hit in candidates:
            i=int(hit.pop('seconds'));hit.update(timestamp=records[i]['timestamp'],
                evidence=[r['file'] for r in records[i-1:i+2]])
        result.append({'capture':file.name,'frames':len(frames),'capped':capture['capped'],'candidates':candidates,
                       'limitation':'Actual CDP frames and timestamps; not guaranteed cadence or native Metal proof.'})
    write(folder/'frame-analysis.json',result)
    return result


def report(folder, result):
    lines=['# LightTable on-demand UI audit', '',
           f'Status: **{result["status"]}**. Visual review: **not-reviewed**.', '',
           'Open every candidate screenshot before classification. Browser evidence does not verify Metal or macOS input.', '',
           '[Review ledger](review.json) · [Machine-readable result](result.json) · [Provenance](provenance.json)', '',
           '| State | Execution | Candidates | Evidence | Baseline |', '|---|---|---:|---|---|']
    for r in result['records']:
        screenshot=r.get('screenshot')
        evidence=f'[screenshot]({screenshot})' if screenshot else 'NOT DONE'
        if r.get('minimal'): evidence+=f' · [replay]({r["minimal"]})'
        lines.append(f'| {r["id"]} | {r["status"]} | {len(r.get("candidates",[]))+len(r.get("initialCandidates",[]))} | {evidence} | {r.get("baseline",{}).get("status","not compared")} |')
    (folder/'README.md').write_text('\n'.join(lines)+'\n')


def approve(source, destination, reviewer):
    review=json.loads((source/'review.json').read_text());result=json.loads((source/'result.json').read_text())
    provenance=json.loads((source/'provenance.json').read_text())
    if provenance['mode']!='snapshot' or result['executionStatus']!='passed': raise ValueError('Only complete snapshot runs can become baselines')
    if not reviewer or review.get('reviewStatus')!='reviewed': raise ValueError('An explicit reviewer and reviewed ledger are required')
    required={r[k] for r in result['records'] for k in ['screenshot','masked']}
    if not required.issubset(set(review.get('inspectedEvidence',[]))): raise ValueError('Ledger must list every original and masked screenshot in inspectedEvidence')
    if any(f.get('classification') in ['confirmed-bug','suspected-bug'] for f in review.get('findings',[])):
        raise ValueError('Resolve bug findings before baseline approval')
    destination.mkdir(parents=True,exist_ok=False)
    for name in required: shutil.copy2(source/name,destination/name)
    write(destination/'baseline.json',{'schema':1,'approvedBy':reviewer,'approvedAt':time.time(),
          'comparison':provenance['comparison'],'source':str(source),'images':{r['id']:{'file':r['masked'],'sha256':digest(source/r['masked'])} for r in result['records']}})


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',nargs='?',choices=['snapshot','invariants','explore'],default='invariants')
    p.add_argument('--profile',choices=['quick','full'],default='quick')
    p.add_argument('--locales',nargs='+');p.add_argument('--viewports',nargs='+');p.add_argument('--states',nargs='+')
    p.add_argument('--list',action='store_true');p.add_argument('--output',type=Path)
    p.add_argument('--rules',nargs='+',choices=['horizontal-overflow','document-direction','hidden-paints','zero-size-control','positive-tab-order','focus-in-aria-hidden','control-text-overflow','overlapping-controls','clipped-text','text-contrast','viewport-clipping','fit-geometry'])
    p.add_argument('--timeout',type=int,default=600);p.add_argument('--steps',type=int,default=40)
    p.add_argument('--seed',type=int,default=20260909);p.add_argument('--minimize-attempts',type=int,default=12)
    p.add_argument('--max-frames',type=int,default=600);p.add_argument('--baseline',type=Path)
    p.add_argument('--replay',type=Path);p.add_argument('--approve-from',type=Path);p.add_argument('--reviewer')
    args=p.parse_args()
    if args.approve_from:
        if not args.baseline: p.error('--approve-from requires --baseline (new directory)')
        approve(args.approve_from.resolve(),args.baseline.resolve(),args.reviewer);return 0
    if not 1<=args.timeout<=14400 or not 1<=args.steps<=10000 or not 0<=args.minimize_attempts<=100 or not 1<=args.max_frames<=10000:
        p.error('Bounds: timeout 1..14400 seconds, steps 1..10000, minimize-attempts 0..100, max-frames 1..10000')
    cases,panes=matrix(args.profile,args.locales,args.viewports,args.states)
    replay=json.loads(args.replay.read_text()) if args.replay else None
    if replay: args.mode='explore';args.seed=replay.get('seed',args.seed);cases=[replay['test']]
    elif args.mode=='explore': cases=[c for c in cases if c['name']=='detail']
    if not cases: p.error('No selected cases; explore requires the detail state')
    if args.list: print(json.dumps({'count':len(cases),'panes':panes,'cases':cases},indent=2));return 0
    folder=(args.output or ROOT/'output/ui-audits'/time.strftime('%Y%m%d-%H%M%S')).resolve()
    folder.mkdir(parents=True,exist_ok=False)
    started=time.monotonic();deadline=started+args.timeout
    result={'schema':1,'status':'NOT DONE','executionStatus':'failed','records':[],'errors':[],'expectedCases':len(cases)}
    provenance={'schema':1,'mode':args.mode,'seed':args.seed,'cases':cases,'bounds':vars(args)|{'output':str(folder)}}
    provenance['bounds']={k:str(v) if isinstance(v,Path) else v for k,v in provenance['bounds'].items()}
    try:
        baseline=json.loads((args.baseline/'baseline.json').read_text()) if args.baseline else None
        from bench.benchmark import server_environment,find_playwright_module,find_playwright_browser
        from PIL import Image
        import preset_library
        try: preset_library.builtin_presets()
        except Exception as error: raise RuntimeError(f'Application dependency preflight failed: {error}. Follow CONTRIBUTING.md source setup.') from error
        module=find_playwright_module();node=shutil.which('node')
        if not module or not node: raise RuntimeError('Requires Node and Playwright; see scripts/ui-audit/README.md')
        browser=os.environ.get('LIGHTTABLE_BROWSER_EXECUTABLE') or find_playwright_browser()
        provenance.update(revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
          dirty=bool(subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True)),
          sourceHashes={str(f.relative_to(ROOT)):digest(f) for f in [*sorted((ROOT/'web').rglob('*')), *sorted(HERE.glob('*'))] if f.is_file() and '__pycache__' not in f.parts},
          comparison={'platform':platform.platform(),'machine':platform.machine(),'maskVersion':MASK_VERSION,'deviceScaleFactor':1,
            'fixtureHashes':{f.name:digest(f) for f in sorted((ROOT/'tests/fixtures/photos').glob('*.jpg'))}})
        write(folder/'provenance.json',provenance)
        for fixture in dict.fromkeys(c['fixture'] for c in cases):
            server=worker=None
            try:
                remaining=deadline-time.monotonic()
                if remaining<=0: raise TimeoutError('Audit wall-clock limit reached')
                state_dir=folder/f'runtime-{fixture}';photos=state_dir/'photos';photos.mkdir(parents=True)
                if fixture in ['photos','missing']:
                    for source in sorted((ROOT/'tests/fixtures/photos').glob('*.jpg')):
                        with Image.open(source) as im:
                            im.thumbnail((1200,1200));im.save(photos/source.name)
                    shutil.copy2(photos/'field.jpg',photos/('Long-'+('filename-Deutsch-Русский-العربية-'*3)+'.jpg'))
                with socket.socket() as sock: sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                env=server_environment(photos,port,state_dir/'cache')
                owned={'LIGHTTABLE_DIR','LIGHTTABLE_PORT','LIGHTTABLE_CACHE_DIR','LIGHTTABLE_PREFS_FILE','LIGHTTABLE_CATALOG_FILE','LIGHTTABLE_CATALOG_MIRROR'}
                env={k:v for k,v in env.items() if not k.startswith('LIGHTTABLE_') or k in owned}
                for key,value in {'LIGHTTABLE_HEADLESS':'1','LIGHTTABLE_PRESETS_FILE':str(state_dir/'presets.json'),
                    'LIGHTTABLE_INSTANCE_DIR':str(state_dir/'instances'),'LIGHTTABLE_AI_DIR':str(state_dir/'ai'),
                    'LIGHTTABLE_PROFILE_ROOT':str(state_dir/'profiles'),'LIGHTTABLE_SERVER_LOG':str(state_dir/'server.log'),
                    'MPLCONFIGDIR':str(state_dir/'mpl')}.items(): env[key]=value
                base=f'http://127.0.0.1:{port}'
                with (state_dir/'stdout.log').open('w') as log:
                    server=subprocess.Popen([sys.executable,str(ROOT/'server.py')],cwd=ROOT,env=env,
                                            stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                until=min(deadline,time.monotonic()+60)
                while time.monotonic()<until:
                    if server.poll() is not None: raise RuntimeError(f'Isolated server exited: {state_dir}/stdout.log')
                    try:
                        with urllib.request.urlopen(base+'/api/images',timeout=2) as r: payload=json.load(r)
                        if len(payload.get('images',[]))==len(list(photos.glob('*.jpg'))): break
                    except (OSError,ValueError): pass
                    time.sleep(.1)
                else: raise TimeoutError('Isolated server startup timed out')
                if fixture=='missing':
                    for f in photos.glob('*.jpg'): f.rename(f.with_suffix('.missing'))
                config={'module':str(module),'browser':str(browser) if browser else None,'baseUrl':base,
                  'output':str(folder),'result':str(folder/f'{fixture}-results.json'),'fixture':fixture,
                  'mode':args.mode,'cases':[c for c in cases if c['fixture']==fixture],'panes':panes,
                  'timeout':max(1,deadline-time.monotonic()),'steps':args.steps,'seed':args.seed,
                  'minimizeAttempts':args.minimize_attempts,'maxFrames':args.max_frames,'replay':replay,
                  'rules':replay.get('rules') if replay else args.rules}
                config_file=folder/f'{fixture}-config.json';write(config_file,config)
                with (folder/f'{fixture}-browser.log').open('w') as log:
                    worker=subprocess.Popen([node,str(HERE/'browser.mjs'),str(config_file)],cwd=ROOT,
                                            stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                    worker.wait(timeout=max(.1,deadline-time.monotonic()))
                if worker.returncode: result['errors'].append(f'{fixture} browser exited {worker.returncode}; see browser log')
            finally:
                stop(worker);stop(server)
                file=folder/f'{fixture}-results.json'
                if file.exists():
                    partial=json.loads(file.read_text());result['records']+=partial['records'];result['errors']+=partial['errors']
                    provenance['comparison']['browserVersion']=partial['browserVersion']
            print(f'{fixture}: {len(result["records"])}/{len(cases)} states recorded',flush=True)
        for r in result['records']:
            if args.mode!='snapshot': continue
            key=(baseline or {}).get('images',{}).get(r['id'])
            if baseline and baseline['comparison']!=provenance['comparison']: r['baseline']={'status':'incompatible'}
            elif key and digest(args.baseline/key['file'])!=key['sha256']: r['baseline']={'status':'corrupt-baseline'}
            elif key and r.get('masked'): r['baseline']=compare(folder/r['masked'],args.baseline/key['file'],folder/f'{r["id"]}-diff.png')
            else: r['baseline']={'status':'needs-approval'}
        result['frameAnalysis']=frames_analysis(folder)
        if any(f['frames']==0 for f in result['frameAnalysis']): result['errors'].append('No screencast frames captured')
        complete=len(result['records'])==len(cases) and not result['errors'] and all(r['status']=='passed' for r in result['records'])
        result['executionStatus']='passed' if complete else 'failed'
        candidates=any(r.get('candidates') or r.get('initialCandidates') or r.get('baseline',{}).get('status','matched')!='matched' for r in result['records']) or any(f['candidates'] or f['capped'] for f in result['frameAnalysis'])
        result['status']=('REVIEW REQUIRED' if candidates else 'CHECKS PASSED — visual review pending') if complete else 'NOT DONE'
    except (Exception,KeyboardInterrupt) as error: result['errors'].append(str(error))
    finally:
        result['durationSeconds']=round(time.monotonic()-started,2)
        write(folder/'provenance.json',provenance);write(folder/'result.json',result)
        write(folder/'review.json',{'reviewStatus':'not-reviewed','inspectedEvidence':[],'findings':[]})
        report(folder,result)
    print(f'{result["status"]}: {folder}/README.md')
    return 2 if result['executionStatus']!='passed' else (1 if result['status']=='REVIEW REQUIRED' else 0)

if __name__=='__main__':
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    raise SystemExit(main())
