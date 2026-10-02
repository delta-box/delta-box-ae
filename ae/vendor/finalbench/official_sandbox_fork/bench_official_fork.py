#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import json
import os
import signal
import statistics
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


FORK_DEFAULTS = [1, 4, 16, 64]
MEM_SERVER_PORT = 38765


MEM_SERVER_CODE = r"""
import os
import signal
import socket
import threading
import time

HOST = "127.0.0.1"
PORT = int(os.environ.get("OFFICIAL_FORK_MEM_PORT", "38765"))
MEM_MIB = int(os.environ.get("OFFICIAL_FORK_MEM_MIB", "64"))
TOKEN = os.environ["OFFICIAL_FORK_TOKEN"]
PAGE = 4096

buf = bytearray(MEM_MIB * 1024 * 1024)
for i in range(0, len(buf), PAGE):
    buf[i] = (i // PAGE) % 251

marker = f"official-fork-state token={TOKEN} bytes={len(buf)} pid={os.getpid()}\n"
open("/tmp/official_fork_state.txt", "w").write(marker)

def touch_buffer():
    total = 0
    # Read one byte per page to force the restored child to fault/touch the
    # memory-backed state rather than just returning from a control-plane API.
    for i in range(0, len(buf), PAGE):
        total = (total + buf[i]) & 0xFFFFFFFF
    return total

requests = 0
lock = threading.Lock()

def handle(conn):
    global requests
    with conn:
        _ = conn.recv(1024)
        checksum = touch_buffer()
        with lock:
            requests += 1
            n = requests
        conn.sendall(
            f"OK token={TOKEN} bytes={len(buf)} checksum={checksum} requests={n} pid={os.getpid()}\n".encode()
        )

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((HOST, PORT))
sock.listen(128)
open("/tmp/official_fork_mem_server.ready", "w").write(str(time.time()))
while True:
    conn, _ = sock.accept()
    threading.Thread(target=handle, args=(conn,), daemon=True).start()
"""


_TEMPORAL_TRACE_HELPER = r'''
import json, threading, time
_trace_count = 0
_trace_guard = threading.Lock()
try:
    open(_trace_path, 'wb').close()
except Exception:
    pass
def _trace_endpoints(conn):
    try:
        return {'local': conn.getsockname(), 'peer': conn.getpeername()}
    except Exception:
        return {}
def _body_clock_ns(name):
    try:
        return getattr(time, name)()
    except Exception:
        return None
def _trace(stage, phase='unknown', endpoints=None, **fields):
    global _trace_count
    try:
        with _trace_guard:
            if _trace_count >= 64:
                return
            record = dict(seq=_trace_count, stage=stage, phase=phase, token=_trace_token,
                pid=os.getpid(), tid=threading.get_native_id(),
                wall_ns=time.time_ns(), monotonic_ns=time.monotonic_ns(),
                **(endpoints or {}), **fields)
            if _trace_count == 63:
                record = dict(seq=_trace_count, stage='trace_incomplete', reason='record_limit', pid=os.getpid())
            data = (json.dumps(record, separators=(',', ':')) + '\n').encode()
            # A cap is explicit evidence loss, never a chopped JSON event.
            if len(data) > 512:
                data = (json.dumps(dict(seq=_trace_count, stage='trace_incomplete', reason='record_bytes',
                    pid=os.getpid())) + '\n').encode()
            with open(_trace_path, 'ab') as stream:
                stream.write(data)
            _trace_count += 1
    except Exception:
        # Diagnostic I/O cannot replace the original request outcome.
        pass
'''


def temporal_trace_enabled() -> bool:
    return os.environ.get('DELTABOX_E2B_DIAGNOSTIC_TRACE') == '1'


def diagnostic_mem_server_code() -> str:
    """Trace only the original request stages, never individual page touches."""
    original = MEM_SERVER_CODE[MEM_SERVER_CODE.index('def handle(conn):'):MEM_SERVER_CODE.index('\nsock = socket.socket')]
    traced = '''def handle(conn):
    global requests
    phase = 'unknown'
    endpoints = _trace_endpoints(conn)
    try:
        with conn:
            _trace('recv_start', phase, endpoints)
            _ = conn.recv(1024)
            phase = 'warmup' if _.startswith(b'warmup') else 'verify' if _.startswith(b'touch') else 'unknown'
            _trace('recv_end', phase, endpoints, bytes=len(_))
            _trace('touch_start', phase, endpoints)
            body_m0 = _body_clock_ns('monotonic_ns')
            body_c0 = _body_clock_ns('thread_time_ns')
            checksum = touch_buffer()
            body_c1 = _body_clock_ns('thread_time_ns')
            body_m1 = _body_clock_ns('monotonic_ns')
            _trace('touch_end', phase, endpoints,
                body_mono_ns=[body_m0, body_m1], body_thread_ns=[body_c0, body_c1],
                body_timing='unknown' if None in (body_m0, body_m1, body_c0, body_c1) else 'captured')
            with lock:
                requests += 1
                n = requests
            _trace('send_start', phase, endpoints)
            conn.sendall(
                f"OK token={TOKEN} bytes={len(buf)} checksum={checksum} requests={n} pid={os.getpid()}\\n".encode()
            )
            _trace('send_end', phase, endpoints)
    except BaseException as error:
        _trace('exception', phase, endpoints, error=type(error).__name__)
        raise
    finally:
        _trace('exit', phase, endpoints)
'''
    code = MEM_SERVER_CODE.replace(original, traced)
    setup = "_trace_path = '/tmp/official_fork_server_trace.jsonl'\n_trace_token = TOKEN\n" + _TEMPORAL_TRACE_HELPER
    code = code.replace('requests = 0\n', setup + '\nrequests = 0\n', 1)
    return code.replace('    conn, _ = sock.accept()\n',
        "    conn, _ = sock.accept()\n    _trace('accept', endpoints=_trace_endpoints(conn))\n", 1)


