import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import signal
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae/vendor/spr_payload'))
sys.path.insert(0, str(ROOT / 'ae'))


def load(name, relative, env=None):
    with patch.dict(os.environ, env or {}):
        spec = importlib.util.spec_from_file_location(name, ROOT / relative)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


PILOT = load('e2b_local_pilot', 'ae/vendor/finalbench/e2b_finalbench/e2b_slim_finalbench_pilot.py',
             {'PYTHONHASHSEED': '0', 'SPR_PAYLOAD': str(ROOT / 'ae/vendor/spr_payload'), 'E2B_EXECUTION': 'local'})
ENV = load('e2b_environment_test', 'ae/runners/e2b_environment.py')


class E2BLocalExecutionTests(unittest.TestCase):
    def test_local_shell_executes_and_preserves_exit_code_without_ssh(self):
        result = PILOT.l1_remote("printf local-transport; exit 7", check=False)
        self.assertEqual((result.returncode, result.stdout), (7, 'local-transport'))
        with self.assertRaises(subprocess.CalledProcessError):
            PILOT.l1_remote('exit 9')

    def test_ssh_mode_still_uses_configured_port(self):
        with patch.object(PILOT, 'EXECUTION', 'ssh'), patch.object(PILOT, 'ssh_cmd') as ssh, \
                patch.dict(os.environ, {'E2B_L1_SSH_PORT': '56556'}):
            PILOT.l1_remote('true', timeout=4, check=False)
        ssh.assert_called_once_with(56556, 'true', timeout=4, check=False)

    def test_local_timeout_kills_sleep_descendant_not_only_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / 'child.pid'
            with self.assertRaises(subprocess.TimeoutExpired):
                PILOT.l1_remote(f'sleep 60 & echo $! > {PILOT.q(str(pidfile))}; wait', timeout=.3)
            pid = int(pidfile.read_text())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                state = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='],
                                       text=True, capture_output=True).stdout.strip()
                if not state or state.startswith('Z'):
                    break
                time.sleep(.02)
            else:
                self.fail(f'local E2B descendant survived timeout: {pid} {state}')

    def test_warm_worker_launch_closes_controller_output_pipe_after_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / 'ready'
            child_pid = Path(tmp) / 'worker.pid'
            worker = ('bash -c ' + PILOT.q(f'echo $$ > {PILOT.q(str(child_pid))}; '
                      f'touch {PILOT.q(str(ready))}; exec sleep 60') + ' >/dev/null 2>&1 </dev/null')
            command = PILOT.launch_background_worker('sleep .1', worker,
                       f'for i in 1 2 3 4 5; do test -f {PILOT.q(str(ready))} && exit 0; sleep .05; done; exit 1')
            try:
                result = PILOT.l1_remote(command, timeout=2)
                self.assertEqual(result.returncode, 0)
                self.assertTrue(ready.exists())
            finally:
                if child_pid.exists():
                    try:
                        os.kill(int(child_pid.read_text()), 9)
                    except ProcessLookupError:
                        pass

    def test_supervisor_sigterm_cancels_owned_group_and_restores_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        timer = threading.Timer(.2, lambda: os.kill(os.getpid(), signal.SIGTERM))
        timer.start()
        try:
            with self.assertRaisesRegex(KeyboardInterrupt, 'terminated'):
                PILOT.l1_remote('sleep 60', timeout=10)
        finally:
            timer.cancel()
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_slim_namespace_keeps_installed_core_importable(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = Path(tmp) / 'llama_index/core'
            core.mkdir(parents=True)
            (core / '__init__.py').write_text('def get_tokenizer(): return "installed-core"\n')
            env = dict(os.environ, PYTHONPATH=os.pathsep.join([
                str(ROOT / 'ae/vendor/finalbench/deltabox_std/slim_shims'), tmp]))
            result = subprocess.run([sys.executable, '-c',
                'from llama_index.core import get_tokenizer; print(get_tokenizer())'],
                capture_output=True, text=True, env=env, check=True)
            self.assertEqual(result.stdout.strip(), 'installed-core')

    def step(self, code, timing, stale=False):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'timings.json'
            if stale:
                output.write_text(json.dumps({'ok': True, 'resume_ms': 1}))
            def invoke(*args, **kwargs):
                if timing is not None:
                    output.write_text(json.dumps(timing))
                return subprocess.CompletedProcess([], code, 'stdout', 'stderr')
            with patch.object(PILOT, 'l1_remote', side_effect=invoke), \
                    patch.dict(os.environ, {'E2B_REMOTE_PATH': '/usr/bin'}):
                return PILOT.e2b_step(from_build='base', to_build='child', storage=tmp,
                                      command='true', timings_path=output, uploads=[], downloads=[])

    def test_success_json_cannot_hide_failed_process_or_overwrite_exit_evidence(self):
        result = self.step(7, {'ok': True, 'host_rc': 0, 'stdout_tail': 'forged'})
        self.assertFalse(result['ok'])
        self.assertEqual(result['host_rc'], 7)
        self.assertEqual(result['stdout_tail'], 'stdout')

    def test_zero_exit_requires_successful_timing_evidence(self):
        for timing in (None, {}, {'ok': False}):
            with self.subTest(timing=timing):
                self.assertFalse(self.step(0, timing)['ok'])
        self.assertTrue(self.step(0, {'ok': True, 'resume_ms': 12})['ok'])

    def test_old_success_timing_is_removed_before_new_process(self):
        result = self.step(0, None, stale=True)
        self.assertFalse(result['ok'])
        self.assertFalse(result['timing_present'])

    def test_local_execution_does_not_guess_sidecar_or_silently_fallback(self):
        cfg = {'e2b': {'execution': 'local', 'infra': '/tmp/infra', 'remote_path': '/usr/bin'}}
        with self.assertRaisesRegex(ValueError, 'sidecar_ip'):
            ENV.configure(cfg, {})
        cfg['e2b']['execution'] = 'anything'
        with self.assertRaisesRegex(ValueError, 'execution'):
            ENV.configure(cfg, {})

    def test_resume_binary_and_baked_snapshot_path_are_shell_quoted(self):
        env = {'E2B_REMOTE_PATH': '/usr/bin', 'E2B_RESUME_BINARY': '/tmp/a binary',
               'E2B_SANDBOX_DIR': '/tmp/sandbox path'}
        with patch.dict(os.environ, env):
            command = PILOT.e2b_resume_build_cmd(from_build='base', to_build='child', storage='/tmp/store',
                        command='printf ok', finalbench_json=Path('/tmp/timing'), uploads=[], downloads=[])
        self.assertIn("'/tmp/a binary'", command)
        self.assertIn("-sandbox-dir '/tmp/sandbox path'", command)
        self.assertNotIn('go run', command)


if __name__ == '__main__':
    unittest.main()
