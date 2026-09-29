"""Bounded Cube probe: preserve official clone timers and retain numeric checks."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time

P = Path(__file__).resolve().parents[2]
DRIVER = P / 'ae/vendor/finalbench/official_sandbox_fork/bench_official_fork.py'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', type=Path)
    ap.add_argument('--forks', default='1,16')
    ap.add_argument('--mem-mib', type=int, default=64)
    ap.add_argument('--cube-api-url', required=True)
    ap.add_argument('--cube-template', required=True)
    ap.add_argument('--cube-proxy-node-ip', default='127.0.0.1')
    ap.add_argument('--timeout', type=int, default=300)
    ap.add_argument('--request-timeout', type=float, default=120)
    ap.add_argument('--exec-timeout', type=float, default=60)
    ap.add_argument('--self-check', action='store_true')
    args = ap.parse_args()
    if min(args.mem_mib, args.timeout, args.request_timeout, args.exec_timeout) <= 0:
        ap.error('memory size and timeouts must be positive')
    if not args.self_check and args.out is None:
        ap.error('--out is required for measurement')
    if not args.self_check:
        args.out.parent.mkdir(parents=True, exist_ok=True)
    if __package__:
        from .cube_client_context import reuse_default_ssl_contexts
    else:
        from cube_client_context import reuse_default_ssl_contexts
    with reuse_default_ssl_contexts() as client_context:
        return run_benchmark(args, ap, client_context)


def run_benchmark(args, ap, client_context):
    spec = importlib.util.spec_from_file_location('official_cube_probe_driver', DRIVER)
    driver = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = driver
    spec.loader.exec_module(driver)
    signal.signal(signal.SIGTERM, driver._ae_interrupted)
    from cubesandbox import Sandbox
    from cubesandbox._exceptions import SandboxNotFoundError, TemplateNotFoundError
    if args.self_check:
        assert hasattr(Sandbox.create, '__func__')
        assert hasattr(Sandbox.delete_snapshot, '__func__')
        assert all(callable(getattr(Sandbox, name)) for name in ('create_snapshot', 'kill', 'clone'))
        print('Cube probe imports and SDK method bindings verified; no API call')
        return 0
    events, checks = [], []
    owned_sandboxes, owned_snapshots = {}, {}
    killed, deleted = set(), set()
    uncertain_creation = []
    guard = threading.Lock()

    def wrapped(name, fn):
        def call(*a, **kw):
            start = time.time_ns()
            before = time.perf_counter_ns()
            record = {'operation': name, 'started_ns': start}
            if name in ('create_snapshot', 'kill_sandbox'):
                record['target_id'] = a[0].sandbox_id
            elif name == 'delete_snapshot':
                record['target_id'] = a[1] if len(a) > 1 else kw.get('snapshot_id')
            elif name == 'create_sandbox':
                record['template'] = kw.get('template')
                record['metadata'] = kw.get('metadata')
            try:
                value = fn(*a, **kw)
                record['ok'] = True
                record['sandbox_id'] = getattr(value, 'sandbox_id', None)
                with guard:
                    if name == 'create_sandbox':
                        owned_sandboxes[value.sandbox_id] = value
                    elif name == 'create_snapshot':
                        record['snapshot_id'] = value.snapshot_id
                        owned_snapshots[value.snapshot_id] = a[0]._config
                    elif name == 'delete_snapshot':
                        deleted.add(record['target_id'])
                    elif name == 'kill_sandbox':
                        killed.add(record['target_id'])
                return value
            except BaseException as exc:
                record.update(ok=False, error=type(exc).__name__ + ': ' + str(exc))
                if name in ('create_sandbox', 'create_snapshot'):
                    uncertain_creation.append(record.copy())
                raise
            finally:
                record['ended_ns'] = time.time_ns()
                record['ms'] = (time.perf_counter_ns() - before) / 1e6
                with guard:
                    events.append(record)
        return call

    Sandbox.create_snapshot = wrapped('create_snapshot', Sandbox.create_snapshot)
    Sandbox.create = classmethod(wrapped('create_sandbox', Sandbox.create.__func__))
    Sandbox.delete_snapshot = classmethod(wrapped('delete_snapshot', Sandbox.delete_snapshot.__func__))
    Sandbox.kill = wrapped('kill_sandbox', Sandbox.kill)
    original_shell = driver.cube_run_shell
    expected_bytes = args.mem_mib * 1024 * 1024
    expected_checksum = sum(i % 251 for i in range(expected_bytes // 4096)) & 0xffffffff

    def verified_shell(sb, command, timeout):
        text = original_shell(sb, command, timeout)
        if "s.sendall(b'touch" in command:
            fields = dict(re.findall(r'(token|bytes|checksum|requests|pid)=([^\s]+)', text))
            expected_token = re.search(r"assert 'OK token=([^']+)'", command).group(1)
            assert fields['token'] == expected_token, fields
            assert int(fields['bytes']) == expected_bytes, fields
            assert int(fields['checksum']) == expected_checksum, fields
            assert int(fields['requests']) >= 2, fields
            with guard:
                checks.append({'sandbox_id': sb.sandbox_id, 'response': fields})
        return text

    driver.cube_run_shell = verified_shell
    forks = driver.parse_forks(args.forks)
    if any(n not in (1,4,16,64) for n in forks) or len(set(forks)) != len(forks):
        ap.error('--forks must select unique values from 1,4,16,64')
    settings = argparse.Namespace(cube_api_url=args.cube_api_url, cube_template=args.cube_template,
        cube_proxy_node_ip=args.cube_proxy_node_ip, timeout=args.timeout, request_timeout=args.request_timeout,
        exec_timeout=args.exec_timeout, mem_mib=args.mem_mib, max_workers=64)
    result = {'host_cpus': sorted(os.sched_getaffinity(0)),
              'host_status': [s for s in Path('/proc/self/status').read_text().splitlines()
                              if s.startswith(('Cpus_allowed_list:', 'Mems_allowed_list:'))],
              'expected_bytes': expected_bytes, 'expected_checksum': expected_checksum,
              'client_context_policy': client_context}
    result['rows'] = []
    try:
        for n in forks:
            row = driver.bench_cube(settings, [n])[0]
            result['rows'].append(row)
            if not row['success']:
                break
    finally:
        cleanup = []
        targets = [('sandbox', key, sb.kill) for key, sb in list(owned_sandboxes.items()) if key not in killed]
        targets += [('snapshot', key, lambda key=key, cfg=cfg: Sandbox.delete_snapshot(key, config=cfg))
                    for key, cfg in list(owned_snapshots.items()) if key not in deleted]
        for kind, key, operation in targets:
            item = {'kind': kind, 'id': key, 'ok': False}
            for attempt in range(3):
                try:
                    operation()
                    item.update(ok=True, attempts=attempt+1)
                    break
                except (SandboxNotFoundError, TemplateNotFoundError):
                    item.update(ok=True, attempts=attempt+1, absent=True)
                    break
                except Exception as exc:
                    item['error'] = str(exc)
                    time.sleep(.5)
            cleanup.append(item)
        result.update(api_events=events, memory_checks=checks, cleanup=cleanup,
                      owned_sandbox_ids=list(owned_sandboxes), owned_snapshot_ids=list(owned_snapshots),
                      uncertain_creation=uncertain_creation)
        result['cleanup_ok'] = all(c['ok'] for c in cleanup) and not uncertain_creation
        result['ok'] = (len(result['rows']) == len(forks) and all(r['success'] for r in result['rows'])
                        and len(checks) == sum(forks) and all(e['ok'] for e in events)
                        and result['cleanup_ok'])
        for row in result['rows']:
            unique = len({c['sandbox_id'] for c in row.get('children', [])}) == row['forks']
            if not unique or not result['cleanup_ok']:
                row['success'] = False
                result['ok'] = False
        (args.out.parent / 'cube-audit.json').write_text(json.dumps(result, indent=2) + '\n')
        args.out.write_text(json.dumps(result['rows'], indent=2) + '\n')
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
