"""Guard and failure-path tests without changing services or launching a VM."""
import copy
from contextlib import ExitStack
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

source = Path(__file__).resolve().parents[2] / 'ae/scripts/e2b_service_context.py'
if not source.is_file():
    source = Path(__file__).with_name('e2b_service_context.py')
spec = importlib.util.spec_from_file_location('e2b_service_context', source)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def group(node=None, cpus='0-95'):
    return {'path': '/sys/fs/cgroup/e2b', 'inode': 37,
            'cpuset.cpus': cpus if node is not None else '', 'cpuset.mems': str(node) if node is not None else '',
            'cpuset.cpus.effective': cpus, 'cpuset.mems.effective': str(node) if node is not None else '0-5',
            'memory.swap.max': '0' if node is not None else 'max', 'memory.swap.current': '0',
            'cgroup.subtree_control': 'cpu memory' + (' cpuset' if node is not None else ''),
            'cgroup.events': 'populated 0\nfrozen 0'}


def task(pid=11, node=None, cpus='0-95', policy='default'):
    return {'pid': pid, 'start_ticks': 21, 'exe': '/registered/e2b', 'cpus': cpus,
            'mems': str(node) if node is not None else '0-5', 'numa_policies': {policy: 12}}


def fixture():
    row = {'units': {}, 'containers': {}, 'vm_root': group()}
    for i, name in enumerate(m.UNITS):
        t = task(pid=11+i)
        if name.endswith('orchestrator.service'):
            t.update(cpus='52-55', numa_policies={'bind:2': 25})
        row['units'][name] = {**{p: '' for p in m.PROPERTIES},
            'MemorySwapMax': 'infinity', 'NUMAPolicy': 'bind' if i == 2 else 'n/a',
            'NUMAMask': '2' if i == 2 else '', 'CPUAffinity': '52-55' if i == 2 else '',
            'process': t, 'binary_sha256': 'fixture-sha', 'tasks': [copy.deepcopy(t)], 'threads': [copy.deepcopy(t)],
            'cgroup': group(), 'dropins': {'/registered/unit': 'unit-sha'}}
    for i, name in enumerate(m.CONTAINERS):
        row['containers'][name] = {'id': 'a'+str(i), 'pid': 30+i,
            'process': task(pid=30+i), 'cpus': '', 'mems': '', 'cgroup': group(), 'tasks': [], 'threads': []}
    return row


class FakeProof:
    def __init__(self, node, cpus):
        self.stop = threading.Event()
        self.worker = Mock(ident=None)
        self.worker.is_alive.return_value = False
        self.rows, self.errors = {}, []

    def finish(self, path):
        m.save(path, {'samples': [], 'errors': []})

    def verify_ids(self, path):
        return {'expected_sandbox_ids': ['measured'], 'observed_sandbox_ids': ['measured']}


