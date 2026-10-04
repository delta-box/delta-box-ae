"""Exercise inherited-mask recovery with fake systemd and kernel-like temp files."""
import ast
from contextlib import contextmanager
import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[2] / 'ae/scripts/cube_control_context.py'
MYSQL_UNIT = 'cube-sandbox-mysql.service'
MYSQL = 'cube-sandbox-mysql'
FRONT = ['cube-sandbox-cube-api.service', 'cube-sandbox-cubemaster.service']
FIELDS = ('cpuset.cpus', 'cpuset.mems', 'cpuset.cpus.effective', 'cpuset.mems.effective')


def functions():
    tree = ast.parse(SOURCE.read_text())
    names = {'save', 'restore_masks', 'service_cgroup_masks', 'restore_service_cgroup_masks',
             'mysql_inherited_masks_need_rebuild', 'restore_mysql_inherited_masks',
             'mysql_volume_removal_disabled', 'mysql_preserve_launcher',
             'verify_mysql_container', 'service_start', 'placement', 'mysql_launcher', 'inspect',
             'canonical_independent_binds'}
    tree.body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = types.ModuleType('cube_mysql_fixture')
    module.__dict__.update(Path=Path, contextmanager=contextmanager, json=json, os=os,
        shlex=shlex, hashlib=hashlib, subprocess=subprocess, time=time, MYSQL=MYSQL,
        MYSQL_UNIT=MYSQL_UNIT, FRONT=FRONT, WEBUI='cube-sandbox-webui.service',
        UNITS=[MYSQL_UNIT], CONTAINERS=[MYSQL],
        UNIT_PROPERTIES=('AllowedCPUs', 'AllowedMemoryNodes', 'CPUAffinity', 'NUMAPolicy', 'NUMAMask'))
    exec(compile(tree, str(SOURCE), 'exec'), module.__dict__)
    return module


