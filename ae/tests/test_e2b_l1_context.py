"""Fake-only lifecycle/resource regression tests: never launch QEMU or SSH."""
import base64
from contextlib import ExitStack
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/e2b_l1_context.py'
spec = importlib.util.spec_from_file_location('e2b_l1_context_under_test', SOURCE)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


class AssetsFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.workspace = self.work / 'instances'
        self.workspace.mkdir()
        self.share = self.work / 'shares/assets'
        self.share.mkdir(parents=True)
        (self.share / 'asset').write_bytes(b'fixed input')
        self.base = self.work / 'base.qcow2'
        self.base.write_bytes(b'frozen prepared image')
        self.base.chmod(0o444)
        self.tools = {}
        for name in ('qemu', 'qemu_img', 'genisoimage'):
            p = self.work / name
            p.write_bytes(('tool-'+name).encode())
            p.chmod(0o755)
            self.tools[name] = p
        self.public = self.work / 'id.pub'
        alg = b'ssh-ed25519'
        blob = len(alg).to_bytes(4, 'big') + alg + (32).to_bytes(4, 'big') + b'x'*32
        self.public.write_text('ssh-ed25519 '+base64.b64encode(blob).decode()+' fixture\n')
        self.private = self.work / 'id'
        self.private.write_text('PRIVATE-BYTES-MUST-NEVER-BE-READ')
        self.private.chmod(0o600)
        self.manifest = self.work / 'manifest.json'
        def asset(p): return {'path': str(p), 'sha256': m.digest(p)}
        self.value = {'schema_version':1, 'kind':'e2b-paper-l1-assets', 'disk_size_gib':128,
                      'expected_l1_kernel':'6.8.0-117-generic', 'base_image':asset(self.base),
                      'tools':{k:asset(v) for k,v in self.tools.items()}, 'ssh_port':56556,
                      'shares':[{'path':str(self.share), 'tag':'ae_assets',
                                 'files':[{'path':'asset', 'sha256':m.digest(self.share/'asset')}]}]}
        self.write_manifest()
        self.config = m.L1Config(self.manifest, self.workspace, self.public, self.private)
        self.work_patch = patch.object(m, 'WORK', self.work)
        self.work_patch.start()
        self.addCleanup(self.work_patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.value))

    def mutate(self, fn):
        fn(self.value)
        self.write_manifest()
        with self.assertRaises((ValueError, TypeError)):
            m.read_manifest(self.config)

class Manifest(AssetsFixture):

    def test_arbitrary_sizes_denied(self):
        for val in (64, 256, True, '128'):
            with self.subTest(val=val):
                self.mutate(lambda d: d.update(disk_size_gib=val))

    def test_bool_schema_denied(self):
        self.mutate(lambda d: d.update(schema_version=True))

    def test_unknown_command_field_denied(self):
        self.mutate(lambda d: d.update(command='sh -c anything'))

    def test_wrong_hash_denied(self):
        self.mutate(lambda d: d['base_image'].update(sha256='0'*64))

    def test_symlink_asset_denied(self):
        link = self.work / 'link.qcow2'
        link.symlink_to(self.base)
        self.mutate(lambda d: d['base_image'].update(path=str(link)))

    def test_manifest_outside_fixed_root_denied(self):
        p = self.root / 'outside.json'
        p.write_text(self.manifest.read_text())
        with self.assertRaises(ValueError):
            m.read_manifest(replace(self.config, manifest=p))

    def test_broad_share_denied(self):
        self.mutate(lambda d: d['shares'][0].update(path=str(self.work)))

    def test_extra_share_file_denied(self):
        (self.share / 'extra').write_text('unreviewed')
        with self.assertRaises(ValueError):
            m.read_manifest(self.config)

    def test_share_symlink_denied(self):
        (self.share / 'escape').symlink_to(self.root)
        with self.assertRaises(ValueError):
            m.read_manifest(self.config)

    def test_share_traversal_denied(self):
        self.mutate(lambda d: d['shares'][0]['files'][0].update(path='../asset'))

    def test_duplicate_share_tag_denied(self):
        self.value['shares'].append(dict(self.value['shares'][0]))
        self.write_manifest()
        with self.assertRaises(ValueError):
            m.read_manifest(self.config)

    def test_private_permissions_denied(self):
        self.private.chmod(0o644)
        with self.assertRaises(ValueError):
            m.read_manifest(self.config)

class FakeProcess:
    def __init__(self):
        self.pid = 123456
        self.returncode = None
        self.waits = []
        self.term_timeout = False
        self.signals = []

    def poll(self): return self.returncode
    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.returncode is None and self.term_timeout:
            raise subprocess.TimeoutExpired('fake-qemu', timeout)
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def send(self, fd, sig):
        self.signals.append(sig)
        if sig == signal.SIGKILL or not self.term_timeout:
            self.returncode = -sig


