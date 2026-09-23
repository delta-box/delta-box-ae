#!/usr/bin/env python3
"""Automatic SSH admission, measurement and evidence collection for Figure 8(b)."""
from __future__ import annotations

import argparse
import contextlib
import csv
from datetime import datetime, timezone
import fcntl
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tarfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'ae'))
from repro.common import digest, file_record, repository_state, write_json
from repro import gpu_protocol as protocol

DEFAULT_CONFIG = ROOT / 'ae/configs/figure08-remote.json'
EXPECTED = {f'{phase}-B{batch}' for phase in protocol.PHASES for batch in protocol.BATCHES}


def load_settings(path):
    config = json.loads(Path(path).read_text())
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', config['host']):
        raise ValueError('Invalid SSH host')
    for key in ('remote_root', 'python', 'model_path', 'generation_python', 'training_python'):
        if not isinstance(config[key], str) or not Path(config[key]).is_absolute():
            raise ValueError(f'{key} must be an absolute remote path')
    devices = config['devices']
    if not devices or len(set(devices)) != len(devices) or any(type(n) is not int or n not in range(8) for n in devices):
        raise ValueError('devices must be a unique subset of physical GPU indices 0–7')
    for key in ('samples', 'connect_timeout_s', 'timeout_s'):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f'{key} must be a positive integer')
    if config['samples'] < 2:
        raise ValueError('At least two idle observations are required')
    for key in ('max_memory_mib', 'max_utilization_pct', 'sample_interval_s'):
        value = config[key]
        if type(value) not in (int, float) or not 0 <= value < float('inf'):
            raise ValueError(f'{key} must be finite and nonnegative')
    for phase in protocol.PHASES:
        env = config.get(phase + '_env', {})
        if not isinstance(env, dict) or any(k not in ('LD_LIBRARY_PATH', 'PATH') or not isinstance(v, str) for k, v in env.items()):
            raise ValueError('Phase environment only supports explicit PATH/LD_LIBRARY_PATH strings')
    return config


def inventory():
    def query(fields, kind='gpu'):
        result = subprocess.run(['nvidia-smi', f'--query-{kind}={fields}', '--format=csv,noheader,nounits'],
                                check=True, capture_output=True, text=True, timeout=15)
        return list(csv.reader(io.StringIO(result.stdout)))
    rows = []
    for fields in query('index,uuid,name,memory.used,utilization.gpu'):
        index, identity, name, memory, util = [s.strip() for s in fields]
        rows.append(dict(index=int(index), uuid=identity, name=name,
                         memory_mib=float(memory), utilization_pct=float(util)))
    processes = [dict(pid=int(pid.strip()), uuid=identity.strip())
                 for pid, identity in query('pid,gpu_uuid', 'compute-apps')]
    return dict(at=datetime.now(timezone.utc).isoformat(), gpus=rows, processes=processes)


def idle_devices(observations, config):
    """Require stable physical identity, no compute PID and low memory/utilization."""
    if len(observations) < config['samples']:
        return []
    stable = None
    for observation in observations:
        busy = {p['uuid'] for p in observation['processes']}
        idle = {(g['index'], g['uuid']) for g in observation['gpus']
                if g['index'] in config['devices'] and g['uuid'] not in busy
                and 0 <= g['memory_mib'] <= config['max_memory_mib']
                and 0 <= g['utilization_pct'] <= config['max_utilization_pct']}
        stable = idle if stable is None else stable & idle
    return [dict(index=index, uuid=identity) for index, identity in sorted(stable)]


def probe(config):
    observations = []
    for i in range(config['samples']):
        if i:
            time.sleep(config['sample_interval_s'])
        observations.append(inventory())
    return dict(observations=observations, idle=idle_devices(observations, config))


def suites_for(devices):
    if not devices:
        return []
    suites = [('generation', [1, 4, 16, 64], devices[:1]), ('training', [1, 4], devices[:1])]
    if len(devices) >= 4:
        suites.append(('training', [16, 64], devices[:4]))
    return suites


