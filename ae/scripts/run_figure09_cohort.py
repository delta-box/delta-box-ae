#!/usr/bin/env python3
"""Run the frozen Figure 9 cohort sequentially in one disposable VM.

VM boot is outside the metric. Each input/arm creates a new 4 GiB loop
filesystem; every edit still gets a fresh overlay upper over its base file.
The measured writer and aggregation are unchanged from the historical replay.
Run inside private mount/network namespaces, with work_dir on noswap tmpfs.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'ae'), str(ROOT/'ae/runners')]
import vm
from vm_experiment import build_extra, require_memory_workdir, validate_war_rows
from provenance import build_guest_archive, cached_digest
from release.lock import from_environment
from repro.catalog import build_jobs
from repro.common import (AE_ROOT, artifact_records, configured_path, file_record,
                          host_state, jsonl, load_config, number, repository_state, write_json)


validate_rows = validate_war_rows


def release_staged_base(base, rootfs, work, receipt, evidence):
    """Release only this launcher's verified disposable copy after VM boot.

    The initial smoke VM must retain it for the full VM that follows. The final
    VM already owns an independent rootfs; retaining this extra image wastes
    scarce memory on the strictly bound NUMA node.
    """
    base, rootfs, work = (Path(p).resolve() for p in (base, rootfs, work))
    require_memory_workdir(work)
    staged = json.loads(Path(receipt).read_text())['images']['base_xfs']
    identity = staged['staged']
    stat = base.stat()
    actual = dict(path=str(base),device=stat.st_dev,inode=stat.st_ino,
                  size=stat.st_size,mtime_ns=stat.st_mtime_ns,ctime_ns=stat.st_ctime_ns)
    if (base.parent != work or base.name != 'base.xfs' or not work.name.startswith('f9-')
            or rootfs.parent.parent != work or rootfs.samefile(base)
            or staged['source']['path'] == str(base)
            or any(identity.get(key) != value for key,value in actual.items())):
        raise ValueError('Refusing to release an unverified or active staging base')
    # start_vm has copied rootfs and booted using rootfs, not base_xfs.
    base.unlink()
    write_json(evidence,dict(action='release-unused-staging-base',
               staged=identity,active_rootfs=str(rootfs),allocated_bytes=stat.st_blocks*512,
               before_measured_jobs=True))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int, help='First N inputs for infrastructure check, labeled smoke')
    parser.add_argument('--release-staged-base',action='store_true',
                        help='Release the wrapper-owned base copy after the final VM boots')
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error('limit must be positive')
    release = from_environment()
    if not release:
        raise ValueError('A frozen DELTABOX_RELEASE_LOCK is required')
    config = load_config(args.config)
    work = configured_path(config, 'work_dir')
    memory = require_memory_workdir(work)
    # Both VM disks must also be resident in the verified memory filesystem.
    for field in ('base_xfs',):
        require_memory_workdir(configured_path(config, field).parent)
    data = configured_path(config, 'images_dir')/'data-tools.xfs'
    require_memory_workdir(data.parent)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    purpose = 'smoke' if args.limit else 'full-cohort'
    jobs = build_jobs(['figure-09'], config, args.config.resolve(), out/'runs', args.limit)
    suite = dict(schema_version=1, analysis_mode='fresh-measurement', experiment='figure-09',
                 run_purpose=purpose, status='running', release=release,
                 runtime=repository_state(), host=host_state(), config_source=file_record(args.config),
                 memory_backing=memory, jobs=jobs,
                 isolation='one sequential VM; new loop filesystem per input/arm; fresh upper per edit')
    write_json(out/'suite.json', suite)
    boot = out/'vm'; boot.mkdir()
    sources = build_guest_archive(ROOT, AE_ROOT/'runners/deltabox/guest', boot/'guest.tar')
    paths = dict(kernel=configured_path(config,'kernel'), base_xfs=configured_path(config,'base_xfs'), data_xfs=data)
    images = {key: cached_digest(path, AE_ROOT/'work/image-hashes.json') for key,path in paths.items()}
    process = None
    with tempfile.TemporaryDirectory(prefix='war-', dir=work) as directory:
        runtime = Path(directory)
        vm_args = SimpleNamespace(**paths, run_rootfs=runtime/'rootfs.xfs', socket=runtime/'fc.sock',
            log=boot/'firecracker.log', ssh_pubkey=None, tap='war'+runtime.name[-8:],
            guest_ip=vm.GUEST_IP, vcpus=4, mem_mib=8192, ssh_timeout=120,
            reuse_rootfs=False, no_nat=True, inherit_process_group=True)
        ssh = ['ssh', *vm.ssh_opts(), '-o', 'ConnectTimeout=10', 'root@'+vm.GUEST_IP]
        scp = ['scp', '-O', *vm.ssh_opts(), '-o', 'ConnectTimeout=10']
        try:
            subprocess.run(['ip','link','set','lo','up'], check=True)
            process = vm.start_vm(vm_args)
            if args.release_staged_base:
                release_staged_base(paths['base_xfs'],vm_args.run_rootfs,work,
                                    out.parent/'staging.json',boot/'staging-base-release.json')
            with (boot/'guest.tar').open('rb') as source:
                subprocess.run(ssh+['mkdir -p /app; tar -xf - -C /app'], stdin=source, check=True, timeout=120)
            for index, job in enumerate(jobs, 1):
                from_environment()
                command = job['command']
                def value(flag): return command[command.index(flag)+1]
                output = Path(value('--out')); output.mkdir(parents=True, exist_ok=False)
                run_args = SimpleNamespace(experiment='figure-09', forks=[1,4,16,64], arm=value('--arm'),
                    input_key=value('--input-key'), actions=Path(value('--actions')), config=args.config)
                record = dict(experiment='figure-09', analysis_mode='fresh-measurement',
                    run_purpose=purpose, status='preparing', release=release, runtime=suite['runtime'],
                    host=suite['host'], images=images, sources=sources, memory_backing=memory,
                    batch_vm=str(boot), isolation=suite['isolation'], **{k:str(v) for k,v in paths.items()})
                print(f'[{index}/{len(jobs)}] {job["key"]}', flush=True)
                try:
                    extras, experiment = build_extra(run_args, output)
                    record.update(experiment, extra_sources=extras, status='running')
                    write_json(output/'run.json', record)
                    with (output/'extra.tar').open('rb') as source:
                        subprocess.run(ssh+['tar -xf - -C /app'], stdin=source, check=True, timeout=120)
                    # Outputs are task-owned and exported before the next job starts.
                    subprocess.run(ssh+['rm -rf /tmp/ae-output; mkdir /tmp/ae-output'], check=True)
                    with (output/'guest.log').open('w') as log:
                        try:
                            subprocess.run(ssh+['python3 -u /app/war.py'], stdout=log,
                                stderr=subprocess.STDOUT, check=True, timeout=1800)
                        finally:
                            subprocess.run(scp+['-r', 'root@'+vm.GUEST_IP+':/tmp/ae-output',
                                str(output/'measurements')], check=True, timeout=90)
                    rows = jsonl(output/'measurements'/(run_args.input_key+'_'+run_args.arm+'.jsonl'))
                    validate_rows(rows, record['expected_edits'], run_args.input_key, run_args.arm)
                    storage = json.loads((output/'measurements/storage.json').read_text())
                    if storage['backing_fstype'] != 'tmpfs' or 'noswap' not in storage['mount_options']:
                        raise ValueError('Guest loop image is not on noswap tmpfs')
                    record.update(status='ok', measured_edits=sum(r.get('measurement_status') != 'excluded-input' for r in rows),
                        excluded_inputs=sum(r.get('measurement_status') == 'excluded-input' for r in rows), artifacts=artifact_records(output, (output/'measurements').glob('*')))
                    job.update(status='ok', run_manifest=str(output/'run.json'),
                        actual_command=['python3','/app/war.py'], execution='sequential shared VM; fresh loop filesystem')
                except BaseException as error:
                    record.update(status='failed', error=f'{type(error).__name__}: {error}')
                    job.update(status='failed', error=record['error'])
                    raise
                finally:
                    write_json(output/'run.json',record)
                    write_json(out/'suite.json',suite)
        finally:
            try:
                if process is not None and process.poll() is None:
                    with (boot/'dmesg.log').open('w') as log:
                        subprocess.run(ssh+['dmesg'], stdout=log, stderr=subprocess.STDOUT, timeout=15)
            finally:
                vm.stop_vm(vm_args,process)
                suite['status'] = 'ok' if all(j.get('status') == 'ok' for j in jobs) else 'failed'
                suite['successful_jobs'] = sum(j.get('status') == 'ok' for j in jobs)
                write_json(out/'suite.json',suite)
    return 0 if suite['status'] == 'ok' else 1


if __name__ == '__main__':
    from repro.common import install_termination_handler
    install_termination_handler()
    raise SystemExit(main())
