#!/usr/bin/env python3
"""Non-performance, owned-process CRIU lifecycle and thread-count diagnostic.

Run on Linux as root. All images, commands, responses and logs stay in --out.
The scientific profile imports the baseline's NumPy/Faiss stack with its exact
worker_env settings; thread counts differ only by explicit Python workers.
This is a controlled diagnostic, not the full Moatless application workload.
"""
import argparse
import ast
import ctypes
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, sort_keys=True) + '\n')
    temporary.replace(path)


def child(args):
    import queue
    import threading
    versions = {}
    if args.profile == 'scientific':
        import numpy as np
        import scipy
        import faiss
        versions = {'numpy': np.__version__, 'scipy': scipy.__version__,
                    'faiss': faiss.__version__}
        vectors = np.arange(4096, dtype='float32').reshape(256, 16)
        index = faiss.IndexFlatL2(16)
        index.add(vectors)
    memory = bytearray(b'\x5a') * (args.mib * 1024 * 1024)
    memory_hash = hashlib.sha256(memory).hexdigest()
    inboxes, replies = [], queue.Queue()
    def thread_main(number, inbox):
        total = 0
        while True:
            delta = inbox.get()
            total += delta
            replies.put((number, total))
    for number in range(args.threads - 1):
        inbox = queue.Queue()
        threading.Thread(target=thread_main, args=(number, inbox), daemon=True).start()
        inboxes.append(inbox)
    counter, last_request = 0, 0
    atomic_json(args.out / 'ready.json', {'pid': os.getpid(), 'versions': versions,
                'memory_hash': memory_hash, 'threads': len(os.listdir('/proc/self/task'))})
    while True:
        command_path = args.out / 'command.json'
        if not command_path.exists():
            time.sleep(.01)
            continue
        command = json.loads(command_path.read_text())
        if command['id'] <= last_request:
            time.sleep(.01)
            continue
        actual_hash = hashlib.sha256(memory).hexdigest()
        assert actual_hash == memory_hash, 'restored heap differs'
        counter += command['delta']
        memory[0] = counter % 256
        memory_hash = hashlib.sha256(memory).hexdigest()
        thread_totals = []
        for inbox in inboxes:
            inbox.put(command['delta'])
        for _ in inboxes:
            thread_totals.append(replies.get(timeout=10))
        search = None
        if args.profile == 'scientific':
            distances, ids = index.search(vectors[[37]], 1)
            assert ids.tolist() == [[37]] and distances.tolist() == [[0.0]]
            search = {'ids': ids.tolist(), 'distances': distances.tolist()}
        last_request = command['id']
        atomic_json(args.out / ('response-%d.json' % last_request), {
            'id': last_request, 'pid': os.getpid(), 'counter': counter,
            'threads': len(os.listdir('/proc/self/task')), 'thread_totals': sorted(thread_totals),
            'memory_before_sha256': actual_hash, 'memory_after_sha256': memory_hash,
            'faiss_search': search,
        })


def baseline_environment(driver):
    # Extract only the actual worker_env function, avoiding driver import side
    # effects. Its source hash and complete resulting environment overrides are
    # retained in the diagnostic result.
    source = driver.read_text()
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef) and n.name == 'worker_env')
    function_source = ast.get_source_segment(source, node)
    scope = {'os': os, 'Path': Path, 'BASE': driver.parent,
             'PAYLOAD': driver.parent.parent / 'spr_payload',
             'INDEX_STORE': Path('/unused'), 'TRACES_ROOT': Path('/unused')}
    exec(compile(function_source, str(driver), 'exec'), scope)
    env = scope['worker_env'](Path('/unused'), Path('/unused'), 1)
    return env, {'driver': str(driver), 'driver_sha256': digest(driver),
                 'function_sha256': hashlib.sha256(function_source.encode()).hexdigest(),
                 'overrides': {k: v for k, v in env.items() if os.environ.get(k) != v}}