class SystemdFixture:
    def __init__(self, root, control):
        self.root, self.control = root, control
        self.cg = root/'cgroup/system.slice'/MYSQL_UNIT
        self.cg.mkdir(parents=True)
        self.events, self.state, self.remove_on_stop = [], 'active', False
        self.stays_populated = self.bad_effective = self.changed_image = False
        self.properties = {key: '' for key in control.UNIT_PROPERTIES}
        self.container = {'id': 'a'*64, 'running': True, 'mounts': [{'Source': '/original/data', 'Destination': '/var/lib/mysql'}],
                          'image_id': 'sha256:original', 'config_sha256': 'original-config',
                          'cpus': '0-95', 'mems': '0-5', 'effective_cpus': '0-95', 'effective_mems': '0-5'}
        self.original = copy.deepcopy(self.container)
        self.real_read, self.real_write = Path.read_text, Path.write_text
        self.initialize_group('', '')
        self.expected = {'path': str(self.cg), 'cpuset.cpus': '', 'cpuset.mems': '',
                         'cpuset.cpus.effective': '0-95', 'cpuset.mems.effective': '0-5'}
        sandbox=root/'cgroup/cube_sandbox';sandbox.mkdir()
        (sandbox/'cpuset.cpus').write_text('52-55\n');(sandbox/'cpuset.mems').write_text('2\n')
        control.Path=self.path;control.run=self.run;control.output=self.output
        control.inspect=self.inspect;control.idle=lambda **kw:self.events.append(('idle',))
        control.variables=lambda:self.events.append(('database-ready',))
        control.MYSQL_DROP=root/'preserve.conf';control.MYSQL_DROP.write_text(control.mysql_preserve_launcher(self.original['id']))
        control.WEBUI_DROP=root/'webui.conf';control.WEBUI_DROP.write_text('[Service]\nRestart=no\n')
        control.MYSQL_ENV_FILE=root/'environment';control.MYSQL_ENV_FILE.write_text('UNRELATED_SECRET=do-not-export\n')
        # No live /proc lookup in unit tests. The dedicated parser has its own tests.
        self.volume_guard=mock.Mock()
        control.mysql_volume_removal_disabled=self.volume_guard

    def path(self, value):
        p=Path(value)
        for before, after in [('/sys/fs/cgroup', self.root/'cgroup'), ('/run/systemd/system', self.root/'systemd')]:
            if p == Path(before) or p.is_relative_to(before):return after/p.relative_to(before)
        return p

    def initialize_group(self, cpus, mems):
        self.cg.mkdir(parents=True, exist_ok=True)
        for name,value in [('cpuset.cpus',cpus),('cpuset.mems',mems),('cgroup.events','populated 1\nfrozen 0')]:
            self.real_write(self.cg/name,value+'\n')
        for name in FIELDS[2:]:self.real_write(self.cg/name,'unused\n')

    def read(self, path, *args, **kwargs):
        if path.parent == self.cg and path.name.endswith('.effective'):
            raw=self.real_read(path.with_name(path.name.removesuffix('.effective'))).strip()
            if self.bad_effective and self.state == 'active' and not raw:return '0\n'
            self.events.append(('read-effective',path.name))
            return (raw or ('0-95' if path.name == 'cpuset.cpus.effective' else '0-5'))+'\n'
        return self.real_read(path,*args,**kwargs)

    def write(self, path, value, *args, **kwargs):
        if path.parent == self.cg and path.name in ('cpuset.cpus','cpuset.mems'):
            old=self.real_read(path).strip()
            if old and not value.strip() and 'populated 1' in self.real_read(self.cg/'cgroup.events'):
                raise OSError(errno.ENOSPC, 'populated cpuset cannot become empty')
            self.events.append(('write-mask',path.name,value.strip()))
        return self.real_write(path,value,*args,**kwargs)

    def output(self, *cmd):
        if cmd[:2] == ('systemctl','is-active'):return 'active'
        if cmd[:2] != ('systemctl','show'):raise AssertionError(cmd)
        unit=cmd[2];key=cmd[cmd.index('-p')+1]
        if unit == self.control.WEBUI:return 'inactive'
        if key == 'ControlGroup':return '/system.slice/'+unit
        if key == 'ActiveState':return self.state
        if key in ('MainPID','ControlPID'):return '1' if key=='MainPID' and self.state=='active' else '0'
        return self.properties[key]

    def run(self, *cmd, **kwargs):
        self.events.append(cmd)
        if cmd[:2] == ('systemctl','stop') and cmd[2:] == (MYSQL_UNIT,):
            self.state='inactive'
            if self.remove_on_stop:shutil.rmtree(self.cg)
            else:self.real_write(self.cg/'cgroup.events','populated '+('1' if self.stays_populated else '0')+'\n')
        elif cmd[:2] == ('systemctl','start') and cmd[2:] == (MYSQL_UNIT,):
            self.state='active'
            if not self.cg.exists():self.initialize_group('', '')
            self.real_write(self.cg/'cgroup.events','populated 1\n')
            if self.changed_image:self.container['image_id']='sha256:changed'
        elif cmd[:2] == ('systemctl','set-property'):
            for arg in cmd[4:]:
                key,value=arg.split('=',1);self.properties[key]=value
                if value:(self.cg/({'AllowedCPUs':'cpuset.cpus','AllowedMemoryNodes':'cpuset.mems'}[key])).write_text(value+'\n')
        elif cmd[:2] == ('docker','update'):
            self.container['cpus']=self.container['effective_cpus']=cmd[cmd.index('--cpuset-cpus')+1]
            self.container['mems']=self.container['effective_mems']=cmd[cmd.index('--cpuset-mems')+1]

    def inspect(self, name):
        self.events.append(('inspect',name))
        return copy.deepcopy(self.container)


class InheritedMaskTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.control=functions();self.fixture=SystemdFixture(self.root,self.control)
        self.patchers=[mock.patch.object(Path,'read_text',lambda p,*a,**k:self.fixture.read(p,*a,**k)),
                       mock.patch.object(Path,'write_text',lambda p,v,*a,**k:self.fixture.write(p,v,*a,**k))]
        for p in self.patchers:p.start();self.addCleanup(p.stop)

    def pinned(self):
        (self.fixture.cg/'cpuset.cpus').write_text('0-3\n');(self.fixture.cg/'cpuset.mems').write_text('0\n')

    def recover(self):
        return self.control.restore_mysql_inherited_masks(self.fixture.expected,self.fixture.original,self.root/'evidence')

    def test_populated_kernel_refuses_empty_then_rebuild_restores_exact_inheritance(self):
        self.pinned()
        with self.assertRaises(OSError) as failure:self.control.restore_service_cgroup_masks(MYSQL_UNIT,self.fixture.expected)
        self.assertEqual(failure.exception.errno,errno.ENOSPC)
        self.fixture.events.clear();self.assertEqual(self.recover(),self.fixture.expected)
        events=self.fixture.events
        stop_front=events.index(('systemctl','stop',*FRONT));stop_mysql=events.index(('systemctl','stop',MYSQL_UNIT))
        clear=events.index(('write-mask','cpuset.mems',''));start=events.index(('systemctl','start',MYSQL_UNIT))
        expose=events.index(('systemctl','start',*reversed(FRONT)))
        self.assertLess(stop_front,stop_mysql);self.assertLess(stop_mysql,clear);self.assertLess(clear,start)
        self.assertTrue(any(e[0]=='read-effective' for e in events[start+1:expose]))
        self.assertTrue(any(e[0]=='inspect' for e in events[start+1:expose]))
        self.assertEqual(self.control.service_cgroup_masks(MYSQL_UNIT),self.fixture.expected)

    def test_removed_cgroup_is_recreated_with_empty_raw_masks(self):
        self.pinned();self.fixture.remove_on_stop=True
        self.assertEqual(self.recover(),self.fixture.expected)

    def test_nonempty_original_masks_do_not_enter_restart_branch(self):
        self.pinned();original=dict(self.fixture.expected,**{'cpuset.mems':'2'})
        self.assertFalse(self.control.mysql_inherited_masks_need_rebuild(original))
        with self.assertRaisesRegex(RuntimeError,'not the populated inherited-mask'):
            self.control.restore_mysql_inherited_masks(original,self.fixture.original,self.root)
        self.assertFalse(any(e[:2]==('systemctl','stop') for e in self.fixture.events))

    def test_wrong_launcher_or_nonquiesced_ui_refuses_before_stopping(self):
        self.pinned()
        self.control.MYSQL_DROP.write_text('wrong container')
        with self.assertRaisesRegex(RuntimeError,'preserve-container'):self.recover()
        self.control.MYSQL_DROP.write_text(self.control.mysql_preserve_launcher(self.fixture.original['id']))
        self.control.WEBUI_DROP.unlink()
        with self.assertRaisesRegex(RuntimeError,'quiesced'):self.recover()
        self.assertFalse(any(e[:2]==('systemctl','stop') for e in self.fixture.events))

    def test_tasks_that_survive_stop_keep_front_down_and_masks_unchanged(self):
        self.pinned();self.fixture.stays_populated=True
        with self.assertRaisesRegex(RuntimeError,'remained populated'):self.recover()
        self.assertEqual((self.fixture.cg/'cpuset.mems').read_text().strip(),'0')
        self.assertNotIn(('systemctl','start',*reversed(FRONT)),self.fixture.events)

    def test_changed_container_or_effective_masks_never_exposes_front(self):
        for flag,message in [('changed_image','image, configuration'),('bad_effective','raw and effective')]:
            with self.subTest(flag=flag):
                self.fixture.container=copy.deepcopy(self.fixture.original);self.fixture.initialize_group('0-3','0')
                self.fixture.state='active';self.fixture.events.clear();setattr(self.fixture,flag,True)
                with self.assertRaisesRegex(RuntimeError,message):self.recover()
                self.assertNotIn(('systemctl','start',*reversed(FRONT)),self.fixture.events)
                setattr(self.fixture,flag,False)

    def test_placement_records_restore_failure_and_keeps_guard(self):
        self.fixture.stays_populated=True
        out=self.root/'placement';guard=out/'RECOVERY_REQUIRED.json'
        with self.assertRaisesRegex(RuntimeError,'placement restoration failed'):
            with self.control.placement(0,'0-3',out,guard):pass
        self.assertTrue(guard.exists())
        errors=json.loads((out/'placement-restored.json').read_text())['errors']
        self.assertTrue(any('remained populated' in error for error in errors))

    def test_front_policy_restores_before_mysql_rebuild_can_restart_front(self):
        self.control.UNITS=[*FRONT,MYSQL_UNIT]
        capture=self.control.service_cgroup_masks;restore=self.control.restore_service_cgroup_masks
        run=self.control.run;rebuild=self.control.restore_mysql_inherited_masks
        self.control.service_cgroup_masks=lambda unit: (capture(unit) if unit==MYSQL_UNIT else
            dict(self.fixture.expected,path='/original/'+unit))
        self.control.restore_service_cgroup_masks=lambda unit,expected: (restore(unit,expected) if unit==MYSQL_UNIT else expected)
        def command(*cmd,**kwargs):
            if cmd[:2]==('systemctl','set-property') and cmd[3] in FRONT:
                self.fixture.events.append(cmd)
            else:run(*cmd,**kwargs)
        self.control.run=command
        def deferred(expected,original,out):
            for unit in FRONT:
                self.assertIn(('systemctl','set-property','--runtime',unit,'AllowedCPUs=','AllowedMemoryNodes='),self.fixture.events)
            return rebuild(expected,original,out)
        self.control.restore_mysql_inherited_masks=deferred
        out=self.root/'ordered';guard=out/'RECOVERY_REQUIRED.json'
        with self.control.placement(0,'0-3',out,guard):pass
        self.assertFalse(guard.exists())

    def test_unrelated_unit_error_is_not_converted_to_mysql_restart(self):
        self.control.UNITS=['other.service'];self.control.CONTAINERS=[]
        # The shared fixture exposes the same path only for this failure injection.
        self.control.service_cgroup_masks=lambda unit:dict(self.fixture.expected)
        self.control.restore_service_cgroup_masks=mock.Mock(side_effect=OSError(errno.EIO,'unrelated failure'))
        out=self.root/'other';guard=out/'RECOVERY_REQUIRED.json'
        with self.assertRaisesRegex(RuntimeError,'unrelated failure'):
            with self.control.placement(0,'0-3',out,guard):pass
        self.assertTrue(guard.exists())
        self.assertNotIn(('systemctl','stop',MYSQL_UNIT),self.fixture.events)


