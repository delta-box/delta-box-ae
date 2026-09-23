"""Opt-in lifecycle tracing/fault injection; diagnostic runs are not benchmarks."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

LOG = '/tmp/dump_lifecycle.jsonl'


def state(pid):
    try:
        fields = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
        return {k: fields[k].strip() for k in ('Name', 'State', 'Pid', 'PPid', 'TracerPid', 'NSpid') if k in fields}
    except OSError:
        return {'gone': True}


def event(kind, **fields):
    record = dict(monotonic_ns=time.monotonic_ns(), pid=os.getpid(), kind=kind, **fields)
    fd = os.open(LOG, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o644)
    try:
        os.write(fd, (json.dumps(record) + '\n').encode())
    finally:
        os.close(fd)


def signals():
    original = os.kill
    def traced(pid, sig):
        frame = sys._getframe(1)
        event('signal', target=pid, signal=int(sig), target_state=state(pid),
              caller=frame.f_code.co_name, file=frame.f_code.co_filename, line=frame.f_lineno)
        return original(pid, sig)
    os.kill = traced


def install_agent():
    import template_fork
    signals()
    original = template_fork._write_response
    def reply(path, response):
        original(path, response)
        if response.get('mode') == 'stash_template':
            delay = float(os.environ.get('DELTABOX_DUMP_DIAG_HELPER_DELAY_MS', '0')) / 1000
            event('stash_ack_sent', response=response, self_state=state(os.getpid()), injected_delay_s=delay)
            if delay:
                time.sleep(delay)
            event('stash_ack_sender_continuing', response=response)
    template_fork._write_response = reply


def install_controller():
    import sandbox_controller
    import template_fork
    signals()
    original = subprocess.check_call
    def dump(command, *args, **kwargs):
        if isinstance(command, list) and len(command) > 1 and command[1] == 'dump' and '--tree' in command:
            pid = int(command[command.index('--tree') + 1])
            ckpt = Path(command[command.index('-D') + 1]).name
            directory = Path('/tmp/criu-diagnostics'); directory.mkdir(exist_ok=True)
            command = [*command, '-v4', '-o', str(directory / (ckpt + '.log'))]
            delay = float(os.environ.get('DELTABOX_DUMP_DIAG_START_DELAY_MS', '0')) / 1000
            event('criu_queued', target=pid, target_state=state(pid), checkpoint=ckpt, injected_delay_s=delay)
            if delay:
                time.sleep(delay)
            event('criu_start', target=pid, target_state=state(pid), checkpoint=ckpt, command=command)
            try:
                return original(command, *args, **kwargs)
            finally:
                event('criu_finished', target=pid, target_state=state(pid), checkpoint=ckpt)
        return original(command, *args, **kwargs)
    subprocess.check_call = dump
    original_kill = sandbox_controller.SandboxController._kill_pids_and_wait
    def kill(controller, pids, label, *args, **kwargs):
        event('kill_set', label=label, active=controller.agent_pid,
              targets={pid: state(pid) for pid in pids}, templates=controller.template_pool.templates,
              dumps={k: {'pid': v.get('async_dump_template_pid'),
                         'done': v.get('dump_future') is None or v['dump_future'].done()}
                     for k, v in controller.registry.items() if v.get('async_dump_template_pid')})
        return original_kill(controller, pids, label, *args, **kwargs)
    sandbox_controller.SandboxController._kill_pids_and_wait = kill
    original_stash = template_fork.TemplatePool.request_stash_template
    def stash(pool, source_pid, snapshot_id, *args, **kwargs):
        pid = original_stash(pool, source_pid, snapshot_id, *args, **kwargs)
        event('stash_return', target=pid, target_state=state(pid), active=source_pid,
              snapshot_id=snapshot_id)
        return pid
    template_fork.TemplatePool.request_stash_template = stash