class Lifecycle(AssetsFixture):
    def fake_runtime(self, *, ssh=None, popen_error=None):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.process = FakeProcess()
        self.commands = []
        self.popen_commands = []
        self.identity = {'pid':self.process.pid, 'ppid':os.getpid(), 'starttime':111,
                         'cgroup':'/owned-test', 'exe':str(self.tools['qemu'])}
        self.proof = {'cgroup':'/owned-test', 'lease_owner':{'pid':999}, 'zero_swap_limits':['fake']}
        self.guest = {'kernel':'6.8.0-117-generic', 'cpus':4, 'memory_kib':16*1024**2-1024,
                      'swap_kib':0, 'kvm':True}
        def run(args, **kwargs):
            self.commands.append((args, kwargs))
            if args[0] == str(self.tools['qemu_img']):
                if args[1] == 'info':
                    return subprocess.CompletedProcess(args, 0, json.dumps(
                        {'format':'qcow2', 'virtual-size':16*m.GIB}))
                if args[1] == 'create':
                    Path(args[-2]).write_bytes(b'private-overlay')
                    return subprocess.CompletedProcess(args, 0, '')
            if args[0] == str(self.tools['genisoimage']):
                Path(args[args.index('-output')+1]).write_bytes(b'seed')
                return subprocess.CompletedProcess(args, 0, '')
            if args[0] == '/usr/bin/ssh':
                if ssh:
                    return ssh(args, **kwargs)
                return subprocess.CompletedProcess(args, 0, json.dumps(self.guest))
            raise AssertionError('Unexpected subprocess: '+repr(args))
        def popen(args, **kwargs):
            self.popen_commands.append((args, kwargs))
            if popen_error:
                raise popen_error
            return self.process
        self.resource_call = stack.enter_context(patch.object(m, 'resources', return_value=self.proof))
        stack.enter_context(patch.object(m, 'capacity', return_value={'available_bytes':180*m.GIB}))
        stack.enter_context(patch.object(m, 'memory_admission', return_value={'available_estimate_bytes':30*m.GIB}))
        stack.enter_context(patch.object(m.subprocess, 'run', side_effect=run))
        stack.enter_context(patch.object(m.subprocess, 'Popen', side_effect=popen))
        self.identity_call = stack.enter_context(patch.object(m, 'proc_identity', side_effect=lambda pid: dict(self.identity)))
        stack.enter_context(patch.object(m.os, 'pidfd_open', return_value=77))
        stack.enter_context(patch.object(m.os, 'close'))
        stack.enter_context(patch.object(m.signal, 'getsignal', return_value=signal.SIG_DFL))
        self.signal_handlers = {}
        stack.enter_context(patch.object(m.signal, 'signal',
            side_effect=lambda sig, fn: self.signal_handlers.update({sig:fn})))
        stack.enter_context(patch.object(m.signal, 'pidfd_send_signal', side_effect=self.process.send))
        stack.enter_context(patch.object(m, 'owned_listener', return_value=True))
        stack.enter_context(patch.object(m.os, 'sched_getaffinity', return_value=m.CPUS))
        original_iterdir = Path.iterdir
        original_read = Path.read_text
        def iterdir(path):
            if str(path) == '/proc/123456/task':
                return iter([Path('/proc/123456/task/123456'), Path('/proc/123456/task/123457')])
            return original_iterdir(path)
        def read(path, *a, **kw):
            if str(path) == '/proc/123456/status':
                return 'Name: qemu\nVmSwap:\t0 kB\n'
            if str(path) == '/proc/123456/numa_maps':
                return '001 bind:1 anon=20 dirty=20 N1=20 kernelpagesize_kB=4\n'
            return original_read(path, *a, **kw)
        stack.enter_context(patch.object(Path, 'iterdir', iterdir))
        stack.enter_context(patch.object(Path, 'read_text', read))
        return stack

    def last_state(self):
        files = list(self.workspace.glob('l1-*/lifecycle.json'))
        self.assertEqual(len(files), 1)
        return json.loads(files[0].read_text())

    def test_unbounded_timeouts_denied_before_subprocess(self):
        self.fake_runtime()
        for kw in ({'readiness_timeout':0},{'readiness_timeout':601},{'stop_grace':61}):
            with self.assertRaises(ValueError):
                with m.owned_l1(replace(self.config, **kw)):
                    self.fail('no yield')
        self.assertEqual(self.commands, [])

    def poweroff_runtime(self, behavior='clean'):
        def ssh(args, **kwargs):
            if args[-1] == m.READINESS:
                return subprocess.CompletedProcess(args,0,json.dumps(self.guest))
            self.assertEqual(args[-1],m.SHUTDOWN)
            if behavior=='clean': self.process.returncode=0
            elif behavior=='badexit': self.process.returncode=9
            elif behavior=='identity': self.identity['starttime']+=1
            elif behavior=='commandfail': return subprocess.CompletedProcess(args,1,'','sudo failed')
            return subprocess.CompletedProcess(args,255,'','connection closed')
        self.fake_runtime(ssh=ssh)

