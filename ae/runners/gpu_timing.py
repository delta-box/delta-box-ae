#!/usr/bin/env python3
"""Plan, check and measure Figure 8(b); planning never imports CUDA frameworks."""
from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from repro import gpu_protocol as protocol
from repro.common import digest, file_record, host_state, repository_state, write_json
from repro.process import execute
from release.lock import from_environment

WORKER = Path(__file__).with_name('gpu_worker.py')


def query_gpus():
    if shutil.which('nvidia-smi') is None:
        raise RuntimeError('nvidia-smi is not available')
    response = subprocess.run(['nvidia-smi', '--query-gpu=index,uuid,name,memory.total,driver_version',
                               '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=15, check=True)
    devices = []
    for row in csv.reader(io.StringIO(response.stdout)):
        if len(row) != 5:
            raise RuntimeError('unexpected nvidia-smi inventory')
        index, uuid, name, memory, driver = [x.strip() for x in row]
        devices.append(dict(index=index, uuid=uuid, name=name, memory_total_mib=float(memory), driver=driver))
    active = subprocess.run(['nvidia-smi', '--query-compute-apps=pid,gpu_uuid', '--format=csv,noheader,nounits'],
                            capture_output=True, text=True, timeout=15, check=True)
    processes = []
    for row in csv.reader(io.StringIO(active.stdout)):
        if not row or not any(x.strip() for x in row):
            continue
        if len(row) != 2:
            raise RuntimeError('unexpected nvidia-smi process list')
        processes.append(dict(pid=int(row[0].strip()), gpu_uuid=row[1].strip()))
    return devices, processes


def check_resources(config):
    checks, software, selected, processes = [], {}, [], []
    def check(name, ok, detail):
        checks.append(dict(name=name, ok=bool(ok), detail=detail))
    check('Linux', sys.platform == 'linux', sys.platform)
    model = Path(config['model_path']) if config['model_path'] else None
    check('local model', model is not None and model.is_dir(), str(model) if model else 'supply --model or model_path')
    if model is not None and model.is_dir():
        check('model config', (model / 'config.json').is_file(), str(model / 'config.json'))
        check('model weights', any(model.glob('*.safetensors')) or any(model.glob('pytorch_model*.bin')), 'local HF weights required')
    needed = max(case['num_gpus'] for case in protocol.cases(config))
    check('allocated device IDs', len(config['devices']) >= needed, f'{needed} GPUs required; selected {config["devices"]}')
    for phase in config['phases']:
        python = config[phase + '_python']
        try:
            result = subprocess.run([python, str(WORKER), '--probe-packages', phase],
                                    capture_output=True, text=True, timeout=30)
            details = json.loads(result.stdout)
            software[phase] = details
            check(phase + ' Python/packages', result.returncode == 0 and details.get('ok'), details)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            check(phase + ' Python/packages', False, str(error))
    try:
        inventory, all_processes = query_gpus()
        for requested in config['devices']:
            candidates = [g for g in inventory if requested in (g['index'], g['uuid'])]
            if len(candidates) != 1:
                check('GPU ' + requested, False, 'device not found; select full GPU indices/UUIDs, not MIG slices')
            else:
                selected.append(candidates[0])
                check('GPU ' + requested, True, candidates[0])
        uuids = {g['uuid'] for g in selected}
        check('distinct physical GPUs', len(uuids) == len(selected), 'index and UUID aliases must not select the same device twice')
        processes = [p for p in all_processes if p['gpu_uuid'] in uuids]
        check('idle GPUs', not processes or config['allow_busy'],
              dict(processes=processes, allow_busy=config['allow_busy']))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        check('NVIDIA inventory', False, str(error))
    return dict(ok=all(row['ok'] for row in checks), checks=checks, software=software,
                selected_gpus=selected, initial_gpu_processes=processes,
                note='Package probes do not load the model or initialize torch CUDA; actual execution validates CUDA/BF16.')


def plan(config):
    rows = protocol.cases(config)
    return dict(schema_version=1, kind='gpu-timing-plan', status='planned', gpu_executed=False,
                model_label=config['model_label'], protocol=config, cases=rows,
                required_simultaneous_gpus=max(row['num_gpus'] for row in rows),
                case_count=len(rows), measured_repetitions=sum(row['reps'] for row in rows),
                model_required=config['model_path'] is None,
                resource_reference=dict(paper_gpu='NVIDIA H20 96 GB', paper_max_gpus=4,
                    historical_single_train_peak_allocated_mib=38869,
                    historical_fsdp_peak_allocated_mib=34181,
                    note='Historical allocated memory is not a guaranteed minimum; driver/cache/framework reserves add overhead.'),
                notes=['Generation and B1/B4 training use one GPU; B16/B64 training use FSDP.',
                       'T_train includes forward, backward, collectives and optimizer; initialization/warmup excluded.',
                       'paper-template historically emitted about 307–308 tokens despite max_tokens=512; actual counts are retained.',
                       'fixed-tokens is a distinct protocol; no extrapolation or automatic OOM downscaling.'])


def wait_gpu_quiet(inventory, selected, initial_pids, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        _, live = inventory()
        extra = [p for p in live if p['gpu_uuid'] in selected and p['pid'] not in initial_pids]
        if not extra or time.monotonic() >= deadline:
            return extra
        time.sleep(min(.05, max(0, deadline - time.monotonic())))


def run_suite(config, output, *, keep_going=False, executor=None, preflight=None, inventory=None):
    output = Path(output).resolve()
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'output must be new: {output}')
    preflight = check_resources(config) if preflight is None else preflight
    if not preflight['ok']:
        raise ValueError('GPU prerequisites are not ready; run the check command and supply resources')
    # Preserve physical identities in the config. vLLM 0.8 requires numeric
    # CUDA_VISIBLE_DEVICES; the worker verifies that mapping before model load.
    config = copy.deepcopy(config)
    config['devices'] = [gpu['uuid'] for gpu in preflight['selected_gpus']]
    device_indices = [gpu['index'] for gpu in preflight['selected_gpus']]
    executor = execute if executor is None else executor
    inventory = query_gpus if inventory is None else inventory
    release = from_environment()
    print('Hashing local model files before GPU execution...', flush=True)
    identity = protocol.model_identity(config['model_path'])
    output.mkdir(parents=True)
    config_path = output / 'config.json'
    write_json(config_path, config)
    config_sha = digest(config_path)
    config_path.chmod(0o444)
    write_json(output / 'model-identity.json', identity)
    record = dict(schema_version=1, kind='gpu-timing-suite', status='running', source_kind='fresh',
                  model_label=config['model_label'], protocol=config, model_identity=identity,
                  host=host_state(), runtime=repository_state(), release=release,
                  preflight=preflight, cases=[], planned_cases=protocol.cases(config),
                  full_paper_batches=set(config['batches']) == set(protocol.BATCHES) and set(config['phases']) == set(protocol.PHASES),
                  config_sha256=config_sha, worker_source=file_record(WORKER),
                  protocol_source=file_record(Path(protocol.__file__)),
                  notes=['Measured GPU generation and training durations; RL occupation is calculated separately.',
                         'Run the separate CPU occupation model to calculate Figure 8(c).'])
    path = output / 'summary.json'
    write_json(path, record)
    selected = {g['uuid'] for g in preflight['selected_gpus']}
    initial_pids = {p['pid'] for p in preflight['initial_gpu_processes']}
    try:
        for case in record['planned_cases']:
            result_path = output / 'cases' / case['case_id'] / 'result.json'
            argv = protocol.command(config, case, config_path, result_path, WORKER)
            row = dict(case, status='running')
            record['cases'].append(row)
            write_json(path, record)
            print(f"{case['case_id']}: {case['num_gpus']} GPU(s); log {result_path.parent / 'stdout.log'}", flush=True)
            process = executor(argv, result_path.parent, cwd=WORKER.parents[2],
                               timeout=config['timeout_s'], env=protocol.child_environment(
                                   config, case, device_indices=device_indices))
            row['process'] = process
            if process.get('status') != 'ok':
                row.update(status='failed', error='GPU worker process failed; see stdout.log/process.json')
            else:
                try:
                    raw = json.loads(result_path.read_text())
                    timing = protocol.validate_result(raw, case, config_sha, config['devices'][:case['num_gpus']])
                    if raw.get('worker_source_sha256') != record['worker_source']['sha256']:
                        raise ValueError('GPU worker source changed during the run')
                    if raw.get('protocol_source_sha256') != record['protocol_source']['sha256']:
                        raise ValueError('GPU protocol source changed during the run')
                    row['timing_s'] = timing
                    row['result'] = dict(path=result_path.relative_to(output).as_posix(),
                                         sha256=digest(result_path), bytes=result_path.stat().st_size)
                    row['status'] = 'ok'
                except (OSError, ValueError, TypeError, KeyError) as error:
                    row.update(status='failed', error=f'invalid worker result: {error}')
            # Never launch the next model on a device left occupied by a failed
            # worker. We report unexpected PIDs rather than killing unowned jobs.
            extra = wait_gpu_quiet(inventory, selected, initial_pids)
            if extra:
                row.update(status='failed', cleanup_error=dict(unexpected_gpu_processes=extra))
                write_json(path, record)
                raise RuntimeError('selected GPUs still have new compute processes; inspect owned worker logs')
            write_json(path, record)
            if row['status'] != 'ok' and not keep_going:
                break
        if digest(config_path) != config_sha:
            raise RuntimeError('effective configuration changed during the run')
        if protocol.model_identity(config['model_path'])['sha256'] != identity['sha256']:
            raise RuntimeError('model files changed during the run')
        record['status'] = ('ok' if len(record['cases']) == len(record['planned_cases'])
                            and all(row['status'] == 'ok' for row in record['cases']) else 'failed')
    except BaseException as error:
        record.update(status='failed', error=f'{type(error).__name__}: {error}')
        for row in record['cases']:
            if row['status'] == 'running':
                row.update(status='failed', error=record['error'])
        raise
    finally:
        write_json(path, record)
    return record


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest='command', required=True)
    for name in ('plan', 'check', 'run'):
        p = commands.add_parser(name)
        p.add_argument('--config', type=Path, default=protocol.DEFAULT_CONFIG)
        p.add_argument('--model', dest='model_path', help='Local Qwen2.5-7B-Instruct HF snapshot directory')
        p.add_argument('--devices', help='Allocated full GPU indices/UUIDs; defaults to config or CUDA_VISIBLE_DEVICES')
        p.add_argument('--generation-python')
        p.add_argument('--training-python')
        p.add_argument('--batches', help='Comma-separated subset of 1,4,16,64')
        p.add_argument('--phase', choices=('all', *protocol.PHASES), default='all')
        p.add_argument('--prompt-mode', choices=('paper-template', 'fixed-tokens'))
        p.add_argument('--test', dest='quick_check', action='store_true', help='Quick check: B1 only, one warmup and one measured repetition per phase')
        p.add_argument('--smoke', dest='quick_check', action='store_true', help=argparse.SUPPRESS)
        p.add_argument('--allow-busy', action='store_true', default=None, help='Explicitly allow contended diagnostic runs')
        p.add_argument('--output', type=Path, required=name == 'run', help='New result directory for run; optional JSON file for plan/check')
        if name == 'run':
            p.add_argument('--keep-going', action='store_true')
    p = commands.add_parser('plot')
    p.add_argument('--input', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    return root


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.command == 'plot':
            from repro.figure08_plots import plot_gpu_timing
            print(json.dumps(plot_gpu_timing(json.loads(args.input.read_text()), args.output), indent=2))
            return 0
        if args.quick_check and args.batches:
            raise ValueError('choose --test or an explicit --batches list')
        config = protocol.load_config(args.config, model_path=args.model_path, devices=args.devices,
            generation_python=args.generation_python, training_python=args.training_python,
            batches=[int(x) for x in args.batches.split(',')] if args.batches else None,
            phases=[args.phase] if args.phase != 'all' else None, prompt_mode=args.prompt_mode,
            allow_busy=args.allow_busy, quick_check=args.quick_check)
        if args.command == 'run':
            result = run_suite(config, args.output, keep_going=args.keep_going)
            print(json.dumps({'status': result['status'], 'summary': str(args.output / 'summary.json')}, indent=2))
            return 0 if result['status'] == 'ok' else 1
        result = plan(config) if args.command == 'plan' else check_resources(config)
        if args.output:
            protocol.publish_json(args.output, result)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if args.command == 'plan' or result['ok'] else 2
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, subprocess.SubprocessError) as error:
        print(f'GPU script failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
