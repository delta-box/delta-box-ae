#!/usr/bin/env python3
"""Build a private, version-pinned CRIU with opt-in exact parent page comparison.

Does not install or replace system CRIU. --source must be an existing clone of
upstream CRIU containing SOURCE_COMMIT; --out must not exist.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

SOURCE_COMMIT = '2cf8f13ca1f11a0491977e438b262e646137256c'
CAPABILITY = 'exact-parent-v1 exact-parent-lazy-v1'

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True,type=Path)
    p.add_argument('--out',required=True,type=Path)
    p.add_argument('--jobs',type=int,default=2)
    args=p.parse_args()
    if args.jobs < 1: p.error('--jobs must be positive')
    source=args.source.resolve(); out=args.out.resolve()
    if out.exists(): p.error('--out already exists; refusing to overwrite')
    patch=Path(__file__).with_name('criu-2cf8f13ca-exact-parent.patch')
    report={'schema_version':1,'source_commit':SOURCE_COMMIT,'source_repository':'https://github.com/checkpoint-restore/criu.git',
            'patch_sha256':digest(patch),'started_unix':time.time(),'affinity':sorted(os.sched_getaffinity(0)), 'passed':False}
    out.mkdir(parents=True); build=out/'source'
    log=(out/'build.log').open('w')
    def run(cmd,**kwargs):
        return subprocess.run(cmd,check=True,stdout=log,stderr=subprocess.STDOUT,**kwargs)
    try:
        run(['git','clone','--no-hardlinks','--no-checkout',str(source),str(build)])
        run(['git','checkout','--detach',SOURCE_COMMIT],cwd=build)
        report['source_tree']=subprocess.check_output(['git','rev-parse','HEAD^{tree}'],cwd=build,text=True).strip()
        run(['git','apply','--check',str(patch)],cwd=build)
        run(['git','apply',str(patch)],cwd=build)
        run(['make',f'-j{args.jobs}','criu'],cwd=build)
        binary=build/'criu/criu'
        env=dict(os.environ,DELTABOX_CRIU_CAPABILITIES='1')
        version=subprocess.check_output([str(binary),'--version'],text=True,env=env)
        if f'DeltaBox capabilities: {CAPABILITY}' not in version:
            raise RuntimeError('built CRIU did not advertise exact-parent-v1')
        report.update(binary=str(binary),binary_sha256=digest(binary),version=version,passed=True)
    except Exception as e:
        report['error']=repr(e)
        raise
    finally:
        report['finished_unix']=time.time();log.close()
        (out/'build.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))

if __name__=='__main__':main()
