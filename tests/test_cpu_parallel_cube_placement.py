"""Exercise actual Cube preparation with fake contexts and real ExitStack cleanup."""
import ast
from contextlib import contextmanager, ExitStack
import copy
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def preparation_method():
    tree = ast.parse((ROOT / 'ae/scripts/run_review.py').read_text())
    review = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Review')
    method = next(node for node in review.body if isinstance(node, ast.FunctionDef) and node.name == 'prepare_cube_service')
    merge = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'deep_merge')
    namespace = {'copy': copy, 'os': os}
    exec(compile(ast.Module(body=[merge, method], type_ignores=[]), 'run_review.py', 'exec'), namespace)
    return namespace['prepare_cube_service'], namespace


class CubePlacementTests(unittest.TestCase):
    def run_preparation(self, layout, *, cleanup_error=False, hosted=False, node=None, validate_only=False):
        events, placements, memory_calls, guards = [], [], [], []
        active = {'placement': False, 'memory': False, 'webui': False, 'mysql': False}
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            node = node if node is not None else (0 if layout == 'numa03' else 2)
            cpus = {0: '0-3', 1: '28-31', 2: '48-51', 3: '72-75'}[node]
            expects_placement = hosted or layout == 'numa03'
            args = types.SimpleNamespace(cpu_layout=layout, cube_profile=None, config=out / 'config.json', numa_node=node, cpus=cpus)
            config = {'cube': {'manage_memory_service': True, 'memory_size_gib': 16}, 'baseline_storage': 'tmpfs'}
            prepare, namespace = preparation_method()
            namespace.update(load_config=lambda path: copy.deepcopy(config),
                             pin_requested=lambda args, config: True,
                             measurement_placement=lambda args, config: {'node': args.numa_node, 'cpus': args.cpus},
                             file_record=lambda path: {'path': str(path)})
            runner = types.SimpleNamespace(args=args, config=config, experiments=['table-02-cube'], overrides={},
                output=out, attempt='attempt-001', record={}, cube_memory_manifest=None, cube_disk_manifest=None,
                cube_placement=None, save=lambda: events.append('saved'))
            runner.prepare_cube_service = types.MethodType(prepare, runner)

            @contextmanager
            def protected_service(name, directory, guard):
                if name == 'mysql':
                    self.assertTrue(active['webui'])
                active[name] = True
                events.append(name+'-enter')
                try:
                    yield
                finally:
                    self.assertFalse(active['placement'] or active['memory'])
                    events.append(name+'-guarded' if guard.exists() else name+'-restored')
                    active[name] = False

            @contextmanager
            def placement(selected_node, selected_cpus, directory, guard):
                self.assertTrue(active['webui'] and active['mysql'])
                placements.append((selected_node, selected_cpus, directory, guard))
                guards.append(guard)
                events.append('placement-enter')
                active['placement'] = True
                try:
                    yield
                finally:
                    self.assertFalse(active['memory'], 'Control plane restored before memory cleanup finished')
                    events.append('placement-guarded' if guard.exists() else 'placement-restored')
                    active['placement'] = False

            @contextmanager
            def memory_service(directory, **kwargs):
                memory_calls.append((directory, kwargs))
                if expects_placement:
                    self.assertTrue(active['placement'], 'Memory context entered before control plane placement')
                    self.assertIs(kwargs['recovery_guard'], guards[0], 'Contexts must share one guard object')
                events.append('memory-enter')
                active['memory'] = True
                manifest = directory / 'storage.json'
                directory.mkdir(parents=True)
                manifest.write_text('{}')
                try:
                    yield manifest
                finally:
                    events.append('memory-cleaned')
                    active['memory'] = False
                    if cleanup_error:
                        guard = kwargs['recovery_guard']
                        guard.parent.mkdir(parents=True, exist_ok=True)
                        guard.write_text('{"cleanup_failed": true}')
                        raise RuntimeError('fixture memory cleanup failed')

            def proof(selected_node, selected_cpus, path):
                self.assertTrue(active['placement'] and active['memory'])
                self.assertEqual((selected_node, selected_cpus), (node, cpus))
                events.append('placement-proof')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')

            def verify(effective):
                self.assertTrue(active['memory'])
                self.assertEqual(effective['measurement'], {'numa_node': node, 'cpus': cpus})
                self.assertEqual(effective['cube']['memory_manifest'], str(memory_calls[0][0] / 'storage.json'))
                events.append('verified')

            control = types.ModuleType('cube_control_context')
            control.metadata_enabled = lambda config: False
            control.placement = mock.Mock(side_effect=placement)
            control.quiesce_webui = lambda directory, guard: protected_service('webui', directory, guard)
            control.mysql_launcher = lambda directory, guard: protected_service('mysql', directory, guard)
            control.proof = mock.Mock(side_effect=proof)
            memory = types.ModuleType('cube_memory_context')
            memory.memory_service = memory_service
            cube = types.ModuleType('cube_memory')
            cube.verify = verify
            modules = {'ae.scripts.cube_control_context': control, 'ae.scripts.cube_memory_context': memory,
                       'runners.cube_memory': cube}
            environment = dict(os.environ)
            environment.pop('AE_HOSTED_CALLER_UID', None)
            if hosted:
                environment['AE_HOSTED_CALLER_UID'] = '1001'
            with mock.patch.dict(sys.modules, modules), mock.patch.dict(os.environ, environment, clear=True):
                if cleanup_error:
                    with self.assertRaisesRegex(RuntimeError, 'memory cleanup failed'):
                        with ExitStack() as contexts:
                            runner.prepare_cube_service(contexts, selected=['table-02-cube'], validate_only=validate_only)
                else:
                    with ExitStack() as contexts:
                        runner.prepare_cube_service(contexts, selected=['table-02-cube'], validate_only=validate_only)
            if validate_only:
                control.placement.assert_not_called()
                control.proof.assert_not_called()
                self.assertEqual(memory_calls, [])
                return events
            if expects_placement:
                service = out / 'environment/attempt-001/table-02-cube'
                self.assertEqual(placements, [(node, cpus, service / 'control-plane', service / 'control-plane/RECOVERY_REQUIRED.json')])
                self.assertEqual(memory_calls[0][0], service / 'cube-memory')
                self.assertEqual(runner.record['cube_control_plane_placement'], {'path': str(service / 'control-plane/placement-active.json')})
                self.assertEqual(memory_calls[0][1], {'node': node, 'cpus': cpus, 'size_gib': 16,
                                                     'recovery_guard': guards[0]})
            else:
                control.placement.assert_not_called()
                control.proof.assert_not_called()
                self.assertNotIn('cube_control_plane_placement', runner.record)
                self.assertEqual(memory_calls[0][1], {'node': node, 'cpus': cpus, 'size_gib': 16})
            return events

    def test_numa03_context_order_proof_and_shared_recovery_guard(self):
        for failed in (False, True):
            with self.subTest(cleanup_error=failed):
                events = self.run_preparation('numa03', cleanup_error=failed)
                self.assertEqual(events, ['webui-enter', 'mysql-enter', 'placement-enter', 'memory-enter', 'placement-proof', 'verified', 'saved',
                                          'memory-cleaned', 'placement-guarded' if failed else 'placement-restored',
                                          'mysql-guarded' if failed else 'mysql-restored',
                                          'webui-guarded' if failed else 'webui-restored'])

    def test_self_managed_numa12_uses_only_existing_memory_context(self):
        self.assertEqual(self.run_preparation('numa12'), ['memory-enter', 'verified', 'saved', 'memory-cleaned'])

    def test_hosted_layouts_bind_actual_lane_and_restore_after_ram_cleanup(self):
        for layout, node in (('numa12', 1), ('numa12', 2), ('numa03', 0), ('numa03', 3)):
            for failed in (False, True):
                with self.subTest(layout=layout, node=node, cleanup_error=failed):
                    events = self.run_preparation(layout, node=node, hosted=True, cleanup_error=failed)
                    self.assertEqual(events, ['webui-enter', 'mysql-enter', 'placement-enter', 'memory-enter', 'placement-proof', 'verified', 'saved',
                                              'memory-cleaned', 'placement-guarded' if failed else 'placement-restored',
                                              'mysql-guarded' if failed else 'mysql-restored',
                                              'webui-guarded' if failed else 'webui-restored'])

    def test_hosted_preflight_validates_placement_without_mutating_services(self):
        for layout in ('numa12', 'numa03'):
            with self.subTest(layout=layout):
                self.assertEqual(self.run_preparation(layout, hosted=True, validate_only=True), [])


if __name__ == '__main__':
    unittest.main()