class VolumeSwitchTests(unittest.TestCase):
    def test_only_literal_disabled_switch_is_allowed_without_executing_environment(self):
        control=functions();control.output=lambda *args:'0'
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ,{},clear=True):
            env=Path(d)/'environment';control.MYSQL_ENV_FILE=env
            for line in ['UNRELATED_SECRET=hidden\n','CUBE_SANDBOX_REMOVE_VOLUMES=0\n',"export CUBE_SANDBOX_REMOVE_VOLUMES='0' # disabled\n"]:
                env.write_text(line);control.mysql_volume_removal_disabled()
            for value in ['1','$(touch /not-executed)','${OTHER}','0; true']:
                env.write_text('CUBE_SANDBOX_REMOVE_VOLUMES='+value+'\n')
                with self.assertRaises(RuntimeError):control.mysql_volume_removal_disabled()
            env.write_text('')
            with mock.patch.dict(os.environ,{'CUBE_SANDBOX_REMOVE_VOLUMES':'1'}),self.assertRaises(RuntimeError):
                control.mysql_volume_removal_disabled()

    def test_live_supervisor_enabled_switch_is_rejected(self):
        control=functions();control.output=lambda *args:'42'
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ,{},clear=True):
            control.MYSQL_ENV_FILE=Path(d)/'environment';control.MYSQL_ENV_FILE.write_text('')
            with mock.patch.object(Path,'read_bytes',return_value=b'OTHER=private\0CUBE_SANDBOX_REMOVE_VOLUMES=1\0'):
                with self.assertRaisesRegex(RuntimeError,'volume removal must be disabled'):
                    control.mysql_volume_removal_disabled()


