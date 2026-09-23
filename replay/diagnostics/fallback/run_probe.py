#!/usr/bin/env python3
"""Run standard replay with one real dead-template fault, in a private VM."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys

REPLAY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPLAY))
import run_instance as standard
from host_execution import Lane, RunningJob, cancel_jobs, cleanup_actions, finish_job, write_json
from provenance import file_digest, signature


def validate_fault(spec):
    rows = [json.loads(line) for line in (spec.output_dir / 'fallback-injection.jsonl').read_text().splitlines()]
    completed = [row for row in rows if row['kind'] == 'injection_completed']
    returned = [row for row in rows if row['kind'] == 'restore_returned' and row['injected']]
    if len(completed) != 1 or not completed[0]['registration_retained']:
        raise ValueError('exactly one registered-template death required')
    if len(returned) != 1 or returned[0]['actual_path'] not in ('criu', 'criu-lazy'):
        raise ValueError('the injected restore did not complete via CRIU fallback')
    results = standard.read_results(spec.results_path)
    restores = [row for row in results if row.get('kind') == 'restore']
    if not restores or restores[0].get('path') != returned[0]['actual_path']:
        raise ValueError('actual fallback path differs from normal replay result')
    first_restore = restores[0]['ev_i']
    post = [row for row in results if row.get('kind') == 'ckpt' and row['ev_i'] > first_restore]
    if not post or not all((row.get('worker_exec') or {}).get('ok') for row in post):
        raise ValueError('post-fallback worker/checkpoint evidence is missing')
    write_json(spec.output_dir / 'fallback-validation.json', {
        'ok': True, 'analysis_mode': 'diagnostic-only',
        'injections': len(completed), 'actual_path': returned[0]['actual_path'],
        'post_restore_checkpoints': len(post), 'standard_validate_results': 'passed',
        'note': 'Fault injection occurs inside the restore call; all timing is diagnostic only.',
    })


def run_guest(config_path):
    from release.lock import from_environment
    from_environment()
    spec = standard.load_instance_run(config_path)
    if file_digest(spec.schedule_path)['sha256'] != spec.config['schedule_sha256']:
        raise ValueError('schedule changed after preparation')
    for key, recorded in spec.config['images'].items():
        if signature(Path(spec.config[key])) != {k: v for k, v in recorded.items() if k != 'sha256'}:
            raise ValueError(f'image changed after preparation: {key}')
    wrapper = spec.output_dir / 'guest_probe.py'
    if file_digest(wrapper)['sha256'] != spec.config['diagnostic_wrapper']['sha256']:
        raise ValueError('diagnostic wrapper changed after preparation')
    with standard.managed_vm(spec) as machine:
        with standard.collect_results_on_exit(machine, spec):
            standard.upload_inputs(machine, spec)
            subprocess.run(machine.scp + [str(wrapper), f'root@{machine.args.guest_ip}:/tmp/fallback_probe.py'],
                           check=True, timeout=60)
            primary = None
            try:
                with (spec.output_dir / 'guest.log').open('w') as log:
                    subprocess.run(machine.ssh + ['python3 -u /tmp/fallback_probe.py'], check=True,
                                   timeout=spec.config['timeout'], stdout=log, stderr=subprocess.STDOUT)
            except BaseException as error:
                primary = error
                raise
            finally:
                cleanup_actions(spec.output_dir, [('collect fault injection evidence', lambda: subprocess.run(
                    machine.scp + [f'root@{machine.args.guest_ip}:/tmp/fallback-injection.jsonl', str(spec.output_dir)],
                    check=True, timeout=60), True)], primary)
    standard.validate_results(spec.results_path, spec.schedule_path)
    validate_fault(spec)
    return 0


def main():
    signal.signal(signal.SIGTERM, standard.interrupted)
    signal.signal(signal.SIGINT, standard.interrupted)
    if len(sys.argv) == 3 and sys.argv[1] == '--_run-config':
        return run_guest(Path(sys.argv[2]))
    args = standard.parse_args()
    if (args.mode != 'fast' or args.checkpoint_profile != 'runtime-default' or args.max_events != 5
            or args.adaptive or args.memory_policy or args.guest_env_json or args.prewarm_policy != 'off'):
        raise ValueError('requires fast/runtime-default, --max-events 5, standard policy, no prewarm/adaptive')
    spec = standard.prepare_single_run(args)
    events = [json.loads(line) for line in spec.schedule_path.read_text().splitlines()]
    first = next((i for i, event in enumerate(events) if event['type'] == 'restore'), None)
    if first is None or not any(event['type'] == 'ckpt' for event in events[first + 1:]):
        raise ValueError('the five-event prefix must include restore followed by checkpoint')
    wrapper = spec.output_dir / 'guest_probe.py'
    shutil.copyfile(Path(__file__).with_name('guest_probe.py'), wrapper)
    command = ['numactl', '--physcpubind=0-1', '--membind=0',
               'unshare', '--mount', '--net', '--propagation', 'private',
               sys.executable, str(Path(__file__).resolve()), '--_run-config', str(spec.config_path)]
    spec.config.update(analysis_mode='diagnostic-only', experiment='registered-template-death-fallback',
                       run_purpose='fault-injection', diagnostic_wrapper=file_digest(wrapper),
                       diagnostic_driver=file_digest(Path(__file__)), host_command=command,
                       host_binding={'cpus': '0-1', 'memory_node': 0})
    spec.save()
    if args.dry_run:
        print(shlex.join(command))
        return 0
    spec.mark('running')
    log = (spec.output_dir / 'runner.log').open('w')
    try:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    except BaseException as error:
        log.close()
        spec.mark('failed', error)
        raise
    job = RunningJob(spec, Lane(0, '0-1', 0), process, log)
    try:
        process.wait(timeout=spec.config['timeout'] + 300)
        finish_job(job)
        for name in ('fallback-injection.jsonl', 'fallback-validation.json', 'guest_probe.py'):
            spec.config['artifacts'].append(file_digest(spec.output_dir / name))
        spec.save()
    except BaseException as error:
        cancel_jobs([job], error)
        raise
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr)
        raise SystemExit(1)
