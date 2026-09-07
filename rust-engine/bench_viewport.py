#!/usr/bin/env python3
"""Verify every viewport float against the same full-frame GPU render.

Covers panning, frame boundaries, every quarter rotation, film/grain pitch,
scanner kernels, stochastic glare, and lattice-aligned camera/print diffusion.
"""
import argparse
import array
import copy
import json
import os
from pathlib import Path
import statistics
import struct
import subprocess
import tempfile

from bench_resident_cache import fixture, invoke


def read_float_tiff(path):
    data = path.read_bytes()
    endian = '<' if data[:2] == b'II' else '>'
    assert struct.unpack_from(endian+'H', data, 2)[0] == 42
    offset = struct.unpack_from(endian+'I', data, 4)[0]
    count = struct.unpack_from(endian+'H', data, offset)[0]
    tags = {}
    for i in range(count):
        at = offset+2+i*12
        tag, kind, n, location = struct.unpack_from(endian+'HHII', data, at)
        if kind not in (3, 4):
            continue
        size, code = (2, 'H') if kind == 3 else (4, 'I')
        start = at+8 if n*size <= 4 else location
        tags[tag] = struct.unpack_from(endian+code*n, data, start)
    assert tags[259] == (1,), 'benchmark needs uncompressed TIFF'
    assert tags[258] == (32, 32, 32)
    assert tags[339] == (3, 3, 3)
    raw = b''.join(data[start:start+n] for start, n in zip(tags[273], tags[279]))
    values = array.array('f')
    values.frombytes(raw)
    if (endian == '<') != (struct.pack('=I', 1) == struct.pack('<I', 1)):
        values.byteswap()
    return tags[256][0], tags[257][0], values


def crop(values, width, rect):
    out = array.array('f')
    for y in range(rect['y'], rect['y']+rect['height']):
        start = (y*width+rect['x'])*3
        out.extend(values[start:start+rect['width']*3])
    return out


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',type=Path,default=Path(__file__).parent/'target/release/lighttable-engine')
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--width',type=int,default=1100)
    parser.add_argument('--viewport-width',type=int,default=240)
    parser.add_argument('--viewport-height',type=int,default=160)
    parser.add_argument('--case',action='append',dest='cases',
                        help='run only the named case (repeatable)')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    records=[]
    with tempfile.TemporaryDirectory(prefix='lighttable-viewport-') as tmp:
        directory=Path(tmp)
        source=directory/'input.tiff'
        fixture(source,args.width,args.width*2//3)
        with (directory/'engine.log').open('w') as log:
            process=subprocess.Popen([str(args.binary.resolve())],text=True,stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=log,env=dict(os.environ,SPEKTRAFILM_BACKEND='wgpu'))
            try:
                base={'io':{'input_color_space':'ProPhoto RGB','input_cctf_decoding':False},
                      'film_render':{'grain':{'active':True},'halation':{'active':True}}}
                cases=[('default',base)]
                spatial=copy.deepcopy(base)
                spatial.update(camera={'lens_blur_um':8.0},scanner={'lens_blur':1.3,'unsharp_mask':[2.1,0.8]},
                               print_render={'glare':{'active':True,'percent':2.0,'roughness':0.2,'blur':3.0}})
                cases.append(('spatial',spatial))
                diffusion=copy.deepcopy(spatial)
                diffusion['camera']['diffusion_filter']={'active':True,'strength':0.5}
                cases.append(('camera_diffusion',diffusion))
                for family in ('glimmerglass', 'pro_mist', 'cinebloom'):
                    filtered=copy.deepcopy(spatial)
                    filtered['camera']['diffusion_filter']={
                        'active':True,'strength':0.5,'filter_family':family,'spatial_scale':0.3}
                    cases.append((family,filtered))
                both=copy.deepcopy(diffusion)
                both['camera']['diffusion_filter']['spatial_scale']=0.3
                both['enlarger']={'diffusion_filter':{
                    'active':True,'strength':0.25,'spatial_scale':0.2}}
                cases.append(('camera_and_print_diffusion',both))
                print_only=copy.deepcopy(spatial)
                print_only['enlarger']={'diffusion_filter':{'active':True,'strength':0.5}}
                cases.append(('print_diffusion',print_only))
                noop=copy.deepcopy(spatial)
                noop['camera']['diffusion_filter']={'active':True,'strength':0.0}
                cases.append(('zero_strength_diffusion',noop))
                if args.cases:
                    unknown=set(args.cases)-{case for case,_ in cases}
                    if unknown: parser.error('unknown cases: '+', '.join(sorted(unknown)))
                    cases=[case for case in cases if case[0] in args.cases]
                for case,params in cases:
                    for rotation in range(4):
                        request=dict(id=len(records)+1,command='render',input=str(source),
                            input_cache_key='viewport-fixture',data_dir=str(args.data.resolve()),
                            film='kodak_portra_400',paper='kodak_endura_premier',params=params,
                            bit_depth=32,rotate_quarters_ccw=rotation)
                        full=directory/'full.tiff'
                        baseline=invoke(process,dict(request,output=str(full)))
                        if 'wgpu' not in baseline['backend'].lower():
                            raise RuntimeError('GPU path NOT DONE: '+baseline['backend'])
                        width,height,pixels=read_float_tiff(full)
                        tw,th=min(args.viewport_width,width//3),min(args.viewport_height,height//3)
                        rects=[dict(x=width//3,y=height//3,width=tw,height=th),
                               dict(x=0,y=0,width=tw,height=th),
                               dict(x=width-tw,y=height-th,width=tw,height=th),
                               dict(x=width//3+17,y=height//3+11,width=tw,height=th)]
                        for rect in rects:
                            output=directory/'viewport.tiff'
                            result=invoke(process,dict(request,viewport=rect,output=str(output)))
                            w,h,actual=read_float_tiff(output)
                            expected=crop(pixels,width,rect)
                            assert (w,h)==(tw,th),result
                            assert (result['full_width'],result['full_height'])==(width,height),result
                            assert actual==expected, (case,rotation,rect,
                                max(abs(a-b) for a,b in zip(actual,expected)))
                            if case in ('default','spatial','zero_strength_diffusion'):
                                assert result['viewport_accelerated'], result
                            result.update(case=case,rotation=rotation,viewport=rect,bit_identical=True,
                                          full_render_ms=baseline['render_ms'])
                            records.append(result)
            finally:
                process.stdin.close()
                try: process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill();process.wait()
    accelerated=[r for r in records if r['viewport_accelerated']]
    summary=dict(cases=len(records),bit_identical=True,
                 accelerated_cases=len(accelerated),
                 full_render_ms=statistics.median(r['full_render_ms'] for r in accelerated),
                 viewport_render_ms=statistics.median(r['render_ms'] for r in accelerated))
    report=dict(summary=summary,results=records)
    print(json.dumps(summary,indent=2))
    if args.output: args.output.write_text(json.dumps(report,indent=2)+'\n')


if __name__=='__main__': main()