def diagnostic_verify_shell(shell: str, token: str) -> str:
    prefix, code = shell.split("python3 - <<'PY'\n", 1)
    code, suffix = code.rsplit('\nPY\n', 1)
    code = code.replace("s = socket.create_connection(", "_trace('connect_start', 'verify')\ns = socket.create_connection(", 1)
    code = code.replace("s.sendall(", "_endpoints = _trace_endpoints(s)\n_trace('connect_end', 'verify', _endpoints)\n_trace('send_start', 'verify', _endpoints)\ns.sendall(", 1)
    code = code.replace('out = s.recv(4096).decode()', "_trace('send_end', 'verify', _endpoints)\n_trace('recv_start', 'verify', _endpoints)\nout = s.recv(4096).decode()\n_trace('recv_end', 'verify', _endpoints, bytes=len(out))", 1)
    setup = "import os\n_trace_path = '/tmp/official_fork_client_trace.jsonl'\n_trace_token = " + repr(token) + '\n' + _TEMPORAL_TRACE_HELPER
    wrapped = '\ntry:\n' + '\n'.join('    ' + line for line in code.splitlines())
    wrapped += "\n    _trace('success', 'verify', locals().get('_endpoints'))\nexcept BaseException as error:\n    _trace('exception', 'verify', locals().get('_endpoints'), error=type(error).__name__)\n    raise\nfinally:\n    _trace('exit', 'verify', locals().get('_endpoints'))\n"
    return prefix + "python3 - <<'PY'\n" + setup + wrapped + '\nPY\n' + suffix


@dataclass
class TimedStep:
    ok: bool
    ms: float
    error: str | None = None


def now_ms() -> float:
    return time.perf_counter() * 1000.0


