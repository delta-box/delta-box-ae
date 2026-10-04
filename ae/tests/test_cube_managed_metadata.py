"""Fault-injection checks; all system/service operations are mocked."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

P = Path(__file__).resolve().parents[2]
W = P / 'ae/work'
sys.path[:0] = [str(P), str(P / 'ae')]
from ae.scripts import cube_memory_context as cm
from ae.scripts import cube_control_context as m
from ae.repro.result_storage import run_lock


class Tests(unittest.TestCase):
    def test_ui_is_quiesced_and_only_resumed_after_inner_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);calls=[];state={'active':'activating'}
            def run(*args,**kw):
                calls.append(args)
                if args[:2]==('systemctl','stop'):state['active']='inactive'
            def output(*args):
                if 'ActiveState' in args:return state['active']
                if 'SubState' in args:return 'start-post'
                if 'Restart' in args:return 'no' if (p/'ui.conf').exists() else 'on-failure'
                return '0'
            with patch.object(m,'WEBUI_DROP',p/'ui.conf'),patch.object(m,'run',side_effect=run),patch.object(m,'output',side_effect=output):
                with m.quiesce_webui(p,p/'guard.json'):
                    self.assertEqual(state['active'],'inactive')
                    calls.append(('original-database-restored',))
                start=('systemctl','start','--no-block',m.WEBUI)
                self.assertGreater(calls.index(start),calls.index(('original-database-restored',)))
                self.assertFalse((p/'ui.conf').exists())

    def test_copy_rejects_loss_of_database_quiescence(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);source=p/'source';source.mkdir();(source/'data').write_bytes(b'original')
            def changed():raise RuntimeError('database restarted')
            with self.assertRaisesRegex(RuntimeError,'restarted'):
                m.copy_verified(source,p/'copy',p/'manifest',quiescent=changed)
            self.assertEqual((source/'data').read_bytes(),b'original')
            self.assertFalse((p/'copy/data').exists())

    def test_empty_docker_specs_restore_recorded_effective_masks(self):
        self.assertEqual(m.restore_masks({'cpus': '', 'mems': '', 'effective_cpus': '0-95',
                                          'effective_mems': '0-5'}), ('0-95', '0-5'))

    def test_start_order_requires_database_before_frontends_and_idle(self):
        calls = []
        with patch.object(m, 'run', side_effect=lambda *a, **kw: calls.append(a)), \
             patch.object(m, 'variables', side_effect=lambda: calls.append(('variables',))), \
             patch.object(m, 'idle', side_effect=lambda **kw: calls.append(('idle',))):
            m.service_start()
        self.assertEqual(calls, [('systemctl', 'start', m.MYSQL_UNIT), ('variables',),
                                ('systemctl', 'start', *reversed(m.FRONT)), ('idle',)])

    def test_side_effect_then_interrupt_restores_cube_resources(self):
        for fault in ('mount', 'stop', 'freeze', 'loop', 'drop', 'device_copy', 'stop-cleanup', 'drop-remove', 'reload-cleanup'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / 'original.xfs'
                source.write_bytes(b'original immutable storage')
                drop = root / 'drop.conf'
                out = root / 'out'
                s = {'ram': False, 'vol': False, 'stopped': False, 'frozen': False,
                     'loop': False, 'injected': False}
                cleanup_fault = fault in ('stop-cleanup','drop-remove','reload-cleanup')
                def interrupt(at):
                    if (at == fault or (at == 'drop' and cleanup_fault)) and not s['injected']:
                        s['injected'] = True
                        raise KeyboardInterrupt('after side effect')
                def run(*a, **kw):
                    if a[:3] == ('mount', '-t', 'tmpfs'):
                        s['ram'] = True
                        interrupt('mount')
                    elif a[:2] == ('systemctl', 'stop'):
                        if fault == 'stop-cleanup' and s['injected']:raise RuntimeError('stop failed')
                        s['stopped'] = True
                        interrupt('stop')
                    elif a[:2] == ('systemctl', 'daemon-reload') and fault == 'reload-cleanup' and s['injected']:
                        raise RuntimeError('reload failed')
                    elif a[:2] == ('systemctl', 'start'):
                        s['stopped'] = False
                    elif a[:2] == ('fsfreeze', '--freeze'):
                        s['frozen'] = True
                        interrupt('freeze')
                    elif a[:2] == ('fsfreeze', '--unfreeze'):
                        s['frozen'] = False
                    elif a[0] == 'cp':
                        shutil.copyfile(a[-2], a[-1])
                    elif a[0] == 'xfs_copy':
                        self.assertTrue(s['frozen'])
                        self.assertEqual(a[-2], '/dev/loop-test')
                        shutil.copyfile(source, a[-1])
                        interrupt('device_copy')
                    elif a[:3] == ('mount', '-t', 'xfs'):
                        s['vol'] = True
                    elif a[0] == 'umount':
                        s['ram' if Path(a[1]) == out/'ram' else 'vol'] = False
                    elif a[:2] == ('losetup', '-d'):
                        s['loop'] = False
                def output(*a):
                    if a[:2] == ('systemctl', 'is-active'):
                        return 'active'
                    if a[:2] == ('systemctl', 'show'):
                        if 'MainPID' in a:
                            return '0' if s['stopped'] else str(os.getpid())
                        if 'ActiveState' in a:return 'inactive' if s['stopped'] else 'active'
                        return '2' if 'AllowedMemoryNodes' in a else '52-55'
                    if a[0] == 'nsenter':return '/dev/loop-test'
                    if a[0] == 'findmnt':
                        return '/dev/loop-test'
                    if a[:3] == ('losetup', '--list', '--json'):
                        return json.dumps({'loopdevices': [{'back-file': str(source) + (' (deleted)' if fault=='device_copy' else '')}]})
                    if a[:2] == ('blockdev', '--getsize64'):
                        return str(64*1024**3)
                    if a[:3] == ('losetup', '--find', '--show'):
                        s['loop'] = True
                        interrupt('loop')
                        return '/dev/loop-test-new'
                    if a[:2] == ('losetup', '-j'):
                        return '/dev/loop-test-new' if s['loop'] else ''
                    return 'original unit'
                unlink = Path.unlink
                def unlink_drop(path, *a, **kw):
                    if path == drop and fault == 'drop-remove' and s['injected']:
                        raise RuntimeError('unlink failed')
                    return unlink(path, *a, **kw)
                write = Path.write_text
                def write_text(path, *a, **kw):
                    value = write(path, *a, **kw)
                    if path == drop:
                        interrupt('drop')
                    return value
                read_text = Path.read_text
                def read_masks(path, *a, **kw):
                    if str(path).startswith('/sys/fs/cgroup/cube_sandbox/'):
                        return '2' if path.name == 'cpuset.mems' else '52-55'
                    return read_text(path, *a, **kw)
                real_stat = os.stat
                lease = root/'lease'
                def named_stat(path, *a, **kw):
                    return real_stat(lease if str(path)=='/run/lock/deltabox-cube-memory.lock' else path, *a, **kw)
                with run_lock(lease) as fd, \
                     patch.object(cm.os, 'geteuid', return_value=0), \
                     patch.object(cm.os, 'stat', side_effect=named_stat), \
                     patch.object(Path, 'read_text', read_masks), \
                     patch.object(cm, 'node_available', return_value=100*cm.GIB), \
                     patch.object(cm, 'sandboxes', return_value=[]), \
                     patch.object(cm, 'PATHS', []), patch.object(cm, 'DROP', drop), \
                     patch.object(cm, 'STORAGE', root/'original-mount'), \
                     patch.object(cm, 'run', side_effect=run), patch.object(cm, 'output', side_effect=output), \
                     patch.object(cm.os.path, 'ismount', side_effect=lambda p: s['ram'] if Path(p)==out/'ram' else s['vol']), \
                     patch.object(Path, 'write_text', write_text), patch.object(Path, 'unlink', unlink_drop):
                    with self.assertRaises((KeyboardInterrupt,RuntimeError)):
                        with cm.memory_service(out, node=0, cpus='0-3', lease_fd=fd):
                            self.fail('fault was not reached')
                self.assertTrue(s['injected'])
                if cleanup_fault:
                    self.assertTrue(s['ram'] and s['vol'] and s['loop'] and s['stopped'])
                    self.assertTrue((out/'RECOVERY_REQUIRED.json').exists())
                else:
                    self.assertFalse(any(s[k] for k in ('ram', 'vol', 'stopped', 'frozen', 'loop')))
                    self.assertFalse(drop.exists())
                self.assertEqual(source.read_bytes(), b'original immutable storage')

    def test_database_unmount_failure_retains_outer_supervisor_and_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            source = p/'source'
            source.mkdir()
            (source/'data').write_bytes(b'original database')
            guard = p/'recovery.json'
            drop = p/'service/drop.conf'
            state = {'running': True, 'bound': False}
            calls = []
            def inspect(name):
                return {'id': 'owned-test-container', 'pid': 12345678, 'running': state['running'],
                        'started_at': 'fixed-start', 'finished_at': 'fixed-stop',
                        'cpus': '', 'mems': '', 'mounts': [{'Source': str(source), 'Destination': '/var/lib/mysql'}]}
            def run(*a, **kw):
                calls.append(a)
                if a[:2] == ('systemctl', 'stop') and m.MYSQL_UNIT in a:
                    state['running'] = False
                if a[:2] == ('mount', '--bind'):
                    state['bound'] = True
                if a[0] == 'umount' and Path(a[1]) == source:
                    raise RuntimeError('injected busy database bind')
            read = Path.read_text
            original_stat = Path.stat
            def path_stat(path, *a, **kw):
                if str(path) == '/proc/12345678/root/var/lib/mysql':
                    return original_stat(p/'database/ram/data')
                return original_stat(path, *a, **kw)
            def read_text(path, *a, **kw):
                if str(path) == '/proc/12345678/mountinfo':
                    return '1 2 0:1 / /var/lib/mysql rw - tmpfs tmpfs rw\n'
                return read(path, *a, **kw)
            with patch.object(m, 'MYSQL_DROP', drop), patch.object(m, 'inspect', side_effect=inspect), \
                 patch.object(m, 'run', side_effect=run), patch.object(m, 'mysql_volume_removal_disabled'), \
                 patch.object(m, 'service_cgroup_masks', return_value={'path': '/fixture'}), \
                 patch.object(m, 'restore_service_cgroup_masks'), \
                 patch.object(m, 'output', side_effect=lambda *a: 'simple' if 'Type' in a else '{}' if a[0]=='findmnt' else 'original'), \
                 patch.object(m, 'variables', return_value='flush=1;binlog=1'), \
                 patch.object(m, 'idle'), patch.object(m, 'node_available', return_value=100*m.GIB), \
                 patch.object(m, 'service_start', side_effect=lambda: state.update(running=True)), \
                 patch.object(m.os.path, 'ismount', side_effect=lambda x: state['bound'] if Path(x)==source else False), \
                 patch.object(Path, 'read_text', read_text), patch.object(Path, 'stat', path_stat):
                entered = []
                with self.assertRaises(RuntimeError):
                    with m.mysql_launcher(p, guard):
                        with m.ram_database(0, p/'database', guard):
                            entered.append(True)
                self.assertTrue(entered)
            self.assertTrue(guard.exists() and drop.exists() and state['bound'])
            self.assertFalse(state['running'])
            self.assertFalse(any(a[:2] == ('systemctl', 'restart') for a in calls))
            self.assertEqual((source/'data').read_bytes(), b'original database')


    def test_metadata_profile_rejects_remote_or_unmanaged_service(self):
        good = {'baseline_storage': 'tmpfs', 'cube': {'manage_metadata_memory': True,
                'manage_memory_service': True, 'api_url': 'http://127.0.0.1:3000'}}
        self.assertTrue(m.metadata_enabled(good))
        for key, value in [('api_url', 'http://other:3000'), ('manage_memory_service', False)]:
            bad = json.loads(json.dumps(good));bad['cube'][key] = value
            with self.assertRaises(ValueError):m.metadata_enabled(bad)

    def test_managed_context_rejects_missing_lease_before_services(self):
        config = {'baseline_storage': 'tmpfs', 'measurement': {'pin': True, 'numa_node': 0, 'cpus': '0-3'},
                  'cube': {'manage_metadata_memory': True, 'manage_memory_service': True,
                           'api_url': 'http://127.0.0.1:3000'}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(m.os, 'geteuid', return_value=0), \
             patch.object(m, 'require_pinned_parent', side_effect=ValueError('missing lease')), \
             patch.object(m, 'run') as run, patch.object(m, 'inspect') as inspect:
            with self.assertRaisesRegex(ValueError, 'missing lease'):
                with m.managed_memory_service(config, Path(tmp)/'env'):self.fail('must reject')
            run.assert_not_called();inspect.assert_not_called()

    def test_managed_context_restores_in_reverse_order(self):
        config = {'baseline_storage': 'tmpfs', 'measurement': {'pin': True, 'numa_node': 0, 'cpus': '0-3'},
                  'cube': {'manage_metadata_memory': True, 'manage_memory_service': True,
                           'api_url': 'http://127.0.0.1:3000'}}
        events=[]
        def context(name, result=None):
            @contextmanager
            def wrapped(*a, **kw):
                events.append('enter '+name)
                try:yield result
                finally:events.append('exit '+name)
            return wrapped
        with tempfile.TemporaryDirectory() as tmp, patch.object(m.os, 'geteuid', return_value=0), \
             patch.object(m, 'require_pinned_parent', return_value=42), \
             patch.object(m, 'run_lock', context('lease',7)), patch.object(cm,'DROP',Path(tmp)/'absent'), \
             patch.object(m,'sandboxes',return_value=[]), patch.object(m,'idle'), \
             patch.object(m,'database_capacity_gib',return_value=(13,1000)), \
             patch.object(m,'node_available',return_value=100*m.GIB), patch.object(m,'proof'), \
             patch.object(m,'quiesce_webui',context('ui')), patch.object(m,'mysql_launcher',context('launcher')), \
             patch.object(m,'placement',context('placement')), \
             patch.object(cm,'memory_service',context('data',Path(tmp)/'storage.json')), \
             patch.object(m,'ram_database',context('metadata')):
            with m.managed_memory_service(config,Path(tmp)/'environment') as active:
                self.assertTrue(active['recovery_guard'].endswith('RECOVERY_REQUIRED.json'))
                events.append('measurement')
        names=['lease','ui','launcher','placement','data','metadata']
        self.assertEqual(events,['enter '+n for n in names]+['measurement']+['exit '+n for n in reversed(names)])

    def test_shipped_figure8_profile_is_valid_and_bounded(self):
        config=json.loads((P/'ae/configs/spr4numa-review.json').read_text())
        override=config['review']['experiment_overrides']['figure-08-cube']
        selected={**config, **override, 'cube':{**config['cube'],**override['cube']}}
        self.assertTrue(m.metadata_enabled(selected))
        self.assertEqual(selected['cube']['fanout_forks'],[1,16])

    def test_catalog_bounds_cube_without_changing_e2b(self):
        from ae.repro.catalog import build_jobs
        config={'python':'python3'}
        jobs=build_jobs(['figure-08-cube','figure-08-e2b'],config,Path('/config'),Path('/output'))
        self.assertEqual([j['command'][j['command'].index('--forks')+1] for j in jobs],['1,16','1,4,16,64'])

    def test_zero_memory_probe_is_rejected_before_sdk_loading(self):
        import subprocess
        result=subprocess.run([sys.executable,str(P/'ae/runners/cube_fanout_audit.py'),
            '--mem-mib','0','--cube-api-url','http://127.0.0.1:3000','--cube-template','fixture'],
            capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertIn('memory size and timeouts must be positive',result.stderr)

    def test_mysql_restore_errors_always_leave_guard(self):
        for fault in ('unlink','reload','identity'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);drop=root/'mysql.conf';guard=root/'guard.json';calls=[]
                original={'id':'original','effective_cpus':'0-95','effective_mems':'0-5'}
                def run(*args, **kw):
                    calls.append(args)
                    if fault=='reload' and args==('systemctl','daemon-reload') and len(calls)>1:
                        raise RuntimeError('reload failed')
                unlink=Path.unlink
                def remove(path, *a, **kw):
                    if path==drop and fault=='unlink':raise RuntimeError('unlink failed')
                    return unlink(path,*a,**kw)
                inspect_count=[0]
                def inspect(name):
                    inspect_count[0]+=1
                    if fault=='identity' and inspect_count[0]>1:raise RuntimeError('identity unavailable')
                    return original
                with patch.object(m,'MYSQL_DROP',drop),patch.object(m,'inspect',side_effect=inspect), \
                     patch.object(m,'run',side_effect=run),patch.object(Path,'unlink',remove), \
                     patch.object(m,'mysql_volume_removal_disabled'), \
                     patch.object(m,'service_cgroup_masks',return_value={'path':'/fixture'}), \
                     patch.object(m,'restore_service_cgroup_masks'), \
                     patch.object(m,'output',side_effect=lambda *a:'simple' if 'Type' in a else 'fixed'):
                    with self.assertRaises(RuntimeError):
                        with m.mysql_launcher(root,guard):pass
                self.assertTrue(guard.exists())
                self.assertFalse(any(a[:2]==('systemctl','start') for a in calls))

    def test_placement_restore_failure_keeps_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);guard=root/'guard.json';read=Path.read_text;write=Path.write_text
            def read_text(path,*a,**kw):
                if str(path).startswith('/sys/fs/cgroup/cube_sandbox/'):return '0'
                return read(path,*a,**kw)
            def write_text(path,*a,**kw):
                if str(path).startswith('/sys/fs/cgroup/cube_sandbox/'):raise RuntimeError('placement restore failed')
                return write(path,*a,**kw)
            with patch.object(m,'UNITS',[]),patch.object(m,'CONTAINERS',[]), patch.object(m,'run'), \
                 patch.object(Path,'read_text',read_text),patch.object(Path,'write_text',write_text):
                with self.assertRaisesRegex(RuntimeError,'placement restoration failed'):
                    with m.placement(0,'0-3',root,guard):pass
            self.assertTrue(guard.exists())

if __name__ == '__main__':
    unittest.main(verbosity=2)
