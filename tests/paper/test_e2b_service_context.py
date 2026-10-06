"""Guard and failure-path tests without changing services or launching a VM."""
import copy
import datetime
from contextlib import ExitStack
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import sys
import types
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
    def __init__(self, node, cpus, *, observer=None, interval_s=.025):
        self.stop = threading.Event()
        self.worker = Mock(ident=None)
        self.worker.is_alive.return_value = False
        self.rows, self.errors = {}, []
        self.discarded_cgroups, self.discarded_processes = [], []
        self.observer = observer

    def start(self):
        self.worker.start()

    def stop_worker(self):
        self.stop.set()
        return False

    def evidence(self):
        return {'samples': [], 'errors': self.errors, 'observer': {}}

    def finish(self, path):
        m.save(path, {'samples': [], 'errors': []})

    def verify_ids(self, path):
        return {'expected_sandbox_ids': ['measured'], 'observed_sandbox_ids': ['measured']}


class E2BServiceTests(unittest.TestCase):
    def config(self, node=0):
        return {'e2b': {'api_url': 'http://127.0.0.1:3100', 'sandbox_url': 'http://127.0.0.1:3102'},
                'measurement': {'numa_node': node, 'cpus': m.LANE_CPUS.get(node, '0-3')}}

    def test_all_four_nodes_keep_fixed_cpus_and_both_leases(self):
        with patch.object(m.os,'geteuid',return_value=0),patch.object(m,'assert_backend_ready'), \
             patch.object(m,'require_pinned_parent',return_value=77) as numa, \
             patch.object(m,'require_results_lease',return_value=88) as results:
            for node,cpus in ((0,'0-3'),(1,'28-31'),(2,'48-51'),(3,'72-75')):
                self.assertEqual(m.require_admission(self.config(node),node,cpus),
                                 {'numa_lease_owner':77,'results_lease_owner':88})
                numa.assert_called_with(node,cpus)
            self.assertEqual(results.call_count,4)
        for node,cpus in ((1,'28-31'),(2,'48-51')):
            with patch.object(m.os,'geteuid',return_value=0),patch.object(m,'assert_backend_ready'), \
                 patch.object(m,'require_pinned_parent',side_effect=ValueError('missing NUMA lease')), \
                 patch.object(m,'run') as mutation:
                with self.assertRaisesRegex(ValueError,'NUMA lease'):
                    m.require_admission(self.config(node),node,cpus)
                mutation.assert_not_called()

    def test_admission_rejects_unsupported_node_or_wrong_lane_cpus(self):
        for uid,node,cpus in ((1000,0,'0-3'),(0,4,'96-99'),(0,True,'28-31'),
                              (0,0,'4-7'),(0,1,'24-27'),(0,2,'28-31'),(0,3,'')):
            with patch.object(m.os,'geteuid',return_value=uid),patch.object(m,'run') as mutation:
                with self.assertRaises(ValueError):m.require_admission(self.config(node),node,cpus)
                mutation.assert_not_called()

    def test_admission_rejects_remote_api_before_mutation(self):
        config = self.config();config['e2b']['api_url'] = 'https://api.e2b.dev'
        with patch.object(m.os, 'geteuid', return_value=0), patch.object(m, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'registered local API'):
                m.require_admission(config, 0, '0-3')
            run.assert_not_called()

    def test_admission_rejects_recovery_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            guard = Path(folder)/'GUARD';guard.touch()
            with patch.object(m, 'GUARD', guard), patch.object(m.os, 'geteuid', return_value=0):
                with self.assertRaisesRegex(RuntimeError, 'recovery'):
                    m.require_admission(self.config(), 0, '0-3')
                with self.assertRaisesRegex(RuntimeError, 'recovery'):
                    m.assert_backend_ready()

    def test_admission_requires_both_leases(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(m, 'GUARD', Path(folder)/'absent'), patch.object(m.os, 'geteuid', return_value=0), \
                 patch.object(m, 'require_pinned_parent', return_value=77) as numa, \
                 patch.object(m, 'require_results_lease', side_effect=ValueError('missing results EX')):
                with self.assertRaisesRegex(ValueError, 'results EX'):
                    m.require_admission(self.config(), 0, '0-3')
                numa.assert_called_once_with(0, '0-3')

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
                 foreign_daemon=False, stop_failure=False, stopped_failure=False, restore_stop_failure=False,
                 storage=False, storage_prepare_error=False, storage_restore_error=False):
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
        owned_storage=Mock(out=root/'e2b-storage',ram=root/'e2b-storage/ram')
        if storage_prepare_error:owned_storage.prepare_stopped.side_effect=RuntimeError('RAM staging failed')
        if storage_restore_error:owned_storage.restore_stopped.side_effect=RuntimeError('RAM restore busy')
        self.owned_storage=owned_storage
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules,{'ae.scripts.e2b_working_storage':types.SimpleNamespace(WorkingStorage=Mock(return_value=owned_storage))}))
            for name, value in [('SYSTEMD_RUNTIME',root/'runtime'),('SYSTEMD_CONTROL',root/'control'),('VM_ROOT',vm),('GUARD',guard)]:
                stack.enter_context(patch.object(m, name, value))
            for name, kwargs in [
                ('require_admission',dict(return_value={'results_lease_owner':77,'numa_lease_owner':77})),
                ('observer_placement',dict(return_value=Mock())),
                ('snapshot',dict(return_value=copy.deepcopy(before))), ('idle',dict(return_value=group())),
                ('cgroup',dict(return_value=copy.deepcopy(before['vm_root']))),
                ('run',dict(side_effect=run)), ('unit',dict(side_effect=unit)), ('container',dict(side_effect=container)),
                ('output',dict(return_value='MainPID=11\nActiveState=active')),
                ('require_stopped_units',dict(side_effect=RuntimeError('cgroup populated') if stopped_failure else None)),
                ('wait_actual',dict(side_effect=wait_actual)), ('restore_root',dict()), ('restore_stopped_unit_cgroups',dict()),
                ('APINodeReadiness',dict(return_value=Mock(before=Mock(return_value={}), next_restart=Mock(return_value=Mock())))),
                ('wait_restored',dict(side_effect=RuntimeError('bad original policy') if restore_error else lambda *a,**k: copy.deepcopy(before))),
                ('VMProof',dict(side_effect=FakeProof))]:
                stack.enter_context(patch.object(m, name, **kwargs))
            try:
                with m.service_placement(self.config(), root/'evidence', fanout_path=root/'fanout.json',
                                         working_storage=storage,source_sha256='a'*64):
                    if body_error:raise RuntimeError('producer failed')
            except RuntimeError as error:
                self.last_error = error
                return commands, guard.exists(), str(error), root
        return commands, guard.exists(), None, root

    def test_ram_restore_failure_prevents_shared_restore_and_original_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            commands,guard,error,root=self.workflow(folder,storage=True,storage_restore_error=True)
            self.assertTrue(guard);self.assertIn('RAM storage restoration failed',error)
            # Only initial three starts occurred; restoration wrote no properties,
            # container update or unit start after its mandatory stop barrier.
            second_stop=[i for i,c in enumerate(commands) if c[:2]==('systemctl','stop')][1]
            self.assertEqual(commands[second_stop+1:],[])
            self.owned_storage.restore_stopped.assert_called_once()

    def test_ram_partial_prepare_failure_is_restored_before_original_services_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            commands,guard,error,root=self.workflow(folder,storage=True,storage_prepare_error=True)
            self.assertIn('RAM staging failed',error);self.assertFalse(guard)
            self.owned_storage.restore_stopped.assert_called_once()
            self.owned_storage.verify_active.assert_not_called()
            self.assertTrue((root/'evidence/after.json').is_file())

    def test_combined_producer_and_ram_restore_failure_retains_original_cause(self):
        with tempfile.TemporaryDirectory() as folder:
            commands,guard,error,root=self.workflow(folder,storage=True,body_error=True,storage_restore_error=True)
            self.assertTrue(guard);self.assertEqual(str(self.last_error.__cause__),'producer failed')
            result=json.loads((root/'evidence/transaction-result.json').read_text())
            self.assertEqual(result['original_error']['message'],'producer failed')
            self.assertEqual(result['restoration_errors'][0]['message'],'RAM restore busy')

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