@contextlib.contextmanager
def phase_environment(config, phase):
    updates = config.get(phase + '_env', {})
    saved = {key: os.environ.get(key) for key in updates}
    try:
        os.environ.update(updates)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def check_versions(check, config, phase):
    constraints = config.get('version_constraints')
    if not constraints:
        return
    path = checked_path(ROOT, constraints)
    expected = {}
    for line in path.read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        if line:
            name, version = line.split('==')
            expected[name] = version
    actual = check.get('software', {}).get(phase, {}).get('packages', {})
    mismatches = {name: dict(expected=expected[name], actual=version)
                  for name, version in actual.items() if name in expected and version != expected[name]}
    check['checks'].append(dict(name='Pinned top-level package versions', ok=not mismatches, detail=mismatches))
    check['ok'] = check['ok'] and not mismatches


def remote_run(root, *, probe_only=False):
    """Runs only on the GPU host. Locks coordinate our runs, never other users."""
    from ae.runners import gpu_timing
    from repro.process import execute
    from repro.common import install_termination_handler
    install_termination_handler()
    # A source subset has its own git identity, explicitly bound to the local snapshot.
    os.environ.pop('DELTABOX_RELEASE_LOCK', None)
    config = load_settings(root / 'remote-config.json')
    output = root / 'results'
    output.mkdir(exist_ok=False)
    report = dict(status='skipped', host=config['host'], reason='', suites=[], selected=[],
                  started_at=datetime.now(timezone.utc).isoformat(), source=json.loads((root / 'source.json').read_text()))
    acquired = []
    try:
        report['probe'] = probe(config)
        write_json(output / 'admission.json', report['probe'])
        if probe_only:
            report['reason'] = 'Probe only; no GPU measurements requested'
            return report
        lock_dir = Path(config['remote_root']) / 'locks'
        lock_dir.mkdir(parents=True, exist_ok=True)
        for device in report['probe']['idle']:
            lock = (lock_dir / (device['uuid'] + '.lock')).open('a')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                continue
            acquired.append(lock)
            report['selected'].append(device)
            if len(acquired) == 4:
                break
        if not acquired:
            report['reason'] = 'No idle/unreserved GPU among physical indices 0–7; CPU results unaffected'
            return report
        report['recheck'] = probe(config)
        available = {(g['index'], g['uuid']) for g in report['recheck']['idle']}
        if any((g['index'], g['uuid']) not in available for g in report['selected']):
            report['reason'] = 'GPU occupancy changed after admission; measurement skipped'
            return report
        selected = [g['uuid'] for g in report['selected']]
        planned = suites_for(selected)
        configs = []
        # Check every selected phase before loading any model.
        for index, (phase, batches, devices) in enumerate(planned):
            settings = protocol.load_config(model_path=config['model_path'], devices=devices, phases=[phase],
                batches=batches, generation_python=config['generation_python'], training_python=config['training_python'])
            with phase_environment(config, phase):
                check = gpu_timing.check_resources(settings)
                check_versions(check, config, phase)
            report.setdefault('preflight', []).append(check)
            configs.append((phase, settings))
        if not all(check['ok'] for check in report['preflight']):
            report['reason'] = 'Remote model, software or GPU prerequisites unavailable; see preflight checks'
            return report
        report['status'] = 'running'
        write_json(output / 'remote.json', report)
        for index, (phase, settings) in enumerate(configs):
            name = f'{index + 1:02d}-{phase}'
            row = dict(path=name + '/summary.json', status='running')
            report['suites'].append(row)
            write_json(output / 'remote.json', report)
            def guarded_execute(*args, **kwargs):
                # The suite also verifies CUDA/NVML identities before loading each worker.
                current = probe(config)
                write_json(output / f'{name}-admission-{uuid.uuid4().hex}.json', current)
                idle = {g['uuid'] for g in current['idle']}
                if not set(settings['devices']) <= idle:
                    raise RuntimeError('GPU became busy before worker launch; no other process was stopped')
                return execute(*args, **kwargs)
            try:
                with phase_environment(config, phase):
                    suite = gpu_timing.run_suite(settings, output / name, executor=guarded_execute)
                row['status'] = suite['status']
            except Exception as error:
                row.update(status='failed', error=f'{type(error).__name__}: {error}')
            summary = output / row['path']
            if summary.is_file():
                row['sha256'] = digest(summary)
            write_json(output / 'remote.json', report)
            if row['status'] != 'ok':
                break
        report['status'] = 'measured'
        report['reason'] = '' if len(selected) >= 4 else 'Fewer than four idle GPUs; training B16/B64 not executed'
    except Exception as error:
        report.update(status='failed' if report['status'] == 'running' else 'skipped',
                      reason=f'{type(error).__name__}: {error}')
    finally:
        for lock in acquired:
            lock.close()
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        write_json(output / 'remote.json', report)
    return report


