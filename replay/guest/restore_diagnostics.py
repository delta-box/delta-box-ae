"""Opt-in restore phase clocks and guest scheduler trace; never a benchmark."""
import atexit
import json
import os
from pathlib import Path
import subprocess
import time


def install_agent():
    import template_fork as tf
    context = {}
    handler_name = ('_handle_template_message' if hasattr(tf, '_handle_template_message')
                    else 'handle_template_message')
    original_handle = getattr(tf, handler_name)
    original_read = tf._read_ctrl_line
    original_fork = os.fork
    original_reply = tf._write_response

    def handle(*args, **kwargs):
        context.clear()
        context['handle_ns'] = time.perf_counter_ns()
        return original_handle(*args, **kwargs)

    def read(fd):
        line = original_read(fd)
        context['read_ns'] = time.perf_counter_ns()
        if line:
            try:
                context['dispatch_ns'] = json.loads(line).get('_dispatch_mono_ns')
            except ValueError:
                pass
        return line

    def fork():
        before = time.perf_counter_ns()
        pid = original_fork()
        context['fork_enter_ns'] = before
        context['fork_return_ns'] = time.perf_counter_ns()
        return pid

    def reply(path, response):
        if response.get('mode') == 'fork':
            now = time.perf_counter_ns()
            timings = response.setdefault('timing_ms', {})
            for name, start, end in (
                ('diag_dispatch_to_handle', 'dispatch_ns', 'handle_ns'),
                ('diag_handle_to_read', 'handle_ns', 'read_ns'),
                ('diag_read_to_fork', 'read_ns', 'fork_enter_ns'),
                ('diag_fork', 'fork_enter_ns', 'fork_return_ns'),
            ):
                if context.get(start) and context.get(end):
                    timings[name] = (context[end] - context[start]) / 1e6
            timings['diag_fork_to_reply'] = (now - context['fork_return_ns']) / 1e6
            response['diagnostic_clocks'] = dict(context, reply_ns=now)
        return original_reply(path, response)

    setattr(tf, handler_name, handle)
    tf._read_ctrl_line = read
    os.fork = fork
    tf._write_response = reply


def install_controller():
    import sandbox_controller as sc
    trace = Path('/sys/kernel/tracing')
    information = {}
    marker = None
    try:
        trace.mkdir(exist_ok=True)
        if not (trace / 'tracing_on').exists():
            subprocess.run(['mount', '-t', 'tracefs', 'tracefs', str(trace)], check=True)
        (trace / 'tracing_on').write_text('0')
        (trace / 'current_tracer').write_text('nop')
        (trace / 'trace_clock').write_text('mono')
        (trace / 'buffer_size_kb').write_text('8192')
        (trace / 'trace').write_text('')
        enabled = []
        for event in ('sched/sched_switch', 'sched/sched_wakeup',
                      'sched/sched_process_fork', 'sched/sched_process_exit',
                      'signal/signal_generate', 'signal/signal_deliver'):
            path = trace / 'events' / event / 'enable'
            if path.exists():
                path.write_text('1')
                enabled.append(event)
        information['events'] = enabled
        marker = os.open(trace / 'trace_marker', os.O_WRONLY)
    except (OSError, subprocess.SubprocessError) as error:
        information['error'] = str(error)
    Path('/tmp/restore-diagnostics.json').write_text(json.dumps(information))

    def error(operation, exception):
        information.setdefault('errors', []).append(dict(operation=operation, error=str(exception)))

    def mark(text):
        if marker is not None:
            try:
                os.write(marker, (text+'\n').encode())
            except OSError as exception:
                error('marker', exception)

    def tracing(enabled):
        if marker is not None:
            try:
                (trace / 'tracing_on').write_text('1' if enabled else '0')
            except OSError as exception:
                error('tracing_on', exception)

    original_restore = sc.SandboxController.restore_action
    original_kill = sc.SandboxController._kill_pids_and_wait

    def restore(controller, target):
        template = controller.template_pool.templates.get(target) if controller.template_pool else None
        tracing(True)
        mark(f'restore_begin active={controller.agent_pid} template={template} target={target}')
        try:
            return original_restore(controller, target)
        finally:
            mark(f'restore_end active={controller.agent_pid}')
            tracing(False)

    def kill(controller, pids, label, *args, **kwargs):
        mark(f'kill_begin pids={pids}')
        try:
            return original_kill(controller, pids, label, *args, **kwargs)
        finally:
            mark(f'kill_end pids={pids}')

    def finish():
        if marker is not None:
            try:
                tracing(False)
                Path('/tmp/restore-kernel-trace.txt').write_bytes((trace / 'trace').read_bytes())
            except OSError as exception:
                error('export', exception)
            finally:
                os.close(marker)
        Path('/tmp/restore-diagnostics.json').write_text(json.dumps(information))

    sc.SandboxController.restore_action = restore
    sc.SandboxController._kill_pids_and_wait = kill
    atexit.register(finish)
