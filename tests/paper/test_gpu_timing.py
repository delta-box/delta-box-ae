"""GPU-free tests of recipe planning, admission and measured-result integrity."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae'))
from repro import gpu_protocol as protocol

spec = importlib.util.spec_from_file_location('gpu_timing_entry', ROOT / 'ae/runners/gpu_timing.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
spec = importlib.util.spec_from_file_location('gpu_worker_entry', ROOT / 'ae/runners/gpu_worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def measurement(case, config_sha='sha', devices=None):
    devices = devices or ['GPU-fixture']
    return dict(schema_version=1, kind='gpu-timing-result', status='ok', gpu_verified=True,
                **case, config_sha256=config_sha,
                samples=[dict(rep=i, total_s=.25) for i in range(case['reps'])],
                hardware=[dict(device=i, uuid=uuid, name='fixture', total_memory_bytes=80*1024**3,
                               capability=[9, 0]) for i, uuid in enumerate(devices)],
                software=dict(ok=True, python='fixture', python_executable=sys.executable,
                              packages=dict(torch='2.4.0', vllm='0.8.5', transformers='4.51.0',
                                            peft='0.14.0', accelerate='1.0.0')),
                cuda_version='12.4', torch_version='2.4.0')


class ProtocolTests(unittest.TestCase):
    def test_worker_does_not_shadow_stdlib_profile(self):
        # Reproduce the path Python adds when gpu_worker.py is run directly.
        script = (
            "import runpy, sys; "
            "sys.path.insert(0, sys.argv[1]); "
            "runpy.run_path(sys.argv[2], run_name='gpu_import_probe'); "
            "import cProfile; cProfile.Profile()"
        )
        result = subprocess.run(
            [sys.executable, '-c', script, str(ROOT / 'ae/runners'),
             str(ROOT / 'ae/runners/gpu_worker.py')],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def config(self, **kw):
        with patch.dict(os.environ, {'CUDA_VISIBLE_DEVICES': ''}):
            return protocol.load_config(**kw)

    def test_paper_matrix_preserves_single_gpu_and_fsdp_cases(self):
        config = self.config()
        self.assertIs(config['generation']['enable_prefix_caching'], True)
        cases = protocol.cases(config)
        self.assertEqual(len(cases), 8)
        self.assertEqual([c['num_gpus'] for c in cases], [1, 1, 1, 1, 1, 1, 4, 4])
        self.assertEqual([c['grad_accum'] for c in cases[-2:]], [1, 4])
        self.assertEqual([c['per_gpu_batch'] for c in cases[-2:]], [4, 4])
        self.assertEqual([c['reps'] for c in cases], [3] * 4 + [5] * 4)
        self.assertEqual(runner.plan(config)['required_simultaneous_gpus'], 4)

    def test_plan_command_succeeds_without_gpu_libraries_or_model(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'plan.json'
            result = subprocess.run([sys.executable, str(ROOT / 'ae/runners/gpu_timing.py'),
                                     'plan', '--output', str(output)], cwd=ROOT,
                                    text=True, capture_output=True, check=True)
            plan = json.loads(result.stdout)
            self.assertEqual(plan['status'], 'planned')
            self.assertFalse(plan['gpu_executed'])
            self.assertEqual(plan['case_count'], 8)
            self.assertIsNone(plan['protocol']['model_path'])
            self.assertEqual(json.loads(output.read_text()), plan)
            again = subprocess.run([sys.executable, str(ROOT / 'ae/runners/gpu_timing.py'),
                                    'plan', '--output', str(output)], cwd=ROOT, capture_output=True)
            self.assertNotEqual(again.returncode, 0)

    def test_quick_check_needs_only_one_gpu_and_is_not_full_matrix(self):
        config = self.config(quick_check=True)
        plan = runner.plan(config)
        self.assertEqual(plan['required_simultaneous_gpus'], 1)
        self.assertEqual([(c['batch'], c['reps']) for c in plan['cases']], [(1, 1), (1, 1)])

    def test_invalid_configuration_is_rejected_before_gpu_use(self):
        base = self.config()
        for name, mutate in [
            ('batch', lambda c: c.update(batches=[16, 16])),
            ('bool batch', lambda c: c.update(batches=[True])),
            ('float batch', lambda c: c.update(batches=[1.0])),
            ('oversized seed', lambda c: c.update(seed=2**32)),
            ('negative reps', lambda c: c['training'].update(reps=-1)),
            ('invalid accumulation', lambda c: c['training'].update(per_gpu_batch=3)),
            ('nan timeout', lambda c: c.update(timeout_s=float('nan'))),
            ('fixed-token overflow', lambda c: c['generation'].update(prompt_mode='fixed-tokens', max_model_len=500)),
        ]:
            with self.subTest(name=name):
                config = copy.deepcopy(base)
                mutate(config)
                with self.assertRaises(ValueError):
                    protocol.validate_config(config)
        for value in ('0,0', '0,', 'MIG-unsupported', '0;command'):
            with self.assertRaises(ValueError):
                protocol.parse_devices(value)

    def test_venv_python_symlink_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            python = root / 'venv/bin/python'
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            config = self.config(training_python=str(python))
            self.assertEqual(config['training_python'], str(python))

    def test_children_get_only_selected_devices_and_a_clean_torchrun_context(self):
        config = self.config(devices=['2', '3', '6', '7'])
        with patch.dict(os.environ, {'RANK': '9', 'WORLD_SIZE': '12', 'MASTER_PORT': '10001'}):
            env = protocol.child_environment(config, protocol.cases(config)[0])
            self.assertEqual(env['CUDA_VISIBLE_DEVICES'], '2')
            self.assertEqual(env['CUDA_DEVICE_ORDER'], 'PCI_BUS_ID')
            self.assertEqual(env['PATH'].split(os.pathsep)[0],
                             str(Path(config['generation_python']).parent))
            self.assertNotIn('RANK', env)
            self.assertEqual(os.environ['RANK'], '9')
        case = protocol.cases(config)[-1]
        command = protocol.command(config, case, '/config.json', '/result.json', '/worker.py')
        self.assertIn('torch.distributed.run', command)
        self.assertIn('--nproc-per-node=4', command)
        self.assertEqual(protocol.child_environment(config, case)['CUDA_VISIBLE_DEVICES'], '2,3,6,7')

    def test_generation_applies_and_records_prefix_cache_policy(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                config = self.config(devices=['0'], quick_check=True)
                config['generation']['enable_prefix_caching'] = enabled
                case = protocol.cases(config)[0]
                output = SimpleNamespace(prompt_token_ids=[1], outputs=[
                    SimpleNamespace(token_ids=[2], finish_reason='length')])
                engine = Mock()
                engine.generate.return_value = [output]
                llm = Mock(return_value=engine)
                sampler = SimpleNamespace(samples=[], errors=[])
                with patch.dict(sys.modules, {'vllm': SimpleNamespace(LLM=llm, SamplingParams=Mock())}),\
                     patch.object(worker, 'generation_inputs', return_value=(['prompt'], [1], [1])),\
                     patch.object(worker, 'UtilSampler', side_effect=lambda _: contextlib.nullcontext(sampler)),\
                     patch.object(worker, 'hardware', return_value={}):
                    result = worker.generation(config, case, Mock())
                self.assertIs(llm.call_args.kwargs['enable_prefix_caching'], enabled)
                self.assertIs(result['generation_protocol']['prefix_caching'], enabled)
        config['generation']['enable_prefix_caching'] = 'false'
        with self.assertRaisesRegex(ValueError, 'must be boolean'):
            protocol.validate_config(config)

    def test_cuda_mapping_is_verified_without_model_or_context_allocation(self):
        import ctypes
        class Function:
            def __init__(self, call): self.call = call
            def __call__(self, *args): return self.call(*args)
        physical = [uuid.UUID(int=7), uuid.UUID(int=9)]
        def assign(pointer, value):
            pointer._obj.value = value
            return 0
        def identity(pointer, device):
            ctypes.memmove(pointer, physical[device.value].bytes, 16)
            return 0
        class Driver:
            cuInit = Function(lambda flags: 0)
            cuDeviceGetCount = Function(lambda pointer: assign(pointer, 2))
            cuDeviceGet = Function(lambda pointer, ordinal: assign(pointer, ordinal))
            cuDeviceGetUuid = Function(identity)
        expected = ['GPU-' + str(item) for item in physical]
        with patch.dict(os.environ, {'CUDA_VISIBLE_DEVICES': '6,8', 'CUDA_DEVICE_ORDER': 'PCI_BUS_ID'}),\
                patch('ctypes.CDLL', return_value=Driver()):
            selection = worker.verify_visible_cuda_devices(expected)
            self.assertEqual(selection['verified_device_uuids'], expected)
            self.assertEqual(selection['cuda_visible_devices'], '6,8')
            with self.assertRaisesRegex(RuntimeError, 'UUID'):
                worker.verify_visible_cuda_devices(expected[::-1])
        with patch.dict(os.environ, {'CUDA_VISIBLE_DEVICES': expected[0]}), patch('ctypes.CDLL') as load:
            with self.assertRaisesRegex(ValueError, 'numeric'):
                worker.verify_visible_cuda_devices(expected[:1])
            load.assert_not_called()

    def test_invalid_measurement_never_becomes_an_average(self):
        case = protocol.cases(self.config(quick_check=True))[0]
        raw = measurement(case)
        self.assertAlmostEqual(protocol.validate_result(raw, case, 'sha', ['GPU-fixture'])['mean'], .25)
        for update in ({'gpu_verified': False}, {'batch': 64}, {'config_sha256': 'other'},
                       {'samples': []}, {'samples': [{'total_s': 0}]}, {'samples': [{'total_s': float('nan')}]},
                       {'timing_s': {'mean': 20}}, {'timing_s': []}, {'status': 'failed'}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                protocol.validate_result(dict(raw, **update), case, 'sha', ['GPU-fixture'])

    def test_wrong_devices_parameters_and_repeated_sample_ids_are_rejected(self):
        case = protocol.cases(self.config())[-1]
        devices = [f'GPU-fixture-{i}' for i in range(4)]
        raw = measurement(case, devices=devices)
        self.assertEqual(protocol.validate_result(raw, case, 'sha', devices)['n'], 5)
        for field, value in [('grad_accum', 1), ('per_gpu_batch', 1), ('warmup_reps', 0),
                             ('reps', 1), ('hardware', []), ('software', {}), ('cuda_version', None),
                             ('samples', [dict(rep=0, total_s=1.)] * 5)]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                protocol.validate_result(dict(raw, **{field: value}), case, 'sha', devices)
        wrong = copy.deepcopy(raw)
        wrong['hardware'][0]['uuid'] = 'GPU-not-selected'
        with self.assertRaisesRegex(ValueError, 'GPU.*identity'):
            protocol.validate_result(wrong, case, 'sha', devices)

    def test_historical_context_limit_and_fixed_token_protocol_are_distinct(self):
        class Tokenizer:
            def encode(self, text):
                return [3] * 524
        settings = self.config()['generation']
        prompts, lengths, caps = worker.generation_inputs(settings, 4, Tokenizer())
        self.assertIsInstance(prompts[0], str)
        self.assertEqual(lengths, [524] * 4)
        self.assertEqual(caps, [308] * 4)
        settings['prompt_mode'] = 'fixed-tokens'
        prompts, lengths, caps = worker.generation_inputs(settings, 4, Tokenizer())
        self.assertEqual(len(prompts[0]['prompt_token_ids']), 256)
        self.assertEqual(lengths, [256] * 4)
        self.assertEqual(caps, [512] * 4)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux PR_SET_PDEATHSIG contract')
    def test_detached_worker_dies_when_its_launcher_exits(self):
        with tempfile.TemporaryDirectory() as folder:
            ready = Path(folder) / 'ready.pid'
            child_code = (
                'import os,sys,time; from pathlib import Path; '
                f'sys.path.insert(0, {str(ROOT / "ae/runners")!r}); '
                'from gpu_worker import die_with_parent; die_with_parent(); '
                f'Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(30)'
            )
            launcher_code = (
                'import subprocess,sys,time; from pathlib import Path\n'
                f'ready=Path({str(ready)!r})\n'
                f'child=subprocess.Popen([sys.executable,"-c",{child_code!r},"deltabox-pdeath-fixture"], '
                'start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
                'deadline=time.monotonic()+5\n'
                'while not ready.exists() and child.poll() is None and time.monotonic()<deadline: time.sleep(.01)\n'
                'if not ready.exists():\n'
                ' child.kill(); child.wait(); raise SystemExit("worker startup failed")\n'
            )
            subprocess.run([sys.executable, '-c', launcher_code], check=True, timeout=10)
            pid = int(ready.read_text())
            def running():
                try:
                    return Path(f'/proc/{pid}/stat').read_text().split()[2] != 'Z'
                except FileNotFoundError:
                    return False
            try:
                deadline = time.monotonic() + 5
                while running() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(running(), 'detached worker survived its launcher')
            finally:
                if running() and b'deltabox-pdeath-fixture' in Path(f'/proc/{pid}/cmdline').read_bytes():
                    os.kill(pid, signal.SIGKILL)


class SuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        model = self.root / 'model'
        model.mkdir()
        (model / 'config.json').write_text('{"model_type":"qwen2"}')
        (model / 'model.safetensors').write_bytes(b'fixture weights, never loaded')
        with patch.dict(os.environ, {'CUDA_VISIBLE_DEVICES': ''}):
            self.config = protocol.load_config(model_path=str(model), devices=['0'], quick_check=True)
        self.ready = dict(ok=True, selected_gpus=[{'uuid': 'GPU-fixture', 'index': '0'}], initial_gpu_processes=[])

    def executor(self, argv, output, **kwargs):
        output.mkdir(parents=True)
        case_id = argv[argv.index('--case') + 1]
        case = protocol.case_by_id(self.config, case_id)
        path = Path(argv[argv.index('--output') + 1])
        config_path = Path(argv[argv.index('--config') + 1])
        raw = measurement(case, runner.digest(config_path), ['GPU-fixture'])
        raw.update(worker_source_sha256=runner.digest(runner.WORKER),
                   protocol_source_sha256=runner.digest(Path(protocol.__file__)))
        protocol.publish_json(path, raw)
        return dict(status='ok', returncode=0)

    def run_suite(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return runner.run_suite(self.config, self.root / 'out', preflight=self.ready,
                                    executor=kwargs.pop('executor', self.executor),
                                    inventory=kwargs.pop('inventory', lambda: ([], [])), **kwargs)

    def test_preflight_failure_has_no_execution_or_output_side_effect(self):
        with self.assertRaisesRegex(ValueError, 'prerequisites'):
            runner.run_suite(self.config, self.root / 'out', preflight={'ok': False})
        self.assertFalse((self.root / 'out').exists())

    def test_successful_worker_results_are_validated_and_published(self):
        record = self.run_suite()
        self.assertEqual(record['status'], 'ok')
        self.assertFalse(record['full_paper_batches'])
        self.assertEqual([r['timing_s']['mean'] for r in record['cases']], [.25, .25])
        self.assertEqual(json.loads((self.root / 'out/summary.json').read_text())['status'], 'ok')
        with self.assertRaises(FileExistsError):
            self.run_suite()

    def test_worker_uses_numeric_cuda_ids_and_expected_physical_uuids(self):
        visible = []
        def observe(*args, **kw):
            visible.append(kw['env']['CUDA_VISIBLE_DEVICES'])
            # vLLM 0.8's NVML platform parses every visible device with int().
            self.assertEqual([int(item) for item in visible[-1].split(',')], [0])
            self.assertEqual(kw['env']['CUDA_DEVICE_ORDER'], 'PCI_BUS_ID')
            return self.executor(*args, **kw)
        record = self.run_suite(executor=observe)
        self.assertEqual(visible, ['0', '0'])
        self.assertEqual(record['protocol']['devices'], ['GPU-fixture'])
        self.assertEqual(self.config['devices'], ['0'])

    def test_interrupted_case_has_terminal_failure_status(self):
        def interrupted(*args, **kw):
            raise KeyboardInterrupt('fixture interruption')
        with self.assertRaises(KeyboardInterrupt):
            self.run_suite(executor=interrupted)
        record = json.loads((self.root / 'out/summary.json').read_text())
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(record['cases'][0]['status'], 'failed')
        self.assertIn('KeyboardInterrupt', record['cases'][0]['error'])
        self.assertNotIn('timing_s', record['cases'][0])

    def test_failed_process_stops_without_fabricating_missing_timings(self):
        calls = []
        def fail(*args, **kw):
            calls.append(args)
            return dict(status='failed', returncode=1)
        result = self.run_suite(executor=fail)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(calls), 1)
        self.assertNotIn('timing_s', result['cases'][0])

    def test_model_change_invalidates_suite(self):
        def changed(*args, **kw):
            result = self.executor(*args, **kw)
            (Path(self.config['model_path']) / 'config.json').write_text('{}')
            return result
        with self.assertRaisesRegex(RuntimeError, 'model files changed'):
            self.run_suite(executor=changed)
        self.assertEqual(json.loads((self.root / 'out/summary.json').read_text())['status'], 'failed')

    def test_gpu_context_cleanup_has_a_bounded_grace_period(self):
        left = {'pid': 42, 'gpu_uuid': 'GPU-fixture'}
        inventory = iter([([], [left]), ([], [])])
        with patch.object(runner.time, 'sleep') as sleep:
            self.assertEqual(runner.wait_gpu_quiet(lambda: next(inventory), {'GPU-fixture'}, set()), [])
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(runner.wait_gpu_quiet(lambda: ([], [left]), {'GPU-fixture'}, set(), timeout=0), [left])


if __name__ == '__main__':
    unittest.main()
