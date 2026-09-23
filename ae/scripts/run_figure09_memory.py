#!/usr/bin/env python3
"""One-command Figure 9 measurement on private noswap RAM disks."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'ae'), str(ROOT/'ae/runners')]
from release.lock import verify
from repro.common import configured_path, load_config, write_json
from vm_experiment import require_memory_workdir, cached_digest


def stage(config_path, output):
    """Called only under the pinning wrapper and private mount/net namespaces."""
    config = load_config(config_path)
    work = ROOT/'ae/work'; work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='f9-', dir=work) as directory:
        memory = Path(directory)
        subprocess.run(['mount','-t','tmpfs','-o','size=24G,noswap,mode=0700',
                        'figure09-memory',str(memory)],check=True)
        try:
            backing = require_memory_workdir(memory)
            inputs = dict(base_xfs=configured_path(config,'base_xfs'),
                          data_xfs=configured_path(config,'images_dir')/'data-tools.xfs')
            bindings = {}
            for key,source in inputs.items():
                target = memory/('base.xfs' if key=='base_xfs' else 'data-tools.xfs')
                before = cached_digest(source,work/'image-hashes.json')
                print('Staging into noswap tmpfs: '+str(source),flush=True)
                subprocess.run(['cp','--sparse=always','--reflink=never',str(source),str(target)],check=True)
                after = cached_digest(target,work/'image-hashes.json')
                if before['sha256'] != after['sha256']:
                    raise ValueError('RAM image differs from its recorded source: '+key)
                bindings[key] = dict(source=before,staged=after)
            measured = {key:config[key] for key in ('repository_fallback',) if key in config}
            measured.update({key:str(configured_path(config,key)) for key in ('kernel','payload','moatless_venv')})
            measured.update(base_xfs=str(memory/'base.xfs'),images_dir=str(memory),work_dir=str(memory),
                            vcpus=4,mem_mib=8192,timeout=1800,
                            measurement=dict(pin=True,numa_node=2,cpus='52-55'))
            write_json(output/'config.json',measured)
            write_json(output/'staging.json',dict(backing=backing,images=bindings,
                       staging_outside_measurement=True,
                       numa_policy=subprocess.check_output(['numactl','--show'],text=True)))
            yield_config = output/'config.json'
            command = [sys.executable,str(ROOT/'ae/scripts/run_figure09_cohort.py'),
                       '--config',str(yield_config)]
            limit = json.loads((output/'request.json').read_text())['limit']
            if limit is None:
                subprocess.run(command+['--output',str(output/'check'),'--limit','1'],check=True)
            subprocess.run(command+['--output',str(output/'raw'),'--release-staged-base']+
                           (['--limit',str(limit)] if limit else []),check=True)
        finally:
            subprocess.run(['umount',str(memory)],check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'ae/configs/spr4numa-review.json')
    parser.add_argument('--output',type=Path,required=True,help='New output directory')
    parser.add_argument('--limit',type=int,help='First N inputs only; explicitly marked quick-check')
    parser.add_argument('--lock',type=Path,default=Path(os.environ.get('DELTABOX_RELEASE_LOCK',ROOT/'release/candidate-lock.json')))
    parser.add_argument('--inside',action='store_true',help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error('limit must be positive')
    output = args.output.resolve()
    if args.inside:
        os.environ['DELTABOX_RELEASE_LOCK'] = str(output/'source-lock.json')
        verify(output/'source-lock.json')
        stage(args.config.resolve(),output)
        return 0
    release = verify(args.lock.resolve())
    output.mkdir(parents=True,exist_ok=False)
    shutil.copyfile(args.lock,output/'source-lock.json')
    write_json(output/'request.json',dict(config=str(args.config.resolve()),limit=args.limit,
               release=release,protocol='historical suffix write; fresh filesystem per input/arm',
               host=dict(numa_node=2,cpus='52-55',maximum_pstate=True),guest=dict(vcpus=4,mem_mib=8192)))
    print('Figure 9 output: '+str(output),flush=True)
    print('Progress log: '+str(output/'environment/command.log'),flush=True)
    with (output/'prepare.log').open('w') as log:
        subprocess.run([sys.executable,str(ROOT/'ae/reproduce.py'),'prepare'],
                       stdout=log,stderr=subprocess.STDOUT,check=True)
    command = [sys.executable,str(ROOT/'ae/scripts/run_pinned_measurement.py'),
        '--node','2','--cpus','52-55','--out',str(output/'environment'),'--timeout','14400','--',
        'unshare','--mount','--net','--propagation','private',sys.executable,str(Path(__file__).resolve()),
        '--inside','--config',str(args.config.resolve()),'--output',str(output)]
    privilege = [] if os.geteuid()==0 else ['sudo','-n']
    try:
        with (output/'launcher.log').open('w') as log:
            subprocess.run(privilege+command,stdout=log,stderr=subprocess.STDOUT,check=True)
    finally:
        subprocess.run(privilege+[sys.executable,str(ROOT/'ae/scripts/run_review.py'),
            '--publish-output',str(output)],check=True)
    suite = json.loads((output/'raw/suite.json').read_text())
    write_json(output/'coverage.json',dict(release=release,run_purpose=suite['run_purpose'],
        status=suite['status'],coverage=[dict(experiment='figure-09',status=suite['status'],
        planned_jobs=len(suite['jobs']),successful_jobs=suite['successful_jobs'],
        reasons=[],unavailable_jobs=[],unavailable_arms=[])]))
    subprocess.run([sys.executable,str(ROOT/'ae/repro/analysis.py'),'--source','fresh',
        '--input',str(output/'raw'),'--output',str(output/'analysis')],check=True)
    subprocess.run([sys.executable,str(ROOT/'ae/repro/plot.py'),'--input',str(output/'analysis/summary.json'),
        '--output',str(output/'plots')],check=True)
    subprocess.run([sys.executable,str(ROOT/'ae/scripts/build_review_comparison.py'),
        '--analysis',str(output/'analysis/summary.json'),'--plots',str(output/'plots/plots.json'),
        '--coverage',str(output/'coverage.json'),'--output',str(output/'comparison')],check=True)
    print('Complete Figure 9 evidence and plots: '+str(output),flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