def ssh_transport(config):
    command = ['ssh', '-o', 'BatchMode=yes', '-o', f'ConnectTimeout={config["connect_timeout_s"]}',
               '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']
    caller = os.environ.get('AE_HOSTED_CALLER_UID') or os.environ.get('SUDO_UID')
    if os.geteuid() == 0 and caller and caller.isdigit() and int(caller) != 0:
        import pwd
        command = ['sudo', '-n', '-H', '-u', pwd.getpwuid(int(caller)).pw_name, '--', *command]
    return command


def ssh(config, argv, *, timeout=60, **kwargs):
    return subprocess.run([*ssh_transport(config), config['host'], shlex.join(list(map(str, argv)))],
                          timeout=timeout, check=True, **kwargs)


def snapshot(output, config):
    """Send executable working bytes, not a mutable remote checkout or models."""
    paths = []
    for folder in ('ae/repro', 'ae/runners', 'ae/configs'):
        paths += [p for p in (ROOT / folder).rglob('*') if p.is_file() and p.suffix in ('.py', '.json')]
    paths += [ROOT / 'release/lock.py', Path(__file__).resolve()]
    paths += list((ROOT / 'ae').glob('requirements*.txt'))
    records = {p.relative_to(ROOT).as_posix(): digest(p) for p in sorted(set(paths))}
    identity = dict(repository=repository_state(), files=records,
                    note='Exact uploaded source subset; remote snapshot commit is not the full repository release identity')
    write_json(output / 'source.json', identity)
    write_json(output / 'remote-config.json', config)
    archive = output / 'source.tar'
    with tarfile.open(archive, 'w') as tar:
        for name in records:
            tar.add(ROOT / name, arcname='source/' + name, recursive=False)
        tar.add(output / 'source.json', arcname='source.json')
        tar.add(output / 'remote-config.json', arcname='remote-config.json')
    # Detect edits racing archive construction rather than measuring an unbound snapshot.
    with tarfile.open(archive) as tar:
        import hashlib
        for name, expected in records.items():
            if hashlib.sha256(tar.extractfile('source/' + name).read()).hexdigest() != expected:
                raise ValueError('Source changed while building remote snapshot')
    return archive


def checked_path(root, relative):
    path = root / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError('Evidence path escapes collected directory')
    return path