class ContainerContractTests(unittest.TestCase):
    def env_digest(self, env):
        control=functions()
        value={'Id':'a'*64,'Image':'sha256:original','Config':{'Env':env},
               'HostConfig':{'CpusetCpus':'0-95','CpusetMems':'0-5'},
               'State':{'Pid':0,'Running':False,'StartedAt':'before','FinishedAt':'before'},
               'Mounts':[]}
        control.output=lambda *args:json.dumps([value])
        return control.inspect(MYSQL)['config_sha256']

    def test_unique_env_order_is_equivalent_but_values_are_not(self):
        original=['MYSQL_DATABASE=db','MYSQL_USER=user','MYSQL_PASSWORD=fake=value',
                  'MYSQL_ROOT_PASSWORD=fake-root','PATH=/usr/bin']
        recreated=[original[i] for i in (2,3,0,1,4)]
        self.assertEqual(self.env_digest(original),self.env_digest(recreated))
        recreated[0]='MYSQL_PASSWORD=different'
        self.assertNotEqual(self.env_digest(original),self.env_digest(recreated))

    def test_duplicate_env_order_remains_significant(self):
        original=['MYSQL_USER=first','MYSQL_USER=last','PATH=/usr/bin']
        reordered=['MYSQL_USER=last','MYSQL_USER=first','PATH=/usr/bin']
        self.assertNotEqual(self.env_digest(original),self.env_digest(reordered))
        self.assertNotEqual(self.env_digest(original),self.env_digest([original[2],*original[:2]]))

    def test_invalid_env_entries_remain_order_sensitive(self):
        for entry in ('NO_EQUALS','=empty-name','BAD\0KEY=value',7):
            with self.subTest(entry=entry):
                self.assertNotEqual(self.env_digest([entry,'PATH=/usr/bin']),
                                    self.env_digest(['PATH=/usr/bin',entry]))

    def test_config_digest_ignores_generated_hostname_and_treatment_masks_only(self):
        control=functions()
        value={'Id':'a'*64,'Image':'sha256:original','Config':{'Hostname':'a'*12,'Image':'mysql:pinned'},
               'HostConfig':{'CpusetCpus':'0-95','CpusetMems':'0-5','Binds':['/data:/var/lib/mysql']},
               'State':{'Pid':0,'Running':False,'StartedAt':'before','FinishedAt':'before'},
               'Mounts':[{'Source':'/data','Destination':'/var/lib/mysql'}]}
        control.output=lambda *args:json.dumps([value])
        before=control.inspect(MYSQL)
        value['Id']='b'*64;value['Config']['Hostname']='b'*12
        value['HostConfig'].update(CpusetCpus='0-3',CpusetMems='0')
        self.assertEqual(control.inspect(MYSQL)['config_sha256'],before['config_sha256'])
        value['HostConfig']['Binds']=['/different:/var/lib/mysql']
        self.assertNotEqual(control.inspect(MYSQL)['config_sha256'],before['config_sha256'])

    def test_recreated_original_supervisor_requires_original_storage_contract(self):
        control=functions()
        original={'id':'old','running':True,'mounts':[{'Source':'/original'}],'image_id':'original-image',
                  'config_sha256':'original-config','effective_cpus':'0-95','effective_mems':'0-5'}
        after=dict(original,id='new');control.inspect=lambda name:after
        control.verify_mysql_container(original,same_id=False)
        with self.assertRaisesRegex(RuntimeError,'did not restore'):
            control.verify_mysql_container(original,same_id=True)
        after['mounts']=[{'Source':'/private-ram'}]
        with self.assertRaisesRegex(RuntimeError,'did not restore'):
            control.verify_mysql_container(original,same_id=False)


if __name__ == '__main__':
    unittest.main()