def parent(args):
    os.sched_setaffinity(0, {args.cpu})
    # Adopt restored descendants so they can be reaped before restoring the
    # same checkpoint PID again, without relying on the host's init process.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), 'PR_SET_CHILD_SUBREAPER')
    args.out.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    env_record = None
    if args.profile == 'scientific':
        if not args.baseline_driver:
            raise ValueError('--baseline-driver is required for scientific profile')
        env, env_record = baseline_environment(args.baseline_driver)
    result = {'purpose': 'non-performance controlled restore/execute/recheckpoint diagnostic',
              'criu': str(args.criu), 'criu_sha256': digest(args.criu),
              'criu_version': subprocess.check_output([str(args.criu), '--version'], text=True).strip(),
              'probe_sha256': digest(Path(__file__)), 'kernel': os.uname().release,
              'affinity': sorted(os.sched_getaffinity(0)), 'threads_requested': args.threads,
              'mib': args.mib, 'profile': args.profile, 'baseline_environment': env_record,
              'steps': [], 'passed': False, 'started_unix': time.time()}
    process = None
    owned_pid = None
    owned_start = None
    log = (args.out / 'worker.log').open('w')

    def identity(pid):
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rpartition(')')[2].split()
        return int(fields[19])

    def cleanup():
        nonlocal owned_pid, owned_start
        if owned_pid is not None:
            try:
                if identity(owned_pid) != owned_start:
                    raise RuntimeError('refusing cleanup of a reused PID')
                os.kill(owned_pid, signal.SIGKILL)
                os.waitpid(owned_pid, 0)
            except (ProcessLookupError, FileNotFoundError):
                pass
            owned_pid = None
            owned_start = None

    def wait_json(path, timeout=30):
        deadline = time.monotonic() + timeout
        while not path.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError(str(path))
            time.sleep(.01)
        return json.loads(path.read_text())

    def invoke(command):
        step = {'command': command, 'started_unix': time.time()}
        result['steps'].append(step)
        try:
            p = subprocess.run(command, capture_output=True, text=True, timeout=45)
            step.update(rc=p.returncode, stdout=p.stdout, stderr=p.stderr)
            if p.returncode:
                raise RuntimeError('CRIU command failed: ' + ' '.join(command))
        finally:
            step['finished_unix'] = time.time()

    def execute(number, delta, expected_counter, previous_hash):
        atomic_json(args.out / 'command.json', {'id': number, 'delta': delta})
        response = wait_json(args.out / ('response-%d.json' % number))
        assert response['counter'] == expected_counter, response
        assert response['memory_before_sha256'] == previous_hash, response
        assert response['threads'] == args.threads, response
        assert response['thread_totals'] == [[n, expected_counter] for n in range(args.threads - 1)], response
        result['steps'].append({'application_execution_verified': response})
        return response['memory_after_sha256']

    flags = ['--shell-job', '--tcp-close', '--file-locks', '--ext-unix-sk']
    try:
        command = [str(args.python), str(Path(__file__).resolve()), '--child',
                   '--out', str(args.out), '--threads', str(args.threads),
                   '--mib', str(args.mib), '--profile', args.profile]
        process = subprocess.Popen(command, env=env, cwd=args.out, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=log, start_new_session=True)
        owned_pid = process.pid
        owned_start = identity(owned_pid)
        ready = wait_json(args.out / 'ready.json')
        result['ready'] = ready
        for name in ('status', 'maps'):
            (args.out / ('worker.' + name)).write_text((Path('/proc') / str(owned_pid) / name).read_text())
        previous_hash = execute(1, 11, 11, ready['memory_hash'])
        for iteration, delta, expected in ((0, 7, 18), (1, 13, 31)):
            images = args.out / ('images-%d' % iteration)
            images.mkdir()
            invoke([str(args.criu), 'dump', '--leave-running', '-t', str(owned_pid),
                    '-D', str(images), *flags, '--track-mem', '--log-file', 'dump-v4.log', '-v4'])
            cleanup()
            if iteration == 0:
                process.wait(timeout=5)
            pidfile = args.out / ('restored-%d.pid' % iteration)
            restore_start = int(float(Path('/proc/uptime').read_text().split()[0]) * os.sysconf('SC_CLK_TCK'))
            try:
                invoke([str(args.criu), 'restore', '-d', '-D', str(images), *flags,
                        '--pidfile', str(pidfile), '--log-file', 'restore-v4.log', '--log-pid', '-v4'])
            finally:
                if pidfile.exists():
                    restored = int(pidfile.read_text())
                    restored_start = identity(restored)
                    if restored != ready['pid'] or restored_start < restore_start:
                        raise RuntimeError('restored PID ownership check failed')
                    owned_pid, owned_start = restored, restored_start
            previous_hash = execute(iteration + 2, delta, expected, previous_hash)
        result['passed'] = True
    except Exception as error:
        result['error'] = repr(error)
    finally:
        try:
            cleanup()
        except Exception as error:
            result['cleanup_error'] = repr(error)
            result['passed'] = False
        log.close()
        result['finished_unix'] = time.time()
        atomic_json(args.out / 'probe.json', result)
    print(json.dumps({'out': str(args.out), 'passed': result['passed'], 'error': result.get('error')}))
    return 0 if result['passed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--criu', type=Path, default=Path('/usr/sbin/criu'))
    parser.add_argument('--python', type=Path, default=Path('/usr/bin/python3.11'))
    parser.add_argument('--baseline-driver', type=Path)
    parser.add_argument('--cpu', type=int, default=0)
    parser.add_argument('--threads', type=int, default=1)
    parser.add_argument('--mib', type=int, default=1)
    parser.add_argument('--profile', choices=('minimal', 'scientific'), default='minimal')
    parser.add_argument('--child', action='store_true')
    args = parser.parse_args()
    if args.threads < 1 or args.mib < 1:
        parser.error('positive threads and mib required')
    return child(args) if args.child else parent(args)


if __name__ == '__main__':
    sys.exit(main())