def collect_timings(output, remote, source):
    """Revalidate copied raw measurements before producing any plotted averages."""
    combined = dict(schema_version=1, kind='gpu-timing-suite', source_kind='fresh',
                    model_label='Qwen2.5-7B-Instruct', status='partial', cases=[])
    seen = set()
    model_sha = None
    for entry in remote['suites']:
        if 'sha256' not in entry:
            continue
        path = checked_path(output, entry['path'])
        if digest(path) != entry['sha256']:
            raise ValueError('Collected suite hash mismatch')
        suite = json.loads(path.read_text())
        # A failed suite may not have completed its post-run model/config checks.
        # Keep its evidence, but do not publish timings from it.
        if suite.get('status') != 'ok' or entry.get('status') != 'ok':
            continue
        config_path = path.parent / 'config.json'
        if digest(config_path) != suite['config_sha256']:
            raise ValueError('Collected configuration hash mismatch')
        config = protocol.load_config(config_path)
        if suite['protocol'] != config:
            raise ValueError('Suite protocol differs from frozen config')
        identity = suite['model_identity']['sha256']
        if model_sha is not None and model_sha != identity:
            raise ValueError('Cannot combine different model snapshots')
        model_sha = identity
        for row in suite['cases']:
            if row['status'] != 'ok':
                continue
            case = protocol.case_by_id(config, row['case_id'])
            if case['case_id'] in seen:
                raise ValueError('Duplicate GPU case')
            raw_path = checked_path(path.parent, row['result']['path'])
            if digest(raw_path) != row['result']['sha256'] or raw_path.stat().st_size != row['result']['bytes']:
                raise ValueError('Collected worker result hash/size mismatch')
            raw = json.loads(raw_path.read_text())
            for field, filename in (('worker_source_sha256', 'ae/runners/gpu_worker.py'),
                                    ('protocol_source_sha256', 'ae/repro/gpu_protocol.py')):
                if raw[field] != source['files'][filename]:
                    raise ValueError('Worker used different source bytes')
            timing = protocol.validate_result(raw, case, suite['config_sha256'], config['devices'][:case['num_gpus']])
            seen.add(case['case_id'])
            combined['cases'].append(dict(row, timing_s=timing,
                result=dict(row['result'], path=raw_path.relative_to(output).as_posix()),
                source_suite=dict(path=path.relative_to(output).as_posix(), sha256=digest(path))))
    combined['missing_cases'] = sorted(EXPECTED - seen)
    combined['status'] = 'ok' if seen else 'failed'
    combined['coverage_status'] = 'complete' if seen == EXPECTED else 'partial'
    combined['full_paper_batches'] = seen == EXPECTED
    write_json(output / 'summary.json', combined)
    return combined