def getenv_any(names: list[str], default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def parse_forks(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        n = int(part)
        if n < 1:
            raise ValueError(f"fork count must be >=1, got {n}")
        out.append(n)
    if not out:
        raise ValueError("empty fork list")
    return out


def summarize(vals: list[float]) -> dict[str, Any]:
    if not vals:
        return {"n": 0}
    ordered = sorted(vals)

    def pct(q: float) -> float:
        return ordered[int(q * (len(ordered) - 1))]

    return {
        "n": len(vals),
        "mean_ms": statistics.fmean(vals),
        "median_ms": statistics.median(vals),
        "p95_ms": pct(0.95),
        "min_ms": ordered[0],
        "max_ms": ordered[-1],
    }


def run_timed(fn) -> tuple[Any, TimedStep]:
    t0 = now_ms()
    try:
        return fn(), TimedStep(ok=True, ms=now_ms() - t0)
    except Exception as exc:
        return None, TimedStep(ok=False, ms=now_ms() - t0, error=f"{type(exc).__name__}: {exc}")


def check_command_result(result: Any) -> str:
    exit_code = getattr(result, "exit_code", 0)
    if exit_code not in (0, None):
        raise RuntimeError(
            f"command exit_code={exit_code} stderr={getattr(result, 'stderr', '')}"
        )
    return str(getattr(result, "stdout", "") or "")


def cube_run_shell(sb: Any, cmd: str, timeout: float) -> str:
    return check_command_result(sb.commands.run(cmd, timeout=timeout))


def e2b_run_shell(sb: Any, cmd: str, timeout: float) -> str:
    return check_command_result(sb.commands.run(cmd, timeout=timeout))


def cube_run_code(sb: Any, code: str, timeout: float) -> str:
    res = sb.run_code(code, timeout=timeout)
    text = getattr(res, "text", None)
    if text:
        return str(text)
    logs = getattr(res, "logs", None)
    stdout = getattr(logs, "stdout", None)
    if stdout:
        return "".join(str(x) for x in stdout)
    return ""


def e2b_run_code(sb: Any, code: str, timeout: float) -> str:
    cmd = getattr(sb, "commands")
    # E2B command API is the official data plane; use it to force the child
    # sandbox to be usable after creation from snapshot.
    result = cmd.run(f"python3 - <<'PY'\n{code}\nPY", timeout=timeout)
    if getattr(result, "exit_code", 0) not in (0, None):
        raise RuntimeError(f"command exit_code={result.exit_code} stderr={getattr(result, 'stderr', '')}")
    return str(getattr(result, "stdout", "") or "")


def start_mem_server_shell(*, mem_mib: int, token: str) -> str:
    code = diagnostic_mem_server_code() if temporal_trace_enabled() else MEM_SERVER_CODE
    encoded = base64.b64encode(code.encode()).decode()
    return f"""
set -euo pipefail
python3 - <<'PY'
import base64
code = base64.b64decode({encoded!r}).decode()
open('/tmp/official_fork_mem_server.py', 'w').write(code)
PY
rm -f /tmp/official_fork_mem_server.ready /tmp/official_fork_mem_server.log
if [ -f /tmp/official_fork_mem_server.pid ]; then
  old="$(cat /tmp/official_fork_mem_server.pid 2>/dev/null || true)"
  if [ -n "$old" ]; then kill "$old" >/dev/null 2>&1 || true; fi
fi
nohup env OFFICIAL_FORK_MEM_MIB={mem_mib} OFFICIAL_FORK_MEM_PORT={MEM_SERVER_PORT} OFFICIAL_FORK_TOKEN={token!r} \
  python3 /tmp/official_fork_mem_server.py >/tmp/official_fork_mem_server.log 2>&1 &
echo $! >/tmp/official_fork_mem_server.pid
python3 - <<'PY'
import socket, time
deadline = time.time() + 30
last = None
while time.time() < deadline:
    try:
        s = socket.create_connection(('127.0.0.1', {MEM_SERVER_PORT}), timeout=2)
        s.sendall(b'warmup\\n')
        out = s.recv(4096).decode()
        s.close()
        assert 'OK token={token}' in out, out
        print(out.strip())
        raise SystemExit(0)
    except Exception as exc:
        last = exc
        time.sleep(0.1)
print(open('/tmp/official_fork_mem_server.log').read())
raise SystemExit(f'mem server not ready: {{last}}')
PY
"""


def verify_mem_server_shell(*, token: str) -> str:
    shell = f"""
set -euo pipefail
python3 - <<'PY'
import os, socket
marker = open('/tmp/official_fork_state.txt').read()
assert 'official-fork-state' in marker, marker
assert 'token={token}' in marker, marker
# The caller's --exec-timeout bounds this guest command, including socket I/O.
# Response latency is measured, not a separate correctness deadline.
s = socket.create_connection(('127.0.0.1', {MEM_SERVER_PORT}), timeout=None)
s.sendall(b'touch\\n')
out = s.recv(4096).decode()
s.close()
assert 'OK token={token}' in out, out
assert 'checksum=' in out and 'bytes=' in out, out
print(out.strip())
PY
"""
    return diagnostic_verify_shell(shell, token) if temporal_trace_enabled() else shell


def cube_write_state(sb: Any, *, mem_mib: int, token: str, timeout: float) -> str:
    return cube_run_shell(sb, start_mem_server_shell(mem_mib=mem_mib, token=token), timeout)


def e2b_write_state(sb: Any, *, mem_mib: int, token: str, timeout: float) -> str:
    return e2b_run_shell(sb, start_mem_server_shell(mem_mib=mem_mib, token=token), timeout)


_PROC_OBSERVATION_CODE = r'''
import json, os, re, sys, time
from pathlib import Path
def read(path, limit=4096):
    try:
        with Path(path).open('rb') as stream: data = stream.read(limit + 1)
        return {'text': data[:limit].decode(errors='replace'), 'truncated': len(data) > limit}
    except OSError as error:
        return {'error': type(error).__name__}
def identity(pid):
    path = Path('/proc') / str(pid)
    value = read(path / 'stat')
    try: start = int(value['text'].rsplit(')', 1)[1].split()[19])
    except (KeyError, IndexError, ValueError): start = None
    return {'pid': pid, 'start_ticks': start, 'stat': value, 'cgroup': read(path / 'cgroup')}
def same_identity(left, right):
    return (left['start_ticks'] is not None and left['start_ticks'] == right['start_ticks']
        and left['pid'] == right['pid'] and 'text' in left['cgroup']
        and left['cgroup'].get('text', '').strip() == right['cgroup'].get('text', '').strip())
def process(pid):
    path = Path('/proc') / str(pid)
    result = identity(pid)
    result['schedstat'] = read(path / 'schedstat')
    result['wchan'] = read(path / 'wchan')
    result['stack'] = read(path / 'stack')
    try:
        tasks = sorted((path / 'task').iterdir(), key=lambda p: int(p.name))
        result['task_count'] = len(tasks)
        result['tasks'] = {p.name: {name: read(p / name) for name in ('stat', 'schedstat', 'wchan', 'stack')} for p in tasks[:8]}
        result['tasks_truncated'] = len(tasks) > 8
        result['userfault_fds'] = {}
        fds = sorted((path / 'fd').iterdir(), key=lambda p: int(p.name))
        result['fd_count'] = len(fds)
        result['fds_truncated'] = len(fds) > 256
        for fd in fds[:256]:
            try:
                if 'userfaultfd' in os.readlink(fd):
                    result['userfault_fds'][fd.name] = read(path / 'fdinfo' / fd.name)
            except OSError: pass
    except OSError as error: result['enumeration_error'] = type(error).__name__
    result['identity_after'] = identity(pid)
    result['identity_stable'] = same_identity(result, result['identity_after'])
    return result
'''

_GUEST_OBSERVATION_CODE = _PROC_OBSERVATION_CODE + r'''
result = {'captured_at': time.time(), 'files': {}}
for name in ('state.txt', 'mem_server.pid', 'mem_server.ready', 'mem_server.log'):
    result['files'][name] = read('/tmp/official_fork_' + name, 8192)
marker = result['files']['state.txt'].get('text', '')
match = re.search(r'\bpid=(\d+)\b', marker)
pid = int(match.group(1)) if match else None
pid_file = result['files']['mem_server.pid'].get('text', '').strip()
result['marker_pid'] = pid
result['pid_file_matches'] = pid is not None and pid_file == str(pid)
if pid is not None:
    before = identity(pid)
    args = read('/proc/' + str(pid) + '/cmdline').get('text', '').split('\0')
    result['expected_command'] = '/tmp/official_fork_mem_server.py' in args
    if result['expected_command'] and result['pid_file_matches']:
        observed = process(pid)
        final_args = read('/proc/' + str(pid) + '/cmdline').get('text', '').split('\0')
        stable = observed['identity_stable'] and same_identity(before, observed)
        result['server'] = dict(observed, ownership='owned-marker-command' if stable
            and '/tmp/official_fork_mem_server.py' in final_args else 'unknown')
    else: result['server_identity'] = 'unknown-or-gone'
print(json.dumps(result))
'''

_GUEST_TEMPORAL_OBSERVATION = r'''
result['temporal_scope'] = 'diagnostic only; post-failure; incomplete events are censored, counters have no pre-request baseline'
result['network_context'] = {}
for name, limit in (('/proc/net/snmp', 8192), ('/proc/net/netstat', 16384),
                    ('/proc/uptime', 128), ('/sys/devices/system/clocksource/clocksource0/current_clocksource', 128)):
    result['network_context'][name] = read(name, limit)
tcp = read('/proc/net/tcp', 16384)
if 'text' in tcp:
    lines = tcp['text'].splitlines()
    relevant = []
    for line in lines[1:]:
        fields = line.split()
        try:
            if any(int(fields[index].rsplit(':', 1)[1], 16) == 38765 for index in (1, 2)):
                relevant.append(line)
        except (IndexError, ValueError):
            pass
    tcp['text'] = '\n'.join(lines[:1] + relevant) + '\n'
result['network_context']['/proc/net/tcp'] = tcp
result['server_socket_fds'] = {}
if result.get('server', {}).get('ownership') == 'owned-marker-command':
    try:
        fds = sorted((Path('/proc') / str(pid) / 'fd').iterdir(), key=lambda p: int(p.name))
        result['server_socket_fds_truncated'] = len(fds) > 32
        for fd in fds[:32]:
            try:
                target = os.readlink(fd)
                if target.startswith('socket:'):
                    result['server_socket_fds'][fd.name] = target
            except OSError:
                pass
    except OSError as error:
        result['server_socket_fds_error'] = type(error).__name__
    result['server_socket_fds_identity'] = ('owned-marker-command'
        if same_identity(result['server'], identity(pid)) else 'unknown')
# Read last so a handler that has since completed leaves its chronology; never wait.
result['temporal_traces'] = {name: read('/tmp/official_fork_' + name + '_trace.jsonl', 64 * 512)
    for name in ('server', 'client')}
'''


def guest_observation_code() -> str:
    if not temporal_trace_enabled():
        return _GUEST_OBSERVATION_CODE
    return _GUEST_OBSERVATION_CODE.replace('print(json.dumps(result))',
        _GUEST_TEMPORAL_OBSERVATION + '\nprint(json.dumps(result))')


_SDK_OBSERVATION_CODE = r'''
import json, sys
from packaging.version import Version
from e2b import Sandbox
from e2b.connection_config import ConnectionConfig
try:
    request = json.load(sys.stdin)
    # Local client construction only: connect() can change the sandbox lifetime.
    sb = Sandbox(sandbox_id=request['id'], sandbox_domain=request['domain'],
        envd_version=Version(request['version']), envd_access_token=request['envd_token'],
        traffic_access_token=request['traffic_token'],
        connection_config=ConnectionConfig(**request['options']))
    value = sb.commands.run(request['command'], timeout=2, request_timeout=2)
    print(json.dumps({'exit_code': value.exit_code, 'observation': json.loads(value.stdout)}))
except Exception as error:
    # SDK exceptions can include connection credentials; retain only their class.
    print(json.dumps({'error': type(error).__name__}))
'''

_HOST_OBSERVATION_CODE = _PROC_OBSERVATION_CODE + r'''
request = json.load(sys.stdin)
result = {'captured_at': time.time(), 'vms': [], 'daemon': {'ownership': 'unknown'}}
root = Path('/sys/fs/cgroup/e2b')
for sandbox_id in request['ids']:
    if not re.fullmatch('[A-Za-z0-9]+', sandbox_id): continue
    for group in sorted(root.glob('sbx-' + sandbox_id + '-*')):
        if not group.is_dir(): continue
        row = {'sandbox_id': sandbox_id, 'cgroup': str(group), 'fields': {}, 'processes': []}
        for name in ('cpu.stat', 'cpu.pressure', 'memory.current', 'memory.events', 'memory.pressure', 'memory.swap.current'):
            row['fields'][name] = read(group / name)
        for token in read(group / 'cgroup.procs').get('text', '').split()[:8]:
            pid = int(token); actual = identity(pid)
            expected = '0::/e2b/' + group.name
            if actual['start_ticks'] is not None and actual['cgroup'].get('text', '').strip() == expected:
                observed = process(pid)
                owned = observed['identity_stable'] and same_identity(actual, observed)
                row['processes'].append(dict(observed, ownership='owned-sandbox-cgroup' if owned else 'unknown'))
            else: row['processes'].append(dict(actual, ownership='unknown'))
        result['vms'].append(row)
context = Path(request['context'])
try:
    import hashlib
    data = context.read_bytes()
    result['context'] = {'path': str(context), 'sha256': hashlib.sha256(data).hexdigest()}
    expected = json.loads(data)['ae-e2b-orchestrator.service']
    actual = identity(int(expected['pid']))
    if actual['start_ticks'] == expected['start_ticks'] and actual['cgroup'].get('text', '').strip() == expected['cgroup']:
        observed = process(actual['pid'])
        owned = observed['identity_stable'] and same_identity(actual, observed)
        result['daemon'] = dict(observed, ownership='owned-context-pid-start-cgroup' if owned else 'unknown')
    else: result['daemon'] = dict(actual, ownership='unknown')
except (OSError, KeyError, ValueError, TypeError) as error:
    result['daemon']['error'] = type(error).__name__
print(json.dumps(result))
'''


def _bounded_observation(code, payload, timeout):
    started = now_ms()
    with subprocess.Popen([sys.executable, '-I', '-c', code], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as child:
        try:
            stdout, stderr = child.communicate(json.dumps(payload), timeout=timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()  # Reap even on timeout, before owned sandbox cleanup.
            return {'status': 'timeout', 'pid': child.pid, 'returncode': child.returncode,
                    'wall_ms': now_ms() - started, 'budget_seconds': timeout}
        except BaseException:
            child.kill()
            child.communicate()
            raise
        result = {'status': 'observed', 'pid': child.pid, 'returncode': child.returncode,
                  'wall_ms': now_ms() - started, 'budget_seconds': timeout, 'stderr_bytes': len(stderr)}
        try:
            result['result'] = json.loads(stdout) if len(stdout) <= 262144 else {'error': 'OutputLimit'}
        except ValueError: result['result'] = {'error': 'InvalidJSON'}
        return result


def observe_e2b_failure(children, per_child, args, base_opts):
    """Bounded, read-only inspection after a failed verify; never retry the touch."""
    started = now_ms()
    by_id = {getattr(child, 'sandbox_id', None): child for child in children}
    selected = [next(row for row in per_child if not row['verify']['ok'])]
    successful = next((row for row in per_child if row['verify']['ok'] and row['sandbox_id'] in by_id), None)
    if successful is not None:
        selected.append(successful)
    result = {'scope': 'post-failure-only; not a measurement or verification retry',
              'extra_budget_seconds': 4 * len(selected) + 2, 'guests': []}
    for row in selected:
        sb = by_id.get(row['sandbox_id'])
        record = {'sandbox_id': row['sandbox_id'], 'original_verify_ok': row['verify']['ok']}
        try:
            payload = {'id': sb.sandbox_id, 'domain': sb.sandbox_domain, 'version': str(sb._envd_version),
                       'envd_token': sb._envd_access_token,
                       'traffic_token': getattr(sb, '_SandboxBase__traffic_access_token', None),
                       'options': dict(base_opts, request_timeout=2,
                           extra_sandbox_headers=dict(sb.connection_config.sandbox_headers)),
                       'command': "python3 - <<'PY'\n" + guest_observation_code() + '\nPY'}
            record['probe'] = _bounded_observation(_SDK_OBSERVATION_CODE, payload, 4)
        except Exception as error: record['error'] = type(error).__name__
        result['guests'].append(record)
    try:
        result['host'] = _bounded_observation(_HOST_OBSERVATION_CODE,
            {'ids': [row['sandbox_id'] for row in selected],
             'context': str(Path(args.out).parent / 'environment/e2b-placement/transaction-unit-identities.json')}, 2)
    except Exception as error: result['host'] = {'error': type(error).__name__}
    result['post_failure_observation_wall_ms'] = now_ms() - started
    return result


def cube_create_after_cleanup(create, *, timeout_s=60, interval_s=1):
    """Wait for Cube's asynchronous resource release before the next timed fan-out."""
    deadline = time.monotonic() + timeout_s
    attempts = 0
    while True:
        attempts += 1
        try:
            return create(), attempts
        except Exception as error:
            if '130597' not in str(error) or time.monotonic() >= deadline:
                raise
            time.sleep(min(interval_s, max(0, deadline - time.monotonic())))


def bench_cube(args: argparse.Namespace, forks: list[int]) -> list[dict[str, Any]]:
    from cubesandbox import Config, Sandbox

    api_url = args.cube_api_url or getenv_any(["CUBE_API_URL", "E2B_API_URL"], "http://127.0.0.1:3000")
    template = args.cube_template or getenv_any(["CUBE_TEMPLATE_ID", "E2B_TEMPLATE_ID"])
    proxy_node_ip = args.cube_proxy_node_ip or getenv_any(["CUBE_PROXY_NODE_IP"])
    cfg = Config(
        api_url=api_url,
        template_id=template,
        proxy_node_ip=proxy_node_ip,
        timeout=args.timeout,
        request_timeout=args.request_timeout,
    )
    rows: list[dict[str, Any]] = []

    for n in forks:
        run_id = f"cube-{int(time.time())}-{uuid.uuid4().hex[:8]}-n{n}"
        source = None
        clones: list[Any] = []
        row: dict[str, Any] = {
            "backend": "cube",
            "forks": n,
            "run_id": run_id,
            "api_url": api_url,
            "template": template,
            "mode": "official Sandbox.clone(n=N, concurrency=N)",
            "success": False,
        }
        t_total0 = now_ms()
        try:
            admitted, create_source = run_timed(
                lambda: cube_create_after_cleanup(lambda: Sandbox.create(
                    template=template,
                    timeout=args.timeout,
                    metadata={"official_fork_run": run_id, "role": "source"},
                    config=cfg,
                ))
            )
            if create_source.ok:
                source, attempts = admitted
                row['source_admission_attempts'] = attempts
                row['source_admission_note'] = 'Capacity wait before measured clone; includes asynchronous cleanup from previous case'
            row["source_create"] = asdict(create_source)
            if not create_source.ok:
                raise RuntimeError(create_source.error)

            _, prep = run_timed(
                lambda: cube_write_state(
                    source,
                    mem_mib=args.mem_mib,
                    token=run_id,
                    timeout=args.exec_timeout,
                )
            )
            row["source_prepare"] = asdict(prep)
            if not prep.ok:
                raise RuntimeError(prep.error)

            # Cube's official RL fan-out wrapper includes creating and cleaning
            # an internal snapshot. Its elapsed time is the e2e fork time for the
            # public clone API.
            clones, clone_step = run_timed(lambda: source.clone(n=n, concurrency=n))
            row["official_clone"] = asdict(clone_step)
            row["freeze_ms"] = None
            row["fork_wall_ms"] = clone_step.ms
            row["e2e_ms"] = clone_step.ms
            if not clone_step.ok:
                raise RuntimeError(clone_step.error)

            per_child: list[dict[str, Any]] = []
            t_verify0 = now_ms()

            def verify_one(idx_sb: tuple[int, Any]) -> dict[str, Any]:
                idx, sb = idx_sb
                _, step = run_timed(
                    lambda: cube_run_shell(
                        sb,
                        verify_mem_server_shell(token=run_id),
                        timeout=args.exec_timeout,
                    )
                )
                return {
                    "index": idx,
                    "sandbox_id": getattr(sb, "sandbox_id", None),
                    "verify": asdict(step),
                }

            with cf.ThreadPoolExecutor(max_workers=min(n, args.max_workers)) as pool:
                for child in pool.map(verify_one, list(enumerate(clones))):
                    per_child.append(child)
            row["verify_wall_ms"] = now_ms() - t_verify0
            row["ready_e2e_ms"] = row["e2e_ms"] + row["verify_wall_ms"]
            row["children"] = per_child
            row["verify_summary"] = summarize([c["verify"]["ms"] for c in per_child if c["verify"]["ok"]])
            row["success_count"] = sum(1 for c in per_child if c["verify"]["ok"])
            row["success"] = row["success_count"] == n
        except BaseException as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["traceback"] = traceback.format_exc()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        finally:
            for sb in clones or []:
                try:
                    sb.kill()
                except Exception:
                    pass
            if source is not None:
                try:
                    source.kill()
                except Exception:
                    pass
            row["total_wall_ms"] = now_ms() - t_total0
            rows.append(row)
            print(json.dumps(row, ensure_ascii=True), flush=True)
    return rows


def bench_e2b(args: argparse.Namespace, forks: list[int]) -> list[dict[str, Any]]:
    from e2b import Sandbox

    api_url = args.e2b_api_url or getenv_any(["E2B_API_URL"], "http://127.0.0.1:3000")
    sandbox_url = args.e2b_sandbox_url or getenv_any(["E2B_SANDBOX_URL"], "http://127.0.0.1:3002")
    api_key = args.e2b_api_key or getenv_any(["E2B_API_KEY"])
    template = args.e2b_template or getenv_any(["E2B_TEMPLATE_ID"], "base")
    base_opts: dict[str, Any] = {
        "api_url": api_url,
        "sandbox_url": sandbox_url,
    }
    if api_key:
        base_opts["api_key"] = api_key

    rows: list[dict[str, Any]] = []
    for n in forks:
        run_id = f"e2b-{int(time.time())}-{uuid.uuid4().hex[:8]}-n{n}"
        source = None
        children: list[Any] = []
        snapshot_id: str | None = None
        cleanup: list[dict[str, Any]] = []

        def clean_resource(kind, resource_id, operation, *, phase="final"):
            record = {"resource": kind, "id": resource_id, "phase": phase}
            try:
                returned = operation()
                if returned is True or returned is False or returned is None:
                    record["returned"] = returned
                if returned is False:
                    raise RuntimeError("SDK cleanup returned False")
                record["status"] = "ok"
            except Exception as exc:
                record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            cleanup.append(record)
            return record["status"] == "ok"

        row: dict[str, Any] = {
            "backend": "e2b",
            "forks": n,
            "run_id": run_id,
            "api_url": api_url,
            "sandbox_url": sandbox_url,
            "template": template,
            "mode": "official create_snapshot + parallel Sandbox.create(snapshot_id)",
            "success": False,
        }
        if temporal_trace_enabled():
            row['diagnostic_temporal_trace'] = {'enabled': True,
                'scope': 'diagnostic only; not paper acceptance',
                'trace_record_limit': 64, 'trace_record_bytes': 512}
        per_create = per_child = batches = None
        t_total0 = now_ms()
        try:
            source, create_source = run_timed(
                lambda: Sandbox.create(
                    template=template,
                    timeout=args.timeout,
                    metadata={"official_fork_run": run_id, "role": "source"},
                    **base_opts,
                )
            )
            row["source_create"] = asdict(create_source)
            if not create_source.ok:
                raise RuntimeError(create_source.error)

            _, prep = run_timed(
                lambda: e2b_write_state(
                    source,
                    mem_mib=args.mem_mib,
                    token=run_id,
                    timeout=args.exec_timeout,
                )
            )
            row["source_prepare"] = asdict(prep)
            if not prep.ok:
                raise RuntimeError(prep.error)

            # Named snapshots return namespace/name:tag; SDK 2.25.1 does not
            # escape that slash in DELETE. Keep the canonical ID instead.
            snapshot, freeze = run_timed(lambda: source.create_snapshot(**base_opts))
            row["freeze"] = asdict(freeze)
            row["freeze_ms"] = freeze.ms
            if not freeze.ok:
                raise RuntimeError(freeze.error)
            snapshot_id = getattr(snapshot, "snapshot_id", None) or getattr(snapshot, "snapshotId", None)
            row["snapshot_id"] = snapshot_id
            if not snapshot_id:
                raise RuntimeError(f"missing snapshot_id in {snapshot!r}")

            per_create: list[dict[str, Any]] = []
            t_fork0 = now_ms()

            def create_child(idx: int) -> tuple[Any, dict[str, Any]]:
                child, step = run_timed(
                    lambda: Sandbox.create(
                        template=snapshot_id,
                        timeout=args.timeout,
                        metadata={"official_fork_run": run_id, "role": "child", "index": str(idx)},
                        **base_opts,
                    )
                )
                return child, {"index": idx, "create": asdict(step)}

            per_child: list[dict[str, Any]] = []
            batches = []
            batch_size = args.e2b_batch_size or n
            for offset in range(0, n, batch_size):
                indices = list(range(offset, min(n, offset + batch_size)))
                started = now_ms()
                batch_children = []
                with cf.ThreadPoolExecutor(max_workers=min(len(indices), args.max_workers)) as pool:
                    for child, meta in pool.map(create_child, indices):
                        per_create.append(meta)
                        if child is not None:
                            children.append(child)
                            batch_children.append((meta["index"], child))
                create_end = now_ms()
                if len(batch_children) != len(indices) or any(not m["create"]["ok"] for m in per_create):
                    raise RuntimeError("child create failure in measured batch")

                def verify_one(idx_sb):
                    idx, sb = idx_sb
                    _, step = run_timed(lambda: e2b_run_shell(sb, verify_mem_server_shell(token=run_id), timeout=args.exec_timeout))
                    return {"index": idx, "sandbox_id": getattr(sb, "sandbox_id", None), "verify": asdict(step)}

                with cf.ThreadPoolExecutor(max_workers=min(len(indices), args.max_workers)) as pool:
                    verified = list(pool.map(verify_one, batch_children))
                ready = now_ms()
                per_child.extend(verified)
                batches.append({"offset": offset, "count": len(indices), "create_ms": create_end-started,
                                "verify_ms": ready-create_end, "ready_ms": ready-started,
                                "success_count": sum(c["verify"]["ok"] for c in verified)})
                if not all(c["verify"]["ok"] for c in verified):
                    raise RuntimeError("inherited-memory verification failed")
                # Free only this batch's owned children before launching the next.
                # Inter-batch cleanup is inside the end-to-end timer.
                if offset + batch_size < n:
                    for _, child in batch_children:
                        if not clean_resource("child", getattr(child, "sandbox_id", None),
                                              lambda: child.kill(**base_opts), phase="inter-batch"):
                            raise RuntimeError("inter-batch child cleanup failed")
                        children.remove(child)
            row["batch_size"] = batch_size
            row["batches"] = batches
            row["mode"] = "one snapshot + sequential measured create/verify batches; inter-batch cleanup included"
            row["fork_wall_ms"] = sum(b["create_ms"] for b in batches)
            row["verify_wall_ms"] = sum(b["verify_ms"] for b in batches)
            row["ready_e2e_ms"] = row["freeze_ms"] + now_ms() - t_fork0
            row["e2e_ms"] = row["ready_e2e_ms"]
            row["child_creates"] = per_create
            row["children"] = per_child
            row["success_count"] = sum(c["verify"]["ok"] for c in per_child)
            row["success"] = row["success_count"] == n
        except BaseException as exc:
            # Preserve completed work on failure as well as success. In particular,
            # keep each SDK/command error instead of only the aggregate exception.
            if per_create is not None:
                row["child_creates"] = per_create
                row["children"] = per_child or []
                row["batches"] = batches or []
                row["batch_size"] = args.e2b_batch_size or n
                row["success_count"] = sum(c["verify"]["ok"] for c in (per_child or []))
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["traceback"] = traceback.format_exc()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if per_child and any(not child["verify"]["ok"] for child in per_child):
                observation_started = now_ms()
                try:
                    row["post_failure_observation"] = observe_e2b_failure(children, per_child, args, base_opts)
                except Exception as observation_error:
                    row["post_failure_observation"] = {"error": type(observation_error).__name__,
                                                       "extra_budget_seconds": 10}
                finally:
                    row["post_failure_observation_wall_ms"] = now_ms() - observation_started
        finally:
            for sb in children:
                clean_resource("child", getattr(sb, "sandbox_id", None), lambda: sb.kill(**base_opts))
            if snapshot_id:
                clean_resource("snapshot", snapshot_id, lambda: Sandbox.delete_snapshot(snapshot_id, **base_opts))
            if source is not None:
                clean_resource("source", getattr(source, "sandbox_id", None), lambda: source.kill(**base_opts))
            row["cleanup"] = cleanup
            if any(record["status"] != "ok" for record in cleanup):
                row["success"] = False
            row["total_wall_ms"] = now_ms() - t_total0
            rows.append(row)
            print(json.dumps(row, ensure_ascii=True), flush=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["cube", "e2b", "both"], default="both")
    ap.add_argument("--forks", default=",".join(str(x) for x in FORK_DEFAULTS))
    ap.add_argument("--out", default="")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--request-timeout", type=float, default=180.0)
    ap.add_argument("--exec-timeout", type=float, default=120.0,
                    help="Guest command execution budget in seconds (hang protection, not a performance threshold)")
    ap.add_argument("--mem-mib", type=int, default=64)
    ap.add_argument("--max-workers", type=int, default=64)
    ap.add_argument("--e2b-batch-size", type=int, default=16, help="0 means one native N-way batch")
    ap.add_argument("--cube-api-url", default="")
    ap.add_argument("--cube-template", default="")
    ap.add_argument("--cube-proxy-node-ip", default="")
    ap.add_argument("--e2b-api-url", default="")
    ap.add_argument("--e2b-sandbox-url", default="")
    ap.add_argument("--e2b-api-key", default="")
    ap.add_argument("--e2b-template", default="")
    args = ap.parse_args()

    forks = parse_forks(args.forks)
    all_rows: list[dict[str, Any]] = []
    if args.backend in ("cube", "both"):
        all_rows.extend(bench_cube(args, forks))
    if args.backend in ("e2b", "both"):
        all_rows.extend(bench_e2b(args, forks))

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(all_rows, indent=2, ensure_ascii=True) + "\n")
    return 0 if all(r.get("success") for r in all_rows) else 1


def _ae_interrupted(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise KeyboardInterrupt(f"Experiment interrupted by signal {signum}")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _ae_interrupted)
    raise SystemExit(main())
