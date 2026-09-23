#!/usr/bin/env python3
"""Run official Cube/E2B fanout, including genuinely measured E2B 4x16 batches."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from repro.common import host_state, run_purpose, artifact_records, AE_ROOT, REPO_ROOT, load_config, configured_path, file_record, write_json, number, repository_state
from repro.process import execute
from repro.fanout_sdk import fanout_python, probe_e2b_sdk
from release.lock import from_environment


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backend',choices=['cube','e2b'],required=True);p.add_argument('--config',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--forks',default='1,4,16,64');p.add_argument('--dry-run',action='store_true')
    args=p.parse_args();release=from_environment();config=load_config(args.config);settings=config[args.backend];out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    forks=list(map(int,args.forks.split(',')))
    if not forks or min(forks)<=0:raise ValueError('fork counts must be positive')
    driver=AE_ROOT/'vendor/finalbench/official_sandbox_fork/bench_official_fork.py'
    env=os.environ.copy()
    if args.backend=='cube':env['PYTHONPATH']=str(configured_path(config,'cube.sdk'))+os.pathsep+env.get('PYTHONPATH','')
    python = fanout_python(config, args.backend)
    command=[str(python),str(driver),'--backend',args.backend,'--forks',args.forks,
             '--mem-mib','64','--out',str(out/'fanout.json')]
    for key in ('api_url','template','proxy_node_ip') if args.backend=='cube' else ('api_url','sandbox_url','template'):
        value=os.path.expandvars(settings[key])
        if '${' in value:raise ValueError('Unresolved '+args.backend+'.'+key)
        command+=['--'+args.backend+'-'+key.replace('_','-'),value]
    if args.backend=='e2b':command+=['--e2b-batch-size','16','--max-workers','16']
    record={'experiment':'figure-08-'+args.backend,'backend':args.backend,'runtime':repository_state(),'release':release,'host':host_state(),
        'analysis_mode':'fresh-measurement','run_purpose':run_purpose('smoke' if forks != [1,4,16,64] else 'full-trace'),'driver':file_record(driver),'command':command,
        'status':'planned' if args.dry_run else 'running','expected_forks':forks,
        'protocol':'E2B: one snapshot; measured sequential <=16-child batches, including inter-batch cleanup. No estimated timing.'}
    if args.backend == 'cube' and not args.dry_run:
        from cube_environment import capture_cube_environment
        record['cube_environment'] = capture_cube_environment(
            api_url=settings['api_url'], template=settings['template'],
            sdk_path=configured_path(config, 'cube.sdk'),
            phase_binary=configured_path(config, 'cube.phase_binary'))
    path=out/'run.json';write_json(path,record)
    if args.dry_run:return 0
    try:
        if args.backend == 'e2b':
            record['e2b_environment'] = probe_e2b_sdk(python, env=env)
            write_json(path, record)
            if not record['e2b_environment']['ok']:
                raise ValueError(record['e2b_environment']['error'])
            if record['e2b_environment']['api_key'] != 'set':
                raise ValueError('E2B_API_KEY is missing')
        result=execute(command,out/'process',cwd=REPO_ROOT,env=env,timeout=config.get('timeout',14400))
        rows=json.loads((out/'fanout.json').read_text())
        if result['status']!='ok' or [r['forks'] for r in rows]!=forks:raise ValueError('Missing or failed fanout run')
        for row in rows:
            if not row.get('success') or row.get('success_count')!=row['forks']:raise ValueError('Incomplete inherited memory verification')
            number(row['ready_e2e_ms'],'ready_e2e_ms')
        record['artifacts']=artifact_records(out,[out/'fanout.json'])
        record['status']='ok'
    except BaseException as error:record.update(status='failed',error=str(error));raise
    finally:write_json(path,record)
    return 0
if __name__ == '__main__':
    from repro.common import install_termination_handler
    install_termination_handler()
    raise SystemExit(main())