def run_auto(output, config_path=DEFAULT_CONFIG, *, probe_only=False):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'ssh.log').touch()
    record = dict(mode='auto', status='skipped', reason='', successful_cases=0, expected_cases=8,
                  started_at=datetime.now(timezone.utc).isoformat())
    started = False
    try:
        config = load_settings(config_path)
        record['host'] = config['host']
        archive = snapshot(output, config)
        remote_root = str(Path(config['remote_root']) / ('run-' + uuid.uuid4().hex))
        record['remote_directory'] = remote_root
        # mkdir without -p for the run itself prevents accidental evidence replacement.
        ssh(config, ['mkdir', '-p', config['remote_root']], capture_output=True)
        ssh(config, ['mkdir', remote_root], capture_output=True)
        with archive.open('rb') as stream:
            ssh(config, ['tar', '-xf', '-', '-C', remote_root], stdin=stream, capture_output=True, timeout=120)
        remote_source = remote_root + '/source'
        for command in (['git', 'init', '-q', remote_source],
                        ['git', '-C', remote_source, 'add', '.'],
                        ['git', '-C', remote_source, '-c', 'user.name=DeltaBox AE', '-c', 'user.email=ae@localhost',
                         '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Exact uploaded Figure 8 source snapshot']):
            ssh(config, command, capture_output=True)
        argv = ['timeout', '--signal=TERM', '--kill-after=45', str(config['timeout_s']),
                config['python'], remote_source + '/ae/scripts/figure08_remote.py', '--remote-run', remote_root]
        if probe_only:
            argv.append('--probe-only')
        started = True
        execution_error = None
        try:
            with (output / 'ssh.log').open('w') as log:
                ssh(config, argv, stdout=log, stderr=subprocess.STDOUT, timeout=config['timeout_s'] + 90)
        except (subprocess.SubprocessError, OSError) as error:
            execution_error = f'{type(error).__name__}: {error}'
        # Preserve partial evidence even if SSH or the remote process failed.
        subprocess.run(['rsync', '-r', '--no-links', '--protect-args', '-e',
                        shlex.join(ssh_transport(config)),
                        config['host'] + ':' + remote_root + '/results/', str(output / 'results') + '/'],
                       check=True, capture_output=True, text=True, timeout=180)
        remote = json.loads((output / 'results/remote.json').read_text())
        source = json.loads((output / 'source.json').read_text())
        if remote['source'] != source:
            raise ValueError('Remote source identity differs from uploaded snapshot')
        record.update(status=remote['status'], reason=remote['reason'], selected=remote['selected'])
        if remote['suites']:
            combined = collect_timings(output / 'results', remote, source)
            record['successful_cases'] = len(combined['cases'])
            record['missing_cases'] = combined['missing_cases']
            failed = any(row['status'] != 'ok' for row in remote['suites'])
            record['status'] = ('complete' if not combined['missing_cases'] else
                                'partial' if combined['cases'] else 'failed')
            if failed:
                record['reason'] = (record['reason'] + '; ' if record['reason'] else '') + 'GPU execution failed; see raw suite logs'
            if combined['cases']:
                try:
                    from repro.figure08_plots import plot_gpu_timing
                    plot_gpu_timing(combined, output / 'plots')
                except Exception as error:
                    record['plot_error'] = f'{type(error).__name__}: {error}'
        if execution_error:
            record.update(status='partial' if record['successful_cases'] else 'failed', reason=execution_error)
    except Exception as error:
        record.update(status='failed' if started else 'skipped', reason=f'{type(error).__name__}: {error}')
        if isinstance(error, subprocess.CalledProcessError):
            stderr = error.stderr
            record['detail'] = stderr.decode(errors='replace') if isinstance(stderr, bytes) else stderr
    finally:
        record['finished_at'] = datetime.now(timezone.utc).isoformat()
        with (output / 'ssh.log').open('a') as log:
            log.write('\n' + json.dumps(record, ensure_ascii=False) + '\n')
        record['evidence'] = {name: file_record(output / name) for name in
                              ('source.json', 'results/remote.json', 'results/summary.json')
                              if (output / name).is_file()}
        write_json(output / 'manifest.json', record)
    return record


