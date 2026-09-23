#!/usr/bin/env python3
"""One-shot template-death injection; never use these timings as performance data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import select
import signal
import sys
import time

LOG = Path('/tmp/fallback-injection.jsonl')


def record(kind, **fields):
    with LOG.open('a') as stream:
        stream.write(json.dumps({'kind': kind, 'monotonic': time.monotonic(), **fields}) + '\n')


def identity(pid):
    """Use stat starttime to reject PID reuse, and refuse zombies/dead tasks."""
    raw = Path(f'/proc/{pid}/stat').read_text()
    fields = raw[raw.rfind(')') + 2:].split()
    state, starttime = fields[0], int(fields[19])
    status = Path(f'/proc/{pid}/status').read_text()
    nspids = next((list(map(int, line.split()[1:])) for line in status.splitlines()
                  if line.startswith('NSpid:')), [])
    if state in ('Z', 'X', 'x') or not nspids or nspids[-1] == 1:
        raise RuntimeError(f'refusing non-live template or namespace init: pid={pid} state={state} nspids={nspids}')
    return {'pid': pid, 'starttime': starttime, 'state': state,
            'nspids': nspids, 'pid_namespace': os.readlink(f'/proc/{pid}/ns/pid')}


def inject(controller, target_id):
    from lifecycle import open_pidfd, send_pidfd_signal
    target = controller.registry[target_id]
    effective_id = target.get('effective_restore_id', target_id)
    pool = controller.template_pool
    pid = pool.templates[effective_id]
    protected = {1, os.getpid(), controller.agent_pid, controller.ns_init_pid}
    if not isinstance(pid, int) or pid <= 1 or pid in protected:
        raise RuntimeError(f'refusing protected or invalid template PID: {pid}')
    before = identity(pid)
    if before['pid_namespace'] != os.readlink(f'/proc/{controller.agent_pid}/ns/pid'):
        raise RuntimeError('template does not belong to the active agent PID namespace')
    fd = open_pidfd(pid)
    try:
        pinned = identity(pid)
        if (pinned['starttime'] != before['starttime'] or pool.templates.get(effective_id) != pid
                or pid in {controller.agent_pid, controller.ns_init_pid}):
            raise RuntimeError('template ownership changed while opening pidfd')
        if select.select([fd], [], [], 0)[0]:
            raise RuntimeError('template exited before injection')
        record('injection_started', target_id=target_id, effective_id=effective_id,
               template=before, active_pid=controller.agent_pid,
               ns_init_pid=controller.ns_init_pid,
               registered_templates=dict(pool.templates))
        send_pidfd_signal(fd, signal.SIGKILL)
        if not select.select([fd], [], [], 5)[0]:
            raise TimeoutError('pidfd did not report template exit within 5 seconds')
        if pool.templates.get(effective_id) != pid:
            raise RuntimeError('injection unexpectedly changed template registration')
        record('injection_completed', target_id=target_id, effective_id=effective_id,
               template_pid=pid, registration_retained=True,
               proc_still_present=Path(f'/proc/{pid}').exists())
    finally:
        os.close(fd)


def install(controller_class):
    original = controller_class.restore_action
    state = {'injections': 0, 'restore_calls': 0}

    def restore(controller, target_id, *args, **kwargs):
        state['restore_calls'] += 1
        injected = state['restore_calls'] == 1
        if injected:
            try:
                inject(controller, target_id)
                state['injections'] += 1
            except BaseException as error:
                record('injection_failed', error_type=type(error).__name__, error=str(error))
                raise
        try:
            result = original(controller, target_id, *args, **kwargs)
        except BaseException as error:
            record('restore_failed', injected=injected, target_id=target_id,
                   error_type=type(error).__name__, error=str(error))
            raise
        record('restore_returned', injected=injected, target_id=target_id,
               actual_path=result.get('path'), active_pid=controller.agent_pid,
               ns_init_pid=controller.ns_init_pid)
        return result

    controller_class.restore_action = restore
    return state


def main():
    sys.path.insert(0, '/app')
    if len(sys.argv) > 1 and sys.argv[1] == '--run-replay':
        required = {'DELTABOX_FORCE_CRIU_RESTORE': '0',
                    'DELTABOX_REPLAY_STRICT_EPOCH': '1',
                    'DELTABOX_ALLOW_COLD_RESTORE_POOL_CLEAR': '1'}
        if any(os.environ.get(key) != value for key, value in required.items()):
            raise RuntimeError('unexpected fallback diagnostic environment')
        record('guest_identity', python_version=sys.version,
               native_pidfd_open=callable(getattr(os, 'pidfd_open', None)),
               native_pidfd_send_signal=callable(getattr(signal, 'pidfd_send_signal', None)),
               required_environment=required)
        import trace_replay_main
        state = install(trace_replay_main.SandboxController)
        sys.argv = ['/app/trace_replay_main.py', *sys.argv[2:]]
        try:
            trace_replay_main.run_replay(trace_replay_main.parse_args())
        finally:
            record('probe_finished', **state)
        if state['injections'] != 1:
            raise RuntimeError('diagnostic did not inject exactly once')
        return

    LOG.unlink(missing_ok=True)
    # Run the untouched standard entry (hash/environment/disk checks included).
    # Intercept only its final exec, before any runtime/controller exists.
    original_exec = os.execv

    def enter_replay(executable, arguments):
        os.execv = original_exec
        if arguments[:3] != [sys.executable, '-u', '/app/trace_replay_main.py']:
            raise RuntimeError(f'unexpected standard entry exec: {arguments!r}')
        original_exec(executable, [sys.executable, '-u', str(Path(__file__).resolve()),
                                   '--run-replay', *arguments[3:]])

    os.execv = enter_replay
    runpy.run_path('/app/entry.py', run_name='__main__')


if __name__ == '__main__':
    main()
