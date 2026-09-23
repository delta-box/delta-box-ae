#!/usr/bin/env python3
"""Replay recorded Figure 2 responses and measure FS deltas and process-tree RSS."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent))
from repro.common import host_state, run_purpose, artifact_records, AE_ROOT, configured_path, file_record, jsonl, load_config, number, write_json, repository_state
from baseline import stage_payload, stage_local_dependencies, configure_test_runtime, configure_mock_latency, validate_mock_latency
from repro.replay_audit import message_policy, summarize, validate_stats
sys.path.insert(0,str(AE_ROOT/'vendor/spr_payload'))
from baseline_audit import flush_audit
from release.lock import from_environment


def http(port,path):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/admin/{path}',timeout=2) as response:return json.load(response)


@contextmanager
def audit_policy_environment(policy):
    """The shared client reads this setting; do not leak it to another run."""
    previous=os.environ.get('MOCK_MESSAGE_POLICY')
    os.environ['MOCK_MESSAGE_POLICY']=policy
    try:yield
    finally:
        if previous is None:os.environ.pop('MOCK_MESSAGE_POLICY',None)
        else:os.environ['MOCK_MESSAGE_POLICY']=previous


def stop_owned(process):
    if process is None:return
    def reap():
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.wait(timeout=5)
    try:reap()
    except KeyboardInterrupt:
        # The termination handler ignores subsequent SIGTERM. Finish owned
        # cleanup before propagating the first interruption to the manifest.
        reap()
        raise


def validate_complete_replay(state, policy):
    validate_stats(state,policy)
    if (state.get('ok') is not True or type(state.get('cursor')) is not int
            or type(state.get('total')) is not int or state['total'] <= 0
            or state['cursor'] != state['total']):
        raise ValueError('Incomplete recorded-response replay')


def sample(root):
    seen=[];stack=[root]
    while stack:
        pid=stack.pop()
        if pid in seen:continue
        try:
            stat=Path(f'/proc/{pid}/status').read_text()
            children=Path(f'/proc/{pid}/task/{pid}/children').read_text()
        except FileNotFoundError:continue
        rss=next((int(line.split()[1]) for line in stat.splitlines() if line.startswith('VmRSS:')),None)
        # Exited/zombie processes have no resident set; do not create a zero sample.
        if rss is None:continue
        seen.append(pid);stack.extend(map(int,children.split()))
        yield pid,rss


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,required=True)
    p.add_argument('--instance',required=True);p.add_argument('--trace',type=Path,required=True)
    p.add_argument('--panel',choices=['filesystem','memory'],required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--timeout',type=int,default=3600);p.add_argument('--dry-run',action='store_true')
    args=p.parse_args();output=args.out.resolve();output.mkdir(parents=True,exist_ok=False);config=load_config(args.config)
    record={'experiment':'figure-02-'+args.panel,'instance':args.instance,'status':'preparing','runtime':repository_state(),'host':host_state(),
        'analysis_mode':'fresh-measurement','release':from_environment(),'run_purpose':run_purpose('full-trace'),'protocol':'recorded-response replay; no new model generation',
        'rtt_policy':'recorded' if (args.trace.parent/'ms_trace.jsonl').exists() else 'No RTT archive: no artificial model wait; only state-size/delta metrics',
        'input':file_record(args.trace)}
    mock=None;worker=None;samples=[];policy=None
    try:
        policy=message_policy(config.get('replay_message_policy','audit'))
        record['message_policy']=policy
        payload,traces,sources=stage_payload(config,args.trace,args.instance,output,allow_missing_rtt=True)
        source_link=payload/'moatless-det-src';source=source_link.resolve();source_link.unlink()
        shutil.copytree(source,source_link,ignore=shutil.ignore_patterns('.git','__pycache__','*.pyc'))
        shutil.copy2(AE_ROOT/'vendor/spr_payload/moatless-det-src/moatless/search_tree.py',source_link/'moatless/search_tree.py')
        record['sources']=sources+[file_record(x) for x in sorted(source_link.rglob('*.py'))]
        repos=output/'repos';repos.mkdir()
        name='swe-bench_'+args.instance
        shutil.copytree(payload/'repos'/name,repos/name,symlinks=True)
        record['repository_commit']=subprocess.check_output(['git','-C',str(repos/name),'rev-parse','HEAD'],text=True).strip()
        record['filesystem_baseline_bytes']=int(subprocess.check_output(
            ['du','-sb','--exclude=.git',str(repos/name)],text=True).split()[0])
        record['filesystem_baseline_metric']='GNU du -sb --exclude=.git at trace base commit; apparent bytes including directory entries'
        env=dict(os.environ,PYTHONPATH=os.pathsep.join([str(source_link),str(payload)]),PYTHONHASHSEED='0',
            MOATLESS_STEP_METRICS_PATH=str(output/'step_metrics.jsonl'),MOCK_MESSAGE_POLICY=policy,
            MOCK_MISMATCH_DIR=str(output/'diagnostics'),OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1')
        record.update(configure_mock_latency('profile',env))
        record['local_dependencies']=stage_local_dependencies(config,output,env)
        record['baseline_test_runtime']=configure_test_runtime(config,'profile',env)
        python=str(configured_path(config,'moatless_venv')/'bin/python')
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        command=[python,str(payload/'replay_driver.py'),'--manifest-line',args.instance+'__ms','--traces-root',str(traces),
            '--mock-port',str(port),'--repo-base',str(repos),'--index-store-dir',str(payload/'index_store'),
            '--skip-mock-spawn','--defer-audit','--runtime','configured']
        record.update(command=command,status='planned' if args.dry_run else 'running');write_json(output/'run.json',record)
        if args.dry_run:return 0
        with (output/'mock.log').open('w') as log:
            mock=subprocess.Popen([python,str(payload/'mock_llm_server.py'),'--tcp-port',str(port),'--traces-root',str(traces)],env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        deadline=time.monotonic()+30
        while True:
            try:
                if http(port,'healthz').get('ok'):break
            except OSError:pass
            if mock.poll() is not None or time.monotonic()>deadline:raise RuntimeError('Mock did not start')
            time.sleep(.1)
        started=time.monotonic()
        with (output/'replay.log').open('w') as log:
            worker=subprocess.Popen(command,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            while worker.poll() is None:
                measured=dict(sample(worker.pid))
                if measured:
                    samples.append({'t_wall_s':time.time(),'rss_kb_total':sum(measured.values()),'rss_kb_by_pid':measured})
                if time.monotonic()-started>args.timeout:raise TimeoutError('Profile timeout')
                time.sleep(.5)
        if worker.returncode:raise RuntimeError(f'Replay worker exited with status {worker.returncode}')
        rows=jsonl(output/'step_metrics.jsonl')
        if not samples:raise ValueError('No process-tree RSS samples')
        for row in rows:
            if row.get('soft_dirty_error') or row.get('soft_dirty_clear_error') or row.get('soft_dirty_skipped_pages',0):raise ValueError('Incomplete soft-dirty scan')
            number(row['soft_dirty_bytes'],'soft_dirty_bytes');number(row['action_write_bytes'],'action_write_bytes')
        record.update(status='ok',step_count=len(rows),rss_sample_count=len(samples))
    except BaseException as error:record.update(status='failed',error=f'{type(error).__name__}: {error}');raise
    finally:
        primary_error=sys.exc_info()[1]
        teardown_error=None
        def failed(error, phase):
            nonlocal teardown_error
            if teardown_error is None:
                teardown_error=error
                if primary_error is None:record.update(status='failed',error=f'{type(error).__name__}: {error}')
            record.setdefault('cleanup_errors',[]).append({'phase':phase,'error':f'{type(error).__name__}: {error}'})
        # End the entire worker process tree and its RSS sampling before any
        # diagnostic export is formatted. Failed runs preserve partial samples.
        try:
            try:stop_owned(worker)
            except BaseException as error:failed(error,'stop_worker')
            if worker is not None:
                try:write_json(output/'tree_rss_samples.json',samples)
                except BaseException as error:failed(error,'save_rss_samples')
            if mock is not None:
                state=None
                try:
                    state=http(port,'stats')
                    write_json(output/'mock_stats.json',state)
                except BaseException as error:failed(error,'save_mock_stats')
                try:
                    with audit_policy_environment(policy):
                        audit=flush_audit(f'http://127.0.0.1:{port}',output/'mock_audit.json',
                                          primary_error=primary_error or teardown_error)
                    validate_mock_latency([audit],record['mock_latency_policy'])
                    record['replay_audit']=summarize([audit],policy)
                    validate_complete_replay(audit['stats'],policy)
                    if state is not None:
                        validate_complete_replay(state,policy)
                        if any(state.get(key) != audit['stats'].get(key) for key in
                               ('cursor','total','n_mismatch','n_protocol_errors')):
                            raise ValueError('Mock stats changed after sampling ended')
                except BaseException as error:
                    failed(error,'export_mock_audit')
                    if not (output/'mock_audit.json').exists():
                        write_json(output/'mock_audit.json',{'ok':False,'message_policy':policy,
                                   'audit_error':{'type':type(error).__name__,'message':str(error)}})
        except BaseException as error:failed(error,'finalize_profile')
        finally:
            # Also runs if a first SIGTERM interrupts stats/export after the
            # measured worker has already exited.
            try:stop_owned(mock)
            except BaseException as error:failed(error,'stop_mock')
            try:
                artifacts=[output/name for name in ('step_metrics.jsonl','tree_rss_samples.json',
                           'mock_stats.json','mock_audit.json','mock.log','replay.log') if (output/name).is_file()]
                if artifacts:record['artifacts']=artifact_records(output,artifacts)
                write_json(output/'run.json',record)
            except BaseException as error:failed(error,'save_manifest')
        if primary_error is None and teardown_error is not None:raise teardown_error
    return 0

if __name__ == '__main__':
    from repro.common import install_termination_handler
    install_termination_handler()
    raise SystemExit(main())