def finish_remote(review, analysis_dir, analyzed):
    """Keep upstream Figure 8(b)/(c) comparison pages with optional remote inputs."""
    from repro.review_gpu import GPU, THEORY, FANOUT
    from repro.gpu_occupation import main as occupation_main, inputs_from_measurements
    root = review.output / review.record['gpu_output']
    output = review.output / 'gpu' / review.attempt / 'comparison'
    output.mkdir(parents=True, exist_ok=True)
    gpu = review.record['gpu']
    panel = dict(experiment=GPU, title='Figure 8(b)', status=gpu['status'],
                 reasons=[gpu.get('reason', '')], artifacts=[])
    panels = [panel]
    summary = None
    try:
        for name, record in gpu.get('evidence', {}).items():
            path = checked_path(root, name)
            if digest(path) != record['sha256'] or path.stat().st_size != record['bytes']:
                raise ValueError('Changed remote evidence: ' + name)
        if gpu.get('successful_cases'):
            source = json.loads((root / 'source.json').read_text())
            remote = json.loads((root / 'results/remote.json').read_text())
            if source != remote['source']:
                raise ValueError('Remote source identity differs')
            combined = collect_timings(root / 'results', remote, source)
            summary = root / 'results/summary.json'
            from repro.figure08_plots import plot_gpu_timing
            plot_gpu_timing(combined, output / 'plots')
            panel.update(status='ok' if gpu['status'] == 'complete' else 'partial', input=file_record(summary),
                         artifacts=[file_record(output / 'plots' / ('figure-08b.' + ext)) for ext in ('png', 'pdf')])
    except Exception as error:
        summary = None
        panel.update(status='failed', reasons=[str(error)], artifacts=[])
        gpu['plot_error'] = str(error)
    selected = set(review.record['experiments'])
    if set(FANOUT).issubset(selected) and not review.limits:
        theory = dict(experiment=THEORY, title='Figure 8(c)', status='unavailable', artifacts=[], reasons=[])
        panels.append(theory)
        ready = (gpu['status'] == 'complete' and summary is not None and analyzed and
                 all(any(row['experiment'] == name and row['status'] == 'ok'
                         for row in review.record['coverage']) for name in FANOUT))
        if not ready:
            theory['reasons'] = ['Requires all eight fresh GPU cases and all three successful CPU fan-out measurements']
        else:
            try:
                host_summary = analysis_dir / 'summary.json'
                inputs = inputs_from_measurements(json.loads(summary.read_text()), json.loads(host_summary.read_text()))
                if (set(inputs['backends']) != {'deltabox', 'cube', 'e2b'} or inputs['source_kind'] != 'fresh'
                        or any(row['estimated'] for row in inputs['sandbox_timings'])):
                    raise ValueError('Theory requires fresh measured fan-out for all three backends')
                destination = output / 'theory'
                code = occupation_main(['--gpu-results', str(summary), '--fanout-summary', str(host_summary),
                                        '--output', str(destination), '--plot'])
                if code:
                    raise ValueError('Figure 8(c) calculation or plotting failed')
                theory.update(status='ok', input=file_record(destination / 'occupation.json'),
                              artifacts=[file_record(destination / 'plots' / ('figure-08c.' + ext)) for ext in ('png', 'pdf')])
            except Exception as error:
                theory.update(status='failed', reasons=[str(error)])
        review.record['coverage'] = [row for row in review.record['coverage'] if row['experiment'] != THEORY]
        review.record['coverage'].append(dict(experiment=THEORY, optional=True, status=theory['status'],
                                              successful_jobs=int(theory['status'] == 'ok'), reasons=theory['reasons']))
    metadata = output / 'comparison.json'
    write_json(metadata, dict(schema_version=1, release=review.record['release'], panels=panels))
    review.record['outputs']['gpu'] = str(root)
    return metadata


def report_lines(record, prefix):
    reason = record.get('reason', '').replace('\n', ' ')
    lines = ['', '## Figure 8(b) — automatic remote GPU measurement', '',
             f"Status: **{record['status']}**; successful cases: {record.get('successful_cases', 0)}/8.", '',
             f"Host: `{record.get('host', 'allinai2plus')}`; candidate physical GPUs: 0–7.", '', reason, '',
             'GPU results are optional and do not change CPU completion. Missing cases are never filled from historical data.', '']
    if prefix:
        lines += [f'[GPU manifest]({prefix}/manifest.json) · [SSH log]({prefix}/ssh.log)', '']
        if record.get('successful_cases'):
            lines += [f'[Raw summary]({prefix}/results/summary.json)', '']
            if not record.get('plot_error'):
                lines += [f'![Figure 8(b)]({prefix}/plots/figure-08b.png)', '']
    if record.get('plot_error'):
        lines += ['Plot unavailable: ' + record['plot_error'], '']
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--probe-only', action='store_true', help='Diagnostic admission check; never loads a GPU model')
    parser.add_argument('--remote-run', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.remote_run:
        remote_run(args.remote_run, probe_only=args.probe_only)
        return 0
    if not args.output:
        parser.error('--output is required')
    result = run_auto(args.output, args.config, probe_only=args.probe_only)
    (args.output / 'result.md').write_text('\n'.join(report_lines(result, '.')))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
