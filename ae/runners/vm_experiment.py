#!/usr/bin/env python3
"""CPU fan-out, WAR, and recovered correctness tests in a disposable VM."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parents[1]))
sys.path.insert(0,str(HERE.parent));sys.path.insert(0,str(HERE/'deltabox'))
from repro.common import host_state, run_purpose, artifact_records, AE_ROOT, REPO_ROOT, configured_path, load_config, file_record, write_json, repository_state, jsonl, number
from repro.process import execute
from repro.repositories import read_blob, select_repository
from release.lock import from_environment
from provenance import build_guest_archive, cached_digest
import vm


def require_memory_workdir(path):
    """Fail before VM allocation if the runtime would use a disk or swap."""
    path = Path(path).resolve(strict=True)
    fstype = subprocess.check_output(['stat', '-f', '-c', '%T', str(path)], text=True).strip()
    mounted = json.loads(subprocess.check_output(
        ['findmnt', '--json', '--target', str(path), '--output', 'TARGET,FSTYPE,OPTIONS'], text=True))['filesystems'][0]
    if fstype != 'tmpfs' or 'noswap' not in mounted['options'].split(','):
        raise ValueError('Figure 9 requires work_dir on noswap tmpfs: '+str(path))
    return dict(path=str(path), fstype=fstype, mount=mounted)


@contextmanager
def runtime_directory(config, output):
    war = config['experiment'] == 'figure-09'
    parent = config.get('work_dir')
    if war and parent:
        require_memory_workdir(parent)
    if war and not parent:
        parent = AE_ROOT/'work'
        parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ae-cpu-', dir=parent) as directory:
        runtime = Path(directory)
        mounted = False
        try:
            if war and not config.get('work_dir'):
                size = Path(config['base_xfs']).stat().st_size + 1024**3
                subprocess.run(['mount', '-t', 'tmpfs', '-o', f'size={size},noswap',
                                'ae-war-vm-memory', str(runtime)], check=True)
                mounted = True
            if war:
                write_json(output/'host-storage.json', require_memory_workdir(runtime))
            yield runtime
        finally:
            if mounted:
                subprocess.run(['umount', str(runtime)], check=True)


def build_extra(args, output):
    files={}
    vendor=AE_ROOT/'vendor'
    files.update({p.name:p for p in (HERE/'guest').glob('*.py')})
    files['batched_fork.py']=vendor/'d-overlayfs/guest/rl/batched_fork.py'
    files['nonshared_bytes.py']=vendor/'d-overlayfs/benchmarks/nonshared_bytes.py'
    files['swesearch_replay_engine.py']=vendor/'d-overlayfs/benchresults/2026-05-11_swesearch_war/swesearch_replay_engine.py'
    files.update({'agentfs/'+p.name:p for p in (vendor/'d-overlayfs/agentfs').iterdir() if p.is_file()})
    experiment={'experiment':args.experiment,'forks':args.forks,'arm':args.arm,'input_key':args.input_key}
    if args.experiment == 'figure-09':
        experiment['filesystem_geometry'] = {'logical_bytes': 4 * 1024**3, 'allocation': 'sparse loop file on explicit noswap tmpfs in disposable guest',
                                             'lower_source': 'recorded base-commit Git file bytes for each edited file'}
    if args.actions:
        info=json.loads(args.actions.read_text());files['actions.json']=args.actions
        files_needed=sorted({edit['file_path'] for edit in info['edits']})
        repo=select_repository(load_config(args.config),info['instance_id'],info['base_commit'],files_needed)
        experiment['repository_source']={'path':str(repo.resolve()),'commit':info['base_commit'],
                                         'files':files_needed,'selection':'exact locally cached Git objects; clone name may differ; all fetch transports disabled'}
        lower=output/'lower.tar'
        if not repo.is_dir(): raise FileNotFoundError(repo)
        with tarfile.open(lower,'w') as tar:
            for name in files_needed:
                if Path(name).is_absolute() or '..' in Path(name).parts: raise ValueError('unsafe edit path')
                raw=read_blob(repo,info['base_commit'],name)
                entry=tarfile.TarInfo(name);entry.size=len(raw);tar.addfile(entry,io.BytesIO(raw))
        files['lower.tar']=lower
        experiment['expected_edits']=len(info['edits'])
    write_json(output/'experiment.json',experiment);files['experiment.json']=output/'experiment.json'
    with tarfile.open(output/'extra.tar','w') as tar:
        for name,path in files.items():tar.add(path.resolve(),arcname=name,recursive=False)
    return [file_record(path) for path in files.values()], experiment


def guest_run(config_path):
    config=json.loads(config_path.read_text());output=config_path.parent
    release=from_environment()
    if (release or {}).get('source_sha256') != (config.get('release') or {}).get('source_sha256'):
        raise ValueError('VM producer release source differs from the active guest-launch lock')
    with runtime_directory(config, output) as runtime:
        args=SimpleNamespace(kernel=Path(config['kernel']),base_xfs=Path(config['base_xfs']),data_xfs=Path(config['data_xfs']),
            run_rootfs=runtime/'rootfs.xfs',socket=runtime/'fc.sock',log=output/'firecracker.log',ssh_pubkey=None,
            tap='ae'+runtime.name[-10:],guest_ip=vm.GUEST_IP,vcpus=4,mem_mib=8192,ssh_timeout=120,
            reuse_rootfs=False,no_nat=True,inherit_process_group=True)
        process=None
        ssh=['ssh',*vm.ssh_opts(),'-o','ConnectTimeout=10',f'root@{args.guest_ip}']
        scp=['scp','-O',*vm.ssh_opts(),'-o','ConnectTimeout=10']
        try:
            subprocess.run(['ip','link','set','lo','up'],check=True)
            process=vm.start_vm(args)
            for name in ('guest.tar','extra.tar'):
                with (output/name).open('rb') as source:
                    subprocess.run(ssh+['mkdir -p /app; tar -xf - -C /app'],stdin=source,check=True,timeout=120)
            exp=config['experiment']
            command=['python3','-u','/app/'+('fanout.py' if exp=='figure-08-deltabox' else 'war.py' if exp=='figure-09' else 'correctness.py')]
            if exp=='figure-08-deltabox':
                command+=['--forks',*map(str,config['forks']),'--mem-mib','64','--touch-mode','read','--out','/tmp/ae-output/fanout.json']
            subprocess.run(ssh+['mkdir -p /tmp/ae-output'],check=True)
            with (output/'guest.log').open('w') as log:
                try:
                    subprocess.run(ssh+[shlex.join(command)],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=config['timeout'])
                finally:
                    subprocess.run(scp+['-r',f'root@{args.guest_ip}:/tmp/ae-output',str(output/'measurements')],check=True,timeout=90)
        finally:
            try:
                if process is not None and process.poll() is None:
                    with (output/'dmesg.log').open('w') as log:subprocess.run(ssh+['dmesg'],stdout=log,stderr=subprocess.STDOUT,timeout=15)
            finally:
                vm.stop_vm(args,process)


def validate_war_rows(rows, expected, key=None, arm=None):
    if len(rows) != expected:
        raise ValueError('Missing WAR edit rows')
    seen = set()
    for row in rows:
        if row.get('error'):
            raise ValueError('WAR infrastructure or replay error: '+row['error'])
        if key is not None:
            if row.get('instance') != key or row.get('fs_arm') != arm or row['edit_idx'] in seen:
                raise ValueError('WAR row identity mismatch or duplicate')
            seen.add(row['edit_idx'])
        if row.get('measurement_status') == 'excluded-input':
            if (row.get('exclusion_reason') != 'historical-diff-out-of-bounds'
                    or row.get('applied_ok') is not False
                    or row.get('copyup_bytes') is not None or row.get('phys_bytes') is not None):
                raise ValueError('Invalid input exclusion; missing measurements must not be zeros')
            number(row['file_size_bytes'], 'file_size_bytes')
            continue
        for field in ('file_size_bytes', 'copyup_bytes', 'phys_bytes'):
            number(row[field], field)


def main():
    if len(sys.argv)==3 and sys.argv[1]=='--guest-config':return guest_run(Path(sys.argv[2]))
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--experiment',choices=['figure-08-deltabox','figure-09','correctness'],required=True)
    p.add_argument('--config',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--forks',nargs='+',type=int,default=[1,4,16,64]);p.add_argument('--actions',type=Path)
    p.add_argument('--arm',choices=['ext4','xfs','xfs_reflink']);p.add_argument('--input-key')
    p.add_argument('--timeout',type=int,default=1800);p.add_argument('--dry-run',action='store_true')
    args=p.parse_args(); release=from_environment();config=load_config(args.config);output=args.out.resolve();output.mkdir(parents=True,exist_ok=False)
    if args.experiment=='figure-09' and not all((args.actions,args.arm,args.input_key)):p.error('WAR requires actions, arm and input-key')
    record={'experiment':args.experiment,'status':'preparing','analysis_mode':'fresh-measurement','run_purpose':run_purpose('full-trace'),'runtime':repository_state(),'release':release,'host':host_state()}
    path=output/'run.json'
    try:
        sources=build_guest_archive(REPO_ROOT,HERE/'deltabox/guest',output/'guest.tar')
        extras,experiment=build_extra(args,output)
        config['data_xfs']=str(configured_path(config,'images_dir')/'data-tools.xfs')
        images={key:cached_digest(configured_path(config,key), AE_ROOT/'work/image-hashes.json') for key in ('kernel','base_xfs','data_xfs')}
        if config.get('work_dir'):
            record['work_dir'] = str(configured_path(config, 'work_dir'))
        record.update(experiment,sources=sources,extra_sources=extras,images=images,timeout=args.timeout,
            **{key:str(configured_path(config,key)) for key in images})
        record['status']='planned' if args.dry_run else 'running';write_json(path,record)
        if args.dry_run:return 0
        result=execute(['unshare','--mount','--net','--propagation','private',sys.executable,str(Path(__file__).resolve()),'--guest-config',str(path)],output/'process',cwd=REPO_ROOT,timeout=args.timeout+300)
        if result['status']!='ok':raise RuntimeError('VM experiment failed; see process/stdout.log and guest.log')
        if args.experiment=='figure-08-deltabox':
            rows=json.loads((output/'measurements/fanout.json').read_text())
            if [r['forks'] for r in rows]!=args.forks or any(not r.get('success') or r.get('success_count')!=r['forks'] for r in rows):raise ValueError('Incomplete fanout state verification')
            for row in rows:number(row['ready_e2e_ms'],'ready_e2e_ms')
        elif args.experiment=='figure-09':
            rows=jsonl(output/'measurements'/(args.input_key+'_'+args.arm+'.jsonl'))
            validate_war_rows(rows, record['expected_edits'])
        elif not json.loads((output/'measurements/correctness.json').read_text()).get('ok'):raise ValueError('Correctness assertion failure')
        artifacts = list((output/'measurements').glob('*'))
        if (output/'host-storage.json').exists():
            artifacts.append(output/'host-storage.json')
        record['artifacts']=artifact_records(output,artifacts)
        record['status']='ok'
    except BaseException as error:
        record.update(status='failed',error=f'{type(error).__name__}: {error}');raise
    finally:write_json(path,record)
    return 0

if __name__ == '__main__':
    from repro.common import install_termination_handler
    install_termination_handler()
    raise SystemExit(main())
