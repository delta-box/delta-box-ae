#!/usr/bin/env python3
"""Prepare, plan, run and analyze the DeltaBox paper's CPU experiments."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release.lock import from_environment

from repro.common import host_state, AE_ROOT, REPO_ROOT, configured_path, configured_value, file_record, load_config, public_config, repository_state, write_json
from repro.catalog import EXPERIMENTS, SKIPPED, DERIVED, build_jobs
from repro.fanout_sdk import fanout_python, probe_e2b_sdk
from repro.process import execute


def doctor(config, experiments):
    checks=[]
    def check(name,ok,detail):checks.append({'name':name,'ok':bool(ok),'detail':str(detail)})
    needs_vm=any(e.startswith(('table-02-deltabox','table-03','figure-06','figure-09')) or e in ('figure-08-deltabox','correctness') for e in experiments)
    needs_payload=any(e.startswith(('table-02-','figure-02')) and e!='table-02-deltabox' or e in ('figure-09','figure-01-cube') for e in experiments)
    if any(e.startswith('figure-02-') for e in experiments):
        check('profile Linux procfs',sys.platform=='linux','RSS and soft-dirty use /proc')
    if needs_vm or any(e in ('table-02-criu','table-02-fc-diff') for e in experiments):
        check('linux',sys.platform=='linux',platform.platform())
        check('root',hasattr(os,'geteuid') and os.geteuid()==0,'Run VM/CRIU/FC experiments under sudo; doctor does not change host configuration')
    if needs_vm or 'table-02-fc-diff' in experiments:
        for name in ('firecracker','ip','unshare','mount','umount','curl','ssh','scp','xfs_info'):
            check('tool:'+name,shutil.which(name),shutil.which(name) or 'missing')
        check('kvm',os.access('/dev/kvm',os.R_OK|os.W_OK),'/dev/kvm')
        for name in ('kernel','base_xfs','images_dir'):
            try:path=configured_path(config,name);check(name,path.exists(),path)
            except ValueError as err:check(name,False,err)
    if needs_payload:
        for name in ('payload','moatless_venv'):
            try:path=configured_path(config,name);check(name,path.is_dir(),path)
            except ValueError as err:check(name,False,err)
        for name in ('git','du'):
            check('tool:'+name,shutil.which(name),shutil.which(name) or 'missing')
    if 'table-02-criu' in experiments:
        for name in ('criu','rsync'):
            check('tool:'+name,shutil.which(name),shutil.which(name) or 'missing')
    if 'table-02-fc-diff' in experiments:
        for name in ('dmsetup','losetup','blockdev','rsync','xfs_growfs'):
            check('tool:'+name,shutil.which(name),shutil.which(name) or 'missing')
    if any(e.endswith('-cube') for e in experiments):
        for name in ('cube.sdk',):
            try:path=configured_path(config,name);check(name,path.is_dir(),path)
            except ValueError as err:check(name,False,err)
        try:
            path = configured_path(config, 'cube.phase_binary')
            check('cube.phase_binary', path.is_file(), path)
        except ValueError as err:check('cube.phase_binary',False,err)
    if 'figure-01-cube' in experiments:
        for name in ('cube.phase_log',):
            try:path=configured_path(config,name);check(name,path.is_file(),path)
            except ValueError as err:check(name,False,err)
    if 'table-02-e2b' in experiments:
        mode = config.get('e2b', {}).get('execution', 'ssh')
        check('e2b.execution', mode in ('local', 'ssh'), mode)
        for name in (('e2b.infra',) if mode == 'local' else ('e2b.infra','e2b.ssh_key')):
            try:path=configured_path(config,name);check(name,path.exists(),path)
            except ValueError as err:check(name,False,err)
        fields = ['e2b.from_build', 'e2b.remote_path', 'e2b.storage',
                  'e2b.sidecar_ip' if mode == 'local' else 'e2b.ssh_host']
        for name in fields:
            try:check(name,True,configured_value(config,name))
            except ValueError as err:check(name,False,err)
        if mode == 'local':
            check('e2b local Linux', sys.platform == 'linux', 'Local E2B requires Linux/KVM')
            check('e2b local root', hasattr(os, 'geteuid') and os.geteuid() == 0,
                  'Local E2B requires root for network and mount namespaces')
            check('e2b local KVM', os.access('/dev/kvm', os.R_OK | os.W_OK), '/dev/kvm')
            try:
                build = configured_path(config, 'e2b.storage') / 'templates' / configured_value(config, 'e2b.from_build')
                for name in ('metadata.json', 'snapfile', 'memfile', 'memfile.header', 'rootfs.ext4', 'rootfs.ext4.header'):
                    check('e2b base ' + name, (build / name).is_file(), build / name)
            except ValueError as err:check('e2b base build', False, err)
    for backend in ('cube','e2b'):
        if 'figure-08-'+backend in experiments:
            if backend == 'e2b':
                key_state = 'set' if os.environ.get('E2B_API_KEY') else 'missing'
                check('E2B_API_KEY', key_state == 'set', key_state)
            try:
                path = fanout_python(config, backend)
                check(backend + ' SDK python', path.is_file(), path)
                if backend == 'e2b':
                    sdk = probe_e2b_sdk(path)
                    check('e2b.Sandbox SDK', sdk['ok'], json.dumps(sdk, sort_keys=True))
            except ValueError as err:check(backend + ' SDK python',False,err)
        if any(e.endswith('-'+backend) for e in experiments) and (backend=='cube' or 'figure-08-e2b' in experiments):
            for name in (backend+'.api_url',backend+'.template'):
                try:check(name,True,configured_value(config,name))
                except ValueError as err:check(name,False,err)
    return {'ok':all(c['ok'] for c in checks),'checks':checks,'gpu':SKIPPED}


def prepare_inputs():
    """Reuse verified data read-only, including a prior root-owned import."""
    data_cli = [sys.executable, str(AE_ROOT / 'scripts/paper_data.py')]
    verified = subprocess.run([*data_cli, 'verify'], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if verified.returncode == 0:
        print(verified.stdout, end='', flush=True)
    else:
        print(verified.stdout, end='', file=sys.stderr, flush=True)
        print('Existing inputs did not verify; importing the configured bundle with current user permissions.', flush=True)
        code = subprocess.call([*data_cli, 'import'])
        if code:
            return code
    return subprocess.call([sys.executable, str(AE_ROOT / 'scripts/verify_runtime_sources.py')])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('prepare',help='Verify/import bundled content-addressed inputs and historical records')
    sub.add_parser('list',help='List published CPU experiments and derived panels')
    for name in ('doctor','plan','run'):
        p=sub.add_parser(name);p.add_argument('--config',type=Path,default=AE_ROOT/'configs/spr4numa.json')
        p.add_argument('--experiment',action='append',choices=EXPERIMENTS);p.add_argument('--all',action='store_true')
        p.add_argument('--output',type=Path);p.add_argument('--limit',type=int,help='First N instances only; always marked smoke')
        p.add_argument('--max-events',type=int,help='Explicit prefix smoke; units are documented per backend')
        if name=='run':p.add_argument('--keep-going',action='store_true',help='Continue independent jobs after failure; final exit stays nonzero')
    for name in ('analyze','plot'):
        p=sub.add_parser(name);p.add_argument('--output',type=Path,required=True);p.add_argument('--input',type=Path)
        if name=='analyze':p.add_argument('--source',choices=['archived','fresh'],required=True)
    args=parser.parse_args()
    if args.command=='prepare':
        return prepare_inputs()
    if args.command=='list':print(json.dumps({'experiments':EXPERIMENTS,'derived':DERIVED,'skipped':SKIPPED},indent=2,ensure_ascii=False));return 0
    if args.command=='analyze':
        cmd=[sys.executable,str(AE_ROOT/'repro/analysis.py'),'--source',args.source,'--output',str(args.output)]
        if args.input:cmd+=['--input',str(args.input)]
        return subprocess.call(cmd)
    if args.command=='plot':
        if not args.input:parser.error('plot requires --input summary.json')
        return subprocess.call([sys.executable,str(AE_ROOT/'repro/plot.py'),'--input',str(args.input),'--output',str(args.output)])
    if args.all and args.experiment:parser.error('choose --all or --experiment')
    experiments=list(EXPERIMENTS) if args.all else args.experiment or ['table-02-deltabox']
    if args.limit is not None and args.limit<=0 or args.max_events is not None and args.max_events<=0:parser.error('limits must be positive')
    config=load_config(args.config)
    release=from_environment()
    if args.command=='doctor':
        result=doctor(config,experiments);print(json.dumps(result,indent=2));return 0 if result['ok'] else 2
    output=(args.output or AE_ROOT/'results'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')).resolve()
    jobs=build_jobs(experiments,config,args.config.resolve(),output,args.limit,args.max_events)
    record={'schema_version':1,'analysis_mode':'fresh-measurement','runtime':repository_state(),'host':host_state(),'config':public_config(config),
            'release':release,
            'config_source':file_record(args.config),'experiments':experiments,'jobs':jobs,'skipped':SKIPPED,
            'derived':DERIVED,'run_purpose':'smoke' if args.limit or args.max_events else 'full-cohort','status':'planned'}
    if args.command=='plan':print(json.dumps(record,indent=2));return 0
    prerequisites=doctor(config,experiments)
    if not prerequisites['ok']:
        print(json.dumps(prerequisites,indent=2));return 2
    output.mkdir(parents=True,exist_ok=False);manifest=output/'suite.json';write_json(manifest,record)
    failed=False
    try:
        for index,job in enumerate(jobs,1):
            from_environment()
            print(f'[{index}/{len(jobs)}] {job["key"]}',flush=True)
            status=execute(job['command'],output/'logs'/job['key'],cwd=REPO_ROOT,timeout=float(job.get('timeout_s',config.get('timeout',14400))),
                           env=dict(os.environ,AE_RUN_PURPOSE=job['run_purpose']))
            job['status']=status['status'];job['process_manifest']=str(output/'logs'/job['key']/'process.json')
            write_json(manifest,record)
            if status['status']!='ok':
                failed=True
                if not args.keep_going:break
    finally:
        record['status']='failed' if failed or any(j.get('status')!='ok' for j in jobs) else 'ok'
        write_json(manifest,record)
    print(f'{record["status"]}: {manifest}')
    return 1 if record['status']!='ok' else 0

if __name__ == '__main__':
    from repro.common import install_termination_handler
    install_termination_handler()
    raise SystemExit(main())
