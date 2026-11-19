#!/usr/bin/env python3
"""Run the preserved November drivers inside the prebuilt L1 appliance.

This adapter changes input/output placement, not the measured C/R operations.
Run serially by default; the original four-lane schedules remain in evidence.
"""
import argparse, fcntl, json, os, shutil, subprocess, sys, time
from pathlib import Path

ROOT = Path('/home/atc-ae/delta-box-ae')
NEST = Path('/mnt/disk2/dyp/deltabox-runtime/ae/work/deferred-nested-20261107')
TABLE = NEST.parent / 'table2-unified12-20261107'
MANIFEST = ROOT / 'ae/work/cube-official-v030-20261105/manifest.tsv'
E2B = Path('/mnt/disk2/dyp/ae-hosted-20260922/e2b')
FC = NEST / 'fc-reflink-20261109'
TRACES = Path('/mnt/disk2/dyp/d-overlayfs/traces/swe-search/qwen3-coder-30b-p-eagle-ms/mcts-iter30')
LANES = [
    ['astropy__astropy-7746', 'django__django-14997', 'sympy__sympy-17630'],
    ['pylint-dev__pylint-7228', 'django__django-14672', 'sympy__sympy-22840'],
    ['matplotlib__matplotlib-23964', 'pylint-dev__pylint-7080', 'matplotlib__matplotlib-26020'],
    ['astropy__astropy-14182', 'astropy__astropy-13453', 'psf__requests-863'],
]

def call(command, **kwargs):
    return subprocess.run([str(v) for v in command], check=True, **kwargs)

def fc_storage():
    image = ROOT/'ae/work/appliance-fc-store.xfs'
    mount = FC/'snapshots'
    mount.mkdir(exist_ok=True)
    if mount.is_mount():
        assert subprocess.check_output(['findmnt', '-n', '-o', 'FSTYPE', '-T', str(mount)], text=True).strip() == 'xfs'
        return mount
    if not image.exists():
        if shutil.disk_usage(image.parent).free < 45*2**30:
            raise RuntimeError('FC needs at least 45 GiB free in the guest')
        with image.open('xb') as f: f.truncate(40*2**30)
        call(['mkfs.xfs', '-f', '-m', 'reflink=1', image])
    call(['mount', '-o', 'loop', image, mount])
    return mount