class E2BServiceTests(unittest.TestCase):
    def config(self, node=0):
        return {'e2b': {'api_url': 'http://127.0.0.1:3100', 'sandbox_url': 'http://127.0.0.1:3102'},
                'measurement': {'numa_node': node, 'cpus': '4-7'}}

    def test_admission_rejects_default_reviewer_nodes(self):
        with patch.object(m.os, 'geteuid', return_value=0):
            for node in (1, 2):
                with self.assertRaisesRegex(ValueError, 'NUMA0/3'):
                    m.require_admission(self.config(node), node, '4-7')

    def test_admission_rejects_remote_api_before_mutation(self):
        config = self.config();config['e2b']['api_url'] = 'https://api.e2b.dev'
        with patch.object(m.os, 'geteuid', return_value=0), patch.object(m, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'registered local API'):
                m.require_admission(config, 0, '4-7')
            run.assert_not_called()

    def test_admission_rejects_recovery_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            guard = Path(folder)/'GUARD';guard.touch()
            with patch.object(m, 'GUARD', guard), patch.object(m.os, 'geteuid', return_value=0):
                with self.assertRaisesRegex(RuntimeError, 'recovery'):
                    m.require_admission(self.config(), 0, '4-7')
                with self.assertRaisesRegex(RuntimeError, 'recovery'):
                    m.assert_backend_ready()

    def test_admission_requires_both_leases(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(m, 'GUARD', Path(folder)/'absent'), patch.object(m.os, 'geteuid', return_value=0), \
                 patch.object(m, 'require_pinned_parent', return_value=77) as numa, \
                 patch.object(m, 'require_results_lease', side_effect=ValueError('missing results EX')):
                with self.assertRaisesRegex(ValueError, 'results EX'):
                    m.require_admission(self.config(), 0, '4-7')
                numa.assert_called_once_with(0, '4-7')

    def test_actual_masks_and_swap_limit_are_required(self):
        row = {'cgroup': group(0, '4-7'), 'tasks': [task(node=0, cpus='4-7')]}
        m.assert_pinned(row, 0, '4-7')
        row['cgroup']['cpuset.mems.effective'] = '0-5'
        with self.assertRaisesRegex(RuntimeError, 'effective'):
            m.assert_pinned(row, 0, '4-7')
        row['cgroup'] = group(0, '4-7');row['cgroup']['memory.swap.max'] = 'max'
        with self.assertRaisesRegex(RuntimeError, 'no-swap'):
            m.assert_pinned(row, 0, '4-7')

    def test_vma_policy_must_follow_lane(self):
        row = {'cgroup': group(0, '4-7'), 'tasks': [task(node=0, cpus='4-7', policy='bind:2')]}
        with self.assertRaisesRegex(RuntimeError, 'actual memory policy'):
            m.assert_pinned(row, 0, '4-7')

    def test_pid_reuse_is_not_same_identity(self):
        a = task();b = copy.deepcopy(a);b['start_ticks'] += 1
        self.assertFalse(m.identity_matches(a, b))

    def test_inventory_never_places_key_in_command_or_manifest(self):
        calls = []
        def urlopen(request, timeout):
            calls.append(request)
            return io.BytesIO(json.dumps([{'sandboxID': 'owned', 'metadata': {'official_fork_run': 'run', 'private': 'DO_NOT_SAVE'}}]).encode())
        with patch.dict(m.os.environ, {'E2B_API_KEY': 'DO_NOT_LOG'}), patch.object(m.urllib.request, 'urlopen', side_effect=urlopen), patch.object(m, 'run') as run:
            result = m.inventory(self.config())
        self.assertEqual(result, [{'id': 'owned', 'run': 'run'}])
        self.assertNotIn('DO_NOT', json.dumps(result))
        self.assertNotIn('DO_NOT_LOG', calls[0].full_url)
        self.assertEqual(calls[0].get_header('X-api-key'), 'DO_NOT_LOG')
        run.assert_not_called()

    def test_vm_ids_must_all_have_observed_process_proof(self):
        proof = m.VMProof(0, '4-7')
        proof.rows = {'sample': {'cgroup': {'path': '/sys/fs/cgroup/e2b/sbx-child-random'}}}
        with tempfile.TemporaryDirectory() as folder:
            f = Path(folder)/'fanout.json'
            f.write_text(json.dumps([{'children': [{'sandbox_id': 'child'}], 'cleanup': [{'resource':'source','id':'source'}]}]))
            with self.assertRaisesRegex(RuntimeError, 'Missing placement proof'):
                proof.verify_ids(f)
            proof.rows['source'] = {'cgroup': {'path': '/sys/fs/cgroup/e2b/sbx-source-random'}}
            self.assertEqual(proof.verify_ids(f)['expected_sandbox_ids'], ['child', 'source'])

    def test_restore_rejects_empty_systemd_property_with_stale_kernel_mask(self):
        before = fixture();after = copy.deepcopy(before)
        after['units'][m.UNITS[0]]['cgroup']['cpuset.mems.effective'] = '0'
        with self.assertRaisesRegex(RuntimeError, 'actual cgroup'):
            m.assert_restored(before, after)

    def test_restore_rejects_wrong_rebound_vma_policy(self):
        before = fixture();after = copy.deepcopy(before)
        after['units'][m.UNITS[2]]['process']['numa_policies'] = {'bind:0': 12}
        with self.assertRaisesRegex(RuntimeError, 'policy did not restore'):
            m.assert_restored(before, after)

    def test_restore_accepts_new_daemon_pid_but_original_policy(self):
        before = fixture();after = copy.deepcopy(before)
        for name in m.UNITS:
            after['units'][name]['process']['pid'] += 100
        m.assert_restored(before, after)

    def test_restore_rejects_container_worker_left_on_new_node(self):
        before = fixture();after = copy.deepcopy(before)
        after['containers'][m.CONTAINERS[0]]['threads'] = [task(pid=71, node=0, cpus='4-7')]
        with self.assertRaisesRegex(RuntimeError, 'container actual task'):
            m.assert_restored(before, after)

    def test_restore_rejects_container_stale_effective_cgroup(self):
        before = fixture();after = copy.deepcopy(before)
        after['containers'][m.CONTAINERS[0]]['cgroup']['cpuset.mems.effective'] = '0'
        with self.assertRaisesRegex(RuntimeError, 'container actual cgroup'):
            m.assert_restored(before, after)

    def test_error_record_redacts_key(self):
        with patch.dict(m.os.environ, {'E2B_API_KEY': 'PRIVATE_API_KEY'}):
            self.assertEqual(m.error_record(RuntimeError('failure PRIVATE_API_KEY'))['message'], 'failure <redacted>')

    def test_stopped_barrier_rejects_live_main_pid(self):
        with patch.object(m, 'output', return_value='MainPID=11\nActiveState=active'):
            with self.assertRaisesRegex(RuntimeError, 'has not stopped'):
                m.require_stopped_units()

    def test_stopped_barrier_rejects_populated_cgroup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'system.slice'/m.UNITS[0];path.mkdir(parents=True)
            row = group();row['cgroup.events'] = 'populated 1\nfrozen 0'
            with patch.object(m, 'CGROUP', Path(folder)), patch.object(m, 'output', return_value='MainPID=0\nActiveState=inactive'), \
                 patch.object(m, 'cgroup', return_value=row), patch.object(m, 'cgroup_processes', return_value=[]):
                with self.assertRaisesRegex(RuntimeError, 'remains populated'):
                    m.require_stopped_units()

    def workflow(self, folder, *, body_error=False, restore_error=False, foreign_container=False,
                 foreign_daemon=False, stop_failure=False, stopped_failure=False, restore_stop_failure=False):
        root = Path(folder);vm = root/'vm';vm.mkdir()
        for name, value in group().items():
            if name not in ('path', 'inode'):
                (vm/name).write_text(value)
        before = fixture()
        before['vm_root']['inode'] = vm.stat().st_ino
        before['vm_root']['path'] = str(vm)
        active = copy.deepcopy(before)
        for row in active['units'].values():row['process']['pid'] += 100
        commands, phase = [], {'active': False, 'foreign': False, 'stops': 0}
        def run(*args):
            commands.append(tuple(map(str,args)))
            if args[:2] == ('systemctl', 'stop'):
                phase['stops'] += 1
                if stop_failure or (restore_stop_failure and phase['stops'] == 2):
                    raise RuntimeError('stop failed')
            if args[:2] == ('systemctl', 'start'):
                phase['active'] = True
        def unit(name):
            row = copy.deepcopy((active if phase['active'] else before)['units'][name])
            if phase['foreign']:
                row['process']['pid'] += 777
                row['process']['start_ticks'] += 1
            return row
        def wait_actual(*args, **kwargs):
            if foreign_daemon:
                phase['foreign'] = True
                raise RuntimeError('readiness failed after foreign restart')
            return active
        def container(name):
            row = copy.deepcopy(before['containers'][name])
            if foreign_container:row['id'] = 'foreign'
            return row
        guard = root/'RECOVERY_REQUIRED'
        with ExitStack() as stack:
            for name, value in [('SYSTEMD_RUNTIME',root/'runtime'),('SYSTEMD_CONTROL',root/'control'),('VM_ROOT',vm),('GUARD',guard)]:
                stack.enter_context(patch.object(m, name, value))
            for name, kwargs in [
                ('require_admission',dict(return_value={'results_lease_owner':77,'numa_lease_owner':77})),
                ('snapshot',dict(return_value=copy.deepcopy(before))), ('idle',dict(return_value=group())),
                ('cgroup',dict(return_value=copy.deepcopy(before['vm_root']))),
                ('run',dict(side_effect=run)), ('unit',dict(side_effect=unit)), ('container',dict(side_effect=container)),
                ('output',dict(return_value='MainPID=11\nActiveState=active')),
                ('require_stopped_units',dict(side_effect=RuntimeError('cgroup populated') if stopped_failure else None)),
                ('wait_actual',dict(side_effect=wait_actual)), ('restore_root',dict()), ('restore_stopped_unit_cgroups',dict()),
                ('wait_restored',dict(side_effect=RuntimeError('bad original policy') if restore_error else lambda *a,**k: copy.deepcopy(before))),
                ('VMProof',dict(side_effect=FakeProof))]:
                stack.enter_context(patch.object(m, name, **kwargs))
            try:
                with m.service_placement(self.config(), root/'evidence', fanout_path=root/'fanout.json'):
                    if body_error:raise RuntimeError('producer failed')
            except RuntimeError as error:
                self.last_error = error
                return commands, guard.exists(), str(error), root
        return commands, guard.exists(), None, root

    def test_success_restores_services_and_clears_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            commands, guard, error, root = self.workflow(folder)
            self.assertIsNone(error);self.assertFalse(guard)
            self.assertEqual(sum(c[:2] == ('systemctl','stop') for c in commands), 2)
            self.assertEqual(sum(c[:2] == ('systemctl','start') for c in commands), 4)
            self.assertTrue((root/'evidence/after.json').is_file())
            self.assertFalse(list((root/'runtime').rglob(m.DROP_NAME)))

    def test_producer_error_still_restores(self):
        with tempfile.TemporaryDirectory() as folder:
            commands, guard, error, root = self.workflow(folder, body_error=True)
            self.assertEqual(error, 'producer failed');self.assertFalse(guard)
            self.assertEqual(sum(c[:2] == ('systemctl','start') for c in commands), 4)

    def test_restore_failure_keeps_guard_and_nonzero_error(self):
        with tempfile.TemporaryDirectory() as folder:
            commands, guard, error, root = self.workflow(folder, restore_error=True)
            self.assertTrue(guard);self.assertIn('restoration failed', error)
            self.assertIn('bad original policy', (root/'evidence/after.json').read_text())

    def test_foreign_container_is_not_updated_or_deleted(self):
        with tempfile.TemporaryDirectory() as folder:
            commands, guard, error, root = self.workflow(folder, foreign_container=True)
            self.assertTrue(guard);self.assertIn('resources require recovery', error)
            self.assertFalse(any(c[:2] == ('docker','update') for c in commands))
            self.assertFalse(any('rm' in c or 'kill' in c for c in commands))

    def test_partial_start_foreign_daemon_is_not_stopped(self):
        with tempfile.TemporaryDirectory() as folder:
            commands, guard, error, root = self.workflow(folder, foreign_daemon=True)
            self.assertTrue(guard);self.assertIn('resources require recovery', error)
            self.assertEqual(sum(c[:2] == ('systemctl','stop') for c in commands), 1)
            result = json.loads((root/'evidence/transaction-result.json').read_text())
            self.assertIn('foreign restart', result['original_error']['message'])
            self.assertIn('unknown instance', result['restoration_errors'][0]['message'])

    def test_stop_command_failure_never_changes_shared_resources(self):
        with tempfile.TemporaryDirectory() as folder:
            commands, guard, error, root = self.workflow(folder, stop_failure=True)
            self.assertTrue(guard);self.assertIn('stop failed', error)
            self.assertFalse(any(c[:2] == ('docker','update') or c[:2] == ('systemctl','start') or c[:2] == ('systemctl','set-property') for c in commands))
            self.assertEqual((root/'vm/cpuset.mems').read_text(), '')

    def test_actual_stopped_cgroup_barrier_prevents_shared_mutations(self):
        with tempfile.TemporaryDirectory() as folder:
            commands, guard, error, root = self.workflow(folder, stopped_failure=True)
            self.assertTrue(guard);self.assertIn('stop failed', error)
            self.assertFalse(any(c[:2] == ('docker','update') or c[:2] == ('systemctl','start') or c[:2] == ('systemctl','set-property') for c in commands))

    def test_restore_stop_failure_never_mutates_shared_resources_or_starts(self):
        with tempfile.TemporaryDirectory() as folder:
            commands, guard, error, root = self.workflow(folder, restore_stop_failure=True)
            self.assertTrue(guard);self.assertIn('restoration was not attempted', error)
            final_stop = max(i for i,c in enumerate(commands) if c[:2] == ('systemctl','stop'))
            self.assertEqual(commands[final_stop+1:], [])
            self.assertEqual((root/'vm/cpuset.mems').read_text(), '0\n')
            self.assertEqual(len(list((root/'runtime').rglob(m.DROP_NAME))), len(m.UNITS))

    def test_producer_and_restore_errors_are_both_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            commands, guard, error, root = self.workflow(folder, body_error=True, restore_error=True)
            result = json.loads((root/'evidence/transaction-result.json').read_text())
            self.assertTrue(guard);self.assertIn('restoration failed', error)
            self.assertEqual(result['original_error']['message'], 'producer failed')
            self.assertIn('bad original policy', result['restoration_errors'][0]['message'])
            self.assertIsInstance(self.last_error.__cause__, RuntimeError)
            self.assertEqual(str(self.last_error.__cause__), 'producer failed')


if __name__ == '__main__':
    unittest.main()