class APIReadinessTests(unittest.TestCase):
    def cursor(self):
        stack=ExitStack();self.addCleanup(stack.close)
        folder=stack.enter_context(tempfile.TemporaryDirectory())
        log=Path(folder)/'api.log';log.write_bytes(b'old log\n')
        ticks={11:21,111:121,211:221}
        real_stat=Path.stat
        def observed_stat(path,*args,**kwargs):
            if str(path).startswith('/proc/') and str(path).endswith('/fd/1'):
                return real_stat(log)
            return real_stat(path,*args,**kwargs)
        stack.enter_context(patch.object(m,'API_LOG',log))
        stack.enter_context(patch.object(m,'start_ticks',side_effect=lambda pid:ticks[pid]))
        stack.enter_context(patch.object(m.os,'readlink',return_value=str(log)))
        stack.enter_context(patch.object(Path,'stat',observed_stat))
        cursor=m.APINodeReadiness({'pid':11,'start_ticks':21})
        return cursor,log,ticks

    def append(self,log,pid=111,*,message='API internal status',nodes=None,at=None,instance='current-instance'):
        row={'service':'orchestration-api','internal':True,'pid':pid}
        if message=='Starting API service...':row['service.instance.id']=instance
        else:
            row['nodes']=nodes if nodes is not None else [{'id':'local','status':'ready','sandboxes':0}]
            row['nodes_count']=len(row['nodes'])
        at=at or datetime.datetime.now(datetime.timezone.utc)
        line=f'{at.isoformat()}  \x1b[34mINFO\x1b[0m  {message}  {json.dumps(row)}\n'.encode()
        with log.open('ab') as f:f.write(line)

    def test_old_pid_and_pre_cursor_status_cannot_satisfy_readiness(self):
        cursor,log,ticks=self.cursor()
        # Historical entries may even have the future numeric PID; the fresh
        # cursor must skip their startup and ready status entirely.
        self.append(log,message='Starting API service...');self.append(log)
        cursor=m.APINodeReadiness({'pid':11,'start_ticks':21})
        self.assertEqual(cursor.initial_offset,log.stat().st_size)
        self.append(log,pid=11,message='Starting API service...')
        self.append(log,pid=11)
        self.assertFalse(cursor.poll({'pid':111,'start_ticks':121}))
        self.assertIsNone(cursor.startup);self.assertIsNone(cursor.latest)

    def test_current_status_requires_fresh_startup_then_local_ready(self):
        cursor,log,ticks=self.cursor();api={'pid':111,'start_ticks':121}
        self.append(log)
        self.assertFalse(cursor.poll(api))
        self.append(log,message='Starting API service...')
        self.append(log,nodes=[])
        self.assertFalse(cursor.poll(api))
        self.append(log,nodes=[{'id':'local','status':'connecting','sandboxes':0}])
        self.assertFalse(cursor.poll(api))
        self.append(log)
        self.assertTrue(cursor.poll(api))
        proof=cursor.evidence()
        self.assertEqual(proof['api'],api)
        self.assertEqual(proof['startup']['service_instance_id'],'current-instance')
        self.assertGreaterEqual(proof['ready_status']['offset'],proof['offset'])
        self.assertNotIn('template_managers',json.dumps(proof))

    def test_real_console_log_compact_timezone_reaches_ready(self):
        cursor,log,ticks=self.cursor();api={'pid':111,'start_ticks':121}
        # Exact timestamp/ANSI/node shape emitted by the registered Go API.
        # isoformat() in the other tests produces +08:00, masking the
        # Python 3.10 rejection of the actual logger's +0800 offset.
        at=datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        stamp=at.strftime('%Y-%m-%dT%H:%M:%S.%f%z')
        rows=[
            ('Starting API service...', {'service.instance.id':'live-instance'}),
            ('API internal status', {'nodes_count':0,'nodes':[]}),
            ('API internal status', {'nodes_count':1,'nodes':[{'id':'local','sandboxes':0,'status':'ready'}]}),
        ]
        for index,(message,fields) in enumerate(rows):
            row={'service':'orchestration-api','internal':True,'pid':111,**fields}
            with log.open('ab') as handle:
                handle.write(f'{stamp}  \x1b[34mINFO\x1b[0m  {message}  {json.dumps(row)}\n'.encode())
            self.assertEqual(cursor.poll(api),index==2)
        self.assertEqual(cursor.evidence()['ready_status']['nodes_count'],1)

    def test_ready_followed_by_current_not_ready_in_same_batch_is_rejected(self):
        cursor,log,ticks=self.cursor()
        self.append(log,message='Starting API service...');self.append(log);self.append(log,nodes=[])
        self.assertFalse(cursor.poll({'pid':111,'start_ticks':121}))

    def test_zero_sandboxes_on_foreign_or_unhealthy_node_is_not_ready(self):
        cursor,log,ticks=self.cursor();api={'pid':111,'start_ticks':121}
        self.append(log,message='Starting API service...')
        for nodes in ([{'id':'foreign','status':'ready','sandboxes':0}],
                      [{'id':'local','status':'unhealthy','sandboxes':0}],
                      [{'id':'local','status':'ready','sandboxes':1}],
                      [{'id':'local','status':'ready','sandboxes':False}]):
            self.append(log,nodes=nodes);self.assertFalse(cursor.poll(api))

    def test_rotation_and_truncation_fail_closed(self):
        cursor,log,ticks=self.cursor();api={'pid':111,'start_ticks':121}
        log.rename(log.with_suffix('.old'));log.write_bytes(b'')
        with self.assertRaisesRegex(RuntimeError,'rotated'):cursor.poll(api)
        cursor,log,ticks=self.cursor()
        log.write_bytes(b'')
        with self.assertRaisesRegex(RuntimeError,'truncated'):cursor.poll(api)

    def test_live_pid_reuse_or_another_restart_cannot_be_ready(self):
        cursor,log,ticks=self.cursor();api={'pid':111,'start_ticks':121}
        cursor.bind(api);ticks[111]=122
        with self.assertRaisesRegex(RuntimeError,'PID/start'):cursor.poll(api)
        ticks[111]=121
        with self.assertRaisesRegex(RuntimeError,'restarted again'):cursor.poll({'pid':211,'start_ticks':221})

    def test_old_timestamp_and_second_service_instance_are_rejected(self):
        cursor,log,ticks=self.cursor();api={'pid':111,'start_ticks':121}
        old=cursor.captured_at-datetime.timedelta(seconds=1)
        self.append(log,message='Starting API service...',at=old);self.append(log,at=old)
        self.assertFalse(cursor.poll(api))
        self.append(log,message='Starting API service...')
        self.append(log,message='Starting API service...',instance='different-instance')
        with self.assertRaisesRegex(RuntimeError,'service instance changed'):cursor.poll(api)

    def test_restoration_uses_new_eof_and_new_api_startup(self):
        cursor,log,ticks=self.cursor();api={'pid':111,'start_ticks':121}
        self.append(log,message='Starting API service...');self.append(log)
        self.assertTrue(cursor.poll(api))
        restored=cursor.next_restart()
        self.assertEqual(restored.initial_offset,log.stat().st_size)
        self.assertFalse(restored.poll({'pid':211,'start_ticks':221}))
        self.append(log,pid=211,message='Starting API service...',instance='original-restarted')
        self.append(log,pid=211)
        self.assertTrue(restored.poll({'pid':211,'start_ticks':221}))

    def test_wait_actual_and_restored_wait_for_current_node_before_return(self):
        before=fixture();readiness=Mock()
        readiness.poll.side_effect=[False,True]
        readiness.evidence.return_value={'api':{'pid':111,'start_ticks':121}}
        with patch.object(m,'snapshot',return_value=before) as snapshot, patch.object(m,'assert_pinned'), \
             patch.object(m,'idle'),patch.object(m.time,'sleep') as sleep:
            actual=m.wait_actual({},before,0,'0-3',readiness=readiness)
        self.assertEqual(snapshot.call_count,2);sleep.assert_called_once_with(.2)
        self.assertEqual(actual['api_node_readiness'],readiness.evidence.return_value)
        readiness.poll.side_effect=[False,True]
        with patch.object(m,'snapshot',return_value=before) as snapshot,patch.object(m,'assert_restored'), \
             patch.object(m,'idle'),patch.object(m.time,'sleep'):
            restored=m.wait_restored({},before,readiness=readiness)
        self.assertEqual(snapshot.call_count,2)
        self.assertEqual(restored['api_node_readiness'],readiness.evidence.return_value)


if __name__ == '__main__':
    unittest.main()
