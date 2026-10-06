#!/usr/bin/env python3
"""Run official Cube/E2B fanout, including genuinely measured E2B 4x16 batches."""
from __future__ import annotations
import argparse
from contextlib import ExitStack
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


def hosted_local_e2b(config):
    """Select the managed local method; service admission still verifies leases."""
    settings = config.get('e2b', {})
    return ('AE_HOSTED_CALLER_UID' in os.environ
            and settings.get('execution', 'ssh') == 'local'
            and settings.get('api_url', '').rstrip('/') in ('http://127.0.0.1:3100', 'http://localhost:3100')
            and settings.get('sandbox_url', '').rstrip('/') in ('http://127.0.0.1:3102', 'http://localhost:3102'))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backend',choices=['cube','e2b'],required=True);p.add_argument('--config',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--forks');p.add_argument('--dry-run',action='store_true')
    args=p.parse_args()
    if args.backend == 'e2b':
        from ae.scripts.e2b_service_context import assert_backend_ready
        assert_backend_ready()
    release=from_environment();config=load_config(args.config);settings=config[args.backend];out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    if args.forks is None:args.forks='1,4,16,64'
    forks=list(map(int,args.forks.split(',')))
    if not forks or min(forks)<=0:raise ValueError('fork counts must be positive')
    driver=AE_ROOT/'vendor/finalbench/official_sandbox_fork/bench_official_fork.py'
    official_driver = driver
    if args.backend == 'cube':
        driver = AE_ROOT/'runners/cube_fanout_audit.py'
    env=os.environ.copy()
    if args.backend=='cube':env['PYTHONPATH']=str(configured_path(config,'cube.sdk'))+os.pathsep+env.get('PYTHONPATH','')
    python = fanout_python(config, args.backend)
    command=[str(python),str(driver),'--forks',args.forks,
             '--mem-mib','64','--out',str(out/'fanout.json')]
    for key in ('api_url','template','proxy_node_ip') if args.backend=='cube' else ('api_url','sandbox_url','template'):
        value=os.path.expandvars(settings[key])
        if '${' in value:raise ValueError('Unresolved '+args.backend+'.'+key)
        command+=['--'+args.backend+'-'+key.replace('_','-'),value]
    if args.backend=='e2b':command+=['--backend','e2b','--e2b-batch-size','16','--max-workers','16']
    record={'experiment':'figure-08-'+args.backend,'backend':args.backend,'runtime':repository_state(),'release':release,'host':host_state(),
        'analysis_mode':'fresh-measurement','run_purpose':run_purpose('quick-check' if forks != [1,4,16,64] else 'full-trace'),'driver':file_record(driver),'command':command,
        'status':'planned' if args.dry_run else 'running','expected_forks':forks,
        'protocol':('Cube: official snapshot/create/delete clone timer plus inherited-memory verification wall; retain every selected point.' if args.backend == 'cube' else 'E2B: one snapshot; measured sequential <=16-child batches, including inter-batch cleanup. No estimated timing.')}
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
        with ExitStack() as contexts:
            managed = None
            if args.backend == 'e2b' and hosted_local_e2b(config):
                from ae.scripts.e2b_service_context import service_placement
                record['e2b_service_placement'] = contexts.enter_context(service_placement(
                    config, out/'environment/e2b-placement', fanout_path=out/'fanout.json',
                    working_storage=True,source_sha256=release['source_sha256']))
                write_json(path, record)
            if args.backend == 'cube':
                from ae.scripts.cube_control_context import metadata_enabled, managed_memory_service, idle, save
                from ae.runners.cube_memory import verify
                if metadata_enabled(config):
                    managed = contexts.enter_context(managed_memory_service(config, out/'environment'))
                    config['cube']['memory_manifest'] = managed['memory_manifest']
                    record['cube_managed_environment'] = managed
                if config.get('baseline_storage') == 'tmpfs':
                    verify(config)
                from cube_environment import capture_cube_environment
                record['official_driver'] = file_record(official_driver)
                record['cube_environment'] = capture_cube_environment(
                    api_url=settings['api_url'], template=settings['template'],
                    sdk_path=configured_path(config, 'cube.sdk'),
                    phase_binary=configured_path(config, 'cube.phase_binary'))
                write_json(path, record)
            if managed:
                save(Path(managed['recovery_guard']), {'reason': 'Measurement may own live Cube resources',
                     'audit': str(out/'cube-audit.json')})
            try:
                result=execute(command,out/'process',cwd=REPO_ROOT,env=env,timeout=config.get('timeout',14400),
                               **({'termination_grace': 120} if args.backend == 'cube' else {}))
            finally:
                if managed:
                    audit_path = out/'cube-audit.json'
                    audit = json.loads(audit_path.read_text()) if audit_path.exists() else {}
                    if audit.get('cleanup_ok') is True:
                        idle()
                        Path(managed['recovery_guard']).unlink()
            rows=json.loads((out/'fanout.json').read_text())
            if result['status']!='ok' or [r['forks'] for r in rows]!=forks:raise ValueError('Missing or failed fanout run')
            for row in rows:
                if not row.get('success') or row.get('success_count')!=row['forks']:raise ValueError('Incomplete inherited memory verification')
                number(row['ready_e2e_ms'],'ready_e2e_ms')
        environment_files = sorted((out/'environment').rglob('*.json')) if args.backend == 'cube' or record.get('e2b_service_placement') else []
        record['artifacts']=artifact_records(out,[out/'fanout.json', *environment_files] + ([out/'cube-audit.json'] if args.backend == 'cube' else []))
        record['status']='ok'
    except BaseException as error:record.update(status='failed',error=str(error));raise
    finally:write_json(path,record)
    return 0
if __name__ == '__main__':
    from repro.common import install_termination_handler
    install_termination_handler()
    raise SystemExit(main())