class Resources(unittest.TestCase):
    def test_cpu_mismatch_denied_before_commands(self):
        with patch.object(m.os,'geteuid',return_value=0), \
             patch.object(m.os,'sched_getaffinity',return_value={0,1,2,3}), \
             patch.object(m.subprocess,'run') as run:
            with self.assertRaises(ValueError): m.resources()
            run.assert_not_called()

    def test_actual_mempolicy_required_not_allowed_mems(self):
        for mode in ('preferred','default'):
            with self.subTest(mode=mode), patch.object(m.os,'geteuid',return_value=0), \
                 patch.object(m.os,'sched_getaffinity',return_value=m.CPUS), \
                 patch.object(m,'held_ancestor_leases',return_value={}), \
                 patch.object(m.subprocess,'run',return_value=types.SimpleNamespace(stdout='policy: '+mode+'\nmembind: 1\n')):
                with self.assertRaisesRegex(ValueError,'actual NUMA1'): m.resources()

    def test_capacity_accounts_remaining_growth_and_reserve(self):
        fake = types.SimpleNamespace(st_dev=8)
        with patch.object(Path,'stat',return_value=fake), patch.object(m,'growth_budget',return_value=None), \
             patch.object(m.os,'statvfs',return_value=types.SimpleNamespace(f_bavail=137, f_frsize=m.GIB)):
            with self.assertRaises(ValueError): m.capacity(Path('/workspace'),128)
            result = m.capacity(Path('/workspace'),128,allocated=2*m.GIB)
            self.assertEqual(result['required_bytes'],136*m.GIB)

    def test_capacity_growth_budget_caps_reservation(self):
        fake = types.SimpleNamespace(st_dev=8)
        with patch.object(Path,'stat',return_value=fake), patch.object(m,'growth_budget',return_value=40), \
             patch.object(m.os,'statvfs',return_value=types.SimpleNamespace(f_bavail=60, f_frsize=m.GIB)):
            result = m.capacity(Path('/workspace'),156,allocated=2*m.GIB)
            self.assertEqual(result['required_bytes'],48*m.GIB)
            self.assertEqual(result['growth_budget_gib'],40)

    def test_growth_budget_requires_root_owned_file(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder)/'growth-budget.json').write_text('{"growth_gib": 40}')
            if os.geteuid() == 0:
                self.assertEqual(m.growth_budget(Path(folder)/'l1-x'),40)
            else:
                with self.assertRaisesRegex(ValueError,'root-owned'): m.growth_budget(Path(folder)/'l1-x')
            self.assertIsNone(m.growth_budget(Path(folder).parent/'absent-workspace'))

    def test_growth_budget_rejects_invalid_values_and_unsafe_files(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            target = folder / 'growth-budget.json'
            for value in (True, 0, -1, '40', None):
                target.write_text(json.dumps({'growth_gib': value}))
                metadata = types.SimpleNamespace(st_mode=0o100644, st_uid=0)
                with self.subTest(value=value), patch.object(Path, 'lstat', return_value=metadata):
                    with self.assertRaisesRegex(ValueError, 'Invalid'):
                        m.growth_budget(folder)
            target.write_text('{"growth_gib": 40}')
            for mode, uid in ((0o100666, 0), (0o100664, 0), (0o100644, 1234), (0o120777, 0)):
                metadata = types.SimpleNamespace(st_mode=mode, st_uid=uid)
                with self.subTest(mode=mode, uid=uid), patch.object(Path, 'lstat', return_value=metadata):
                    with self.assertRaisesRegex(ValueError, 'root-owned'):
                        m.growth_budget(folder)

    def test_growth_budget_cannot_remove_reserve_or_reserve_beyond_full_size(self):
        with patch.object(Path, 'stat', return_value=types.SimpleNamespace(st_dev=8)), \
             patch.object(m, 'growth_budget', return_value=256), \
             patch.object(m.os, 'statvfs', return_value=types.SimpleNamespace(f_bavail=138, f_frsize=m.GIB)):
            self.assertEqual(m.capacity(Path('/workspace'), 128)['required_bytes'], 138 * m.GIB)
            self.assertEqual(m.capacity(Path('/workspace'), 128, allocated=200 * m.GIB)['required_bytes'], 10 * m.GIB)
        with patch.object(Path, 'stat', return_value=types.SimpleNamespace(st_dev=8)), \
             patch.object(m, 'growth_budget', return_value=40), \
             patch.object(m.os, 'statvfs', return_value=types.SimpleNamespace(f_bavail=9, f_frsize=m.GIB)):
            with self.assertRaisesRegex(ValueError, 'reserve'):
                m.capacity(Path('/workspace'), 128, allocated=40 * m.GIB)

    def test_disk2_nvme_device_accepted(self):
        def stat(path):
            return types.SimpleNamespace(st_dev={'/mnt/disk1': 1, '/mnt/disk2': 3}.get(str(path), 3))
        with patch.object(Path,'stat',stat), patch.object(m,'growth_budget',return_value=None), \
             patch.object(m.os,'statvfs',return_value=types.SimpleNamespace(f_bavail=200, f_frsize=m.GIB)):
            self.assertEqual(m.capacity(Path('/workspace'),156)['required_bytes'],166*m.GIB)

    def test_wrong_disk_device_denied(self):
        def stat(path):
            return types.SimpleNamespace(st_dev={'/mnt/disk1': 1, '/mnt/disk2': 3}.get(str(path), 2))
        with patch.object(Path,'stat',stat):
            with self.assertRaisesRegex(ValueError,'disk1'): m.capacity(Path('/workspace'),80)

    def test_noswap_rejects_no_effective_zero_limit(self):
        with patch.object(m,'proc_identity',return_value={'cgroup':'/test/child'}), \
             patch.object(Path,'exists',return_value=True), \
             patch.object(Path,'read_text',return_value='max\n'):
            with self.assertRaisesRegex(ValueError,'MemorySwapMax'): m.cgroup_noswap()

    def test_inherited_zero_swap_limit_accepted(self):
        def read(path): return '0\n' if str(path)=='/sys/fs/cgroup/test/memory.swap.max' else 'max\n'
        with patch.object(m,'proc_identity',return_value={'cgroup':'/test/child'}), \
             patch.object(Path,'exists',return_value=True), patch.object(Path,'read_text',read):
            self.assertEqual(m.cgroup_noswap()['zero_swap_limits'],['/sys/fs/cgroup/test/memory.swap.max'])

    def lease_fixture(self, *, foreign=False, different_owner=False, wrong_cmd=False):
        stack=ExitStack()
        self.addCleanup(stack.close)
        names=['deltabox-numa-1.lock',*['deltabox-policy%d.lock'%c for c in sorted(m.CPUS)]]
        inodes={name:10+i for i,name in enumerate(names)}
        owner=999
        lines=['%d: FLOCK ADVISORY WRITE %d 00:08:%d 0 EOF'%(i+1,
               2000 if foreign or (different_owner and i==1) else owner,inodes[name])
               for i,name in enumerate(names)]
        original_stat=Path.stat
        original_read=Path.read_text
        original_bytes=Path.read_bytes
        def stat(path,*a,**kw):
            if path.name in inodes: return types.SimpleNamespace(st_dev=os.makedev(0,8),st_ino=inodes[path.name])
            return original_stat(path,*a,**kw)
        def read(path,*a,**kw):
            if str(path)=='/proc/locks':return '\n'.join(lines)
            return original_read(path,*a,**kw)
        def readbytes(path,*a,**kw):
            if str(path)=='/proc/999/cmdline':
                return b'python3\0'+ (b'/unrelated.py' if wrong_cmd else str(m.REPO/'ae/scripts/run_pinned_measurement.py').encode()) + b'\0'
            return original_bytes(path,*a,**kw)
        stack.enter_context(patch.object(m,'ancestors',return_value={999:{'pid':999},2000:{'pid':2000}} if different_owner else {999:{'pid':999}}))
        stack.enter_context(patch.object(m,'trusted',side_effect=lambda p: p))
        stack.enter_context(patch.object(Path,'stat',stat))
        stack.enter_context(patch.object(Path,'read_text',read))
        stack.enter_context(patch.object(Path,'read_bytes',readbytes))

    def test_existing_ancestor_owns_all_five_leases(self):
        self.lease_fixture()
        self.assertEqual(m.held_ancestor_leases(),{'pid':999})

    def test_nonancestor_lease_owner_denied(self):
        self.lease_fixture(foreign=True)
        with self.assertRaisesRegex(ValueError,'live ancestor'):m.held_ancestor_leases()

    def test_separate_lease_owners_denied(self):
        self.lease_fixture(different_owner=True)
        with self.assertRaisesRegex(ValueError,'different owners'):m.held_ancestor_leases()

    def test_wrong_ancestor_command_denied(self):
        self.lease_fixture(wrong_cmd=True)
        with self.assertRaisesRegex(ValueError,'pinned measurement controller'):m.held_ancestor_leases()



class LifecycleWriter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def test_symlink_folder_rejected_without_writing(self):
        target = self.folder/'target';target.mkdir()
        alias = self.folder/'alias';alias.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            m.write_lifecycle(alias, {'status':'ready'})
        self.assertEqual(list(target.iterdir()), [])

if __name__ == '__main__':
    unittest.main()