def command_for(backend, instance, out, smoke):
    node = next(i for i, items in enumerate(LANES) if instance in items)
    cpus = f'{12*node}-{12*node+7}'
    env = dict(os.environ)
    if backend in ('cube', 'e2b'):
        py = str(E2B/'sdk-venv/bin/python') if backend == 'e2b' else '/mnt/disk2/dyp/cube-l1/python-venv/bin/python'
        template = 'ae-e2b-agent-4c8g-20261106' if backend == 'e2b' else 'cube-agent-full-4c8g-l1e'
        cmd = [py, str(NEST/f'{backend}_cold_full.py'), '--manifest', str(MANIFEST), '--instances', instance,
               '--template', template, '--lane', f'n{node}', '--out', str(out), '--max-steps', '3' if smoke else '29']
        if backend == 'cube':
            env.update(AGENT_TASK='/mnt/disk2/dyp/cube-l1', CUBE_SDK_PATH='/mnt/disk2/dyp/cube-l1/sdk-runtime',
                       CUBE_API_URL='http://127.0.0.1:3000', CUBE_PROXY_NODE_IP='127.0.0.1')
            cmd += ['--vm-cpus', f'{12*node}-{12*node+3}', '--helper-cpus', f'{12*node+4}-{12*node+7}']
        return cmd, env
    if backend == 'table3':
        return ['/usr/bin/python3', str(NEST/'criu-overlap-20261109/run_trace.py'), '--instance', instance,
                '--out', str(out), '--max-steps', '3' if smoke else '29'], env
    if backend == 'replay':
        stage = NEST/'replay-stage'/instance
        driver = out/'driver'
        out.mkdir(parents=True)
        shutil.copytree(stage/'driver', driver, ignore=shutil.ignore_patterns('results', '__pycache__'))
        env.update(AE_BASE=str(driver), SPR_PAYLOAD=str(stage/'payload'), MOCK_TRACES_ROOT=str(stage/'mock_traces'),
                   MOCK_MESSAGE_POLICY='audit', MOCK_MISMATCH_DIR=str(out/'diagnostics'),
                   MOATLESS_VENV='/mnt/disk2/dyp/moatless_det_venv',
                   PYTHONPATH=f'{driver}:{stage / "payload"}:{stage / "payload/moatless-det-src"}',
                   MOCK_LATENCY_POLICY='zero', LITELLM_LOCAL_MODEL_COST_MAP='True',
                   NLTK_DATA='/mnt/disk2/dyp/replay-offline-nltk-20260921', OPENAI_API_KEY='dummy')
        cmd = ['systemd-run', '--scope', '-q', '-p', 'MemoryMax=8G', '-p', 'MemorySwapMax=0',
               'numactl', f'--membind={node}', f'--physcpubind={cpus}',
               '/mnt/disk2/dyp/moatless_det_venv/bin/python', str(driver/'real_trace_runner.py'), instance]
        if smoke: cmd += ['--max-restores', '2']
        return cmd, env
    source = FC if backend == 'fc-diff' else TABLE
    config = source/('config-fc-diff.json' if backend == 'fc-diff' else 'config-criu.json')
    trace = TRACES/instance/'trajectory.json'
    cmd = ['numactl', f'--membind={node}', f'--physcpubind={cpus}', '/usr/bin/python3',
           str(source/'ae_baseline_shim.py'), '--backend', backend, '--instance', instance,
           '--trace', str(trace), '--config', str(config), '--out', str(out)]
    if not json.loads(trace.read_text()).get('repository', {}).get('commit'):
        commit = subprocess.check_output(['git', '-c', 'safe.directory=*', '-C',
            '/mnt/disk2/dyp/spr_payload/repos/swe-bench_'+instance, 'rev-parse', 'HEAD'], text=True).strip()
        cmd += ['--repository-commit', commit]
    if smoke: cmd += ['--limit', '3']
    if backend == 'fc-diff':
        env.update(FCDM_SNAPSHOT_ROOT=str(fc_storage()), FC_MERGE_VALIDATE='1' if smoke else '0',
                   FC_VALIDATION_LOG=str(out.parent/(instance+'-merge-validation.jsonl')),
                   TMPDIR=str(FC/'tmp'))
    else:
        env.update(FINALBENCH_CRIU_LAZY_RESTORE='1', FINALBENCH_CRIU_ALIGNED='1', FINALBENCH_MAILBOX_POLL_S='0.001')
    return cmd, env

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('backend', choices=['cube', 'e2b', 'fc-diff', 'criu', 'replay', 'table3'])
    p.add_argument('--instance', action='append', help='Omit for the 12-trace cohort')
    p.add_argument('--smoke', action='store_true', help='Short functional check, not a paper timing result')
    p.add_argument('--out', type=Path, required=True, help='New directory under ae/results/')
    a=p.parse_args()
    if os.geteuid()!=0: p.error('Run with sudo inside the disposable appliance')
    out=a.out.resolve()
    if ROOT/'ae/results' not in out.parents: p.error('Output must be below /home/atc-ae/delta-box-ae/ae/results/')
    instances=a.instance or (['psf__requests-863'] if a.smoke else sum(LANES, []))
    if any(i not in sum(LANES, []) for i in instances): p.error('Unknown instance')
    if out.exists(): p.error('Refusing to overwrite an existing output directory')
    lock=open(ROOT/'ae/work/appliance-run.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
    out.mkdir(parents=True)
    meta={'backend':a.backend, 'instances':instances, 'functional_only':a.smoke,
          'started_epoch':time.time(), 'placement':'original per-instance virtual NUMA node, serial adapter', 'runs':[]}
    for instance in instances:
        command, env=command_for(a.backend, instance, out/instance, a.smoke)
        with (out/(instance+'.log')).open('w') as log:
            result=subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
        meta['runs'].append({'instance':instance, 'command':command, 'rc':result.returncode})
        (out/'adapter-run.json').write_text(json.dumps(meta, indent=2))
        print(instance, 'rc', result.returncode, flush=True)
        if result.returncode: return result.returncode
    meta['finished_epoch']=time.time(); (out/'adapter-run.json').write_text(json.dumps(meta, indent=2))
    return 0

if __name__=='__main__': raise SystemExit(main())
