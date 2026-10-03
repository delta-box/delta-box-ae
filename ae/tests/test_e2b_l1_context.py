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
    def test_valid_frozen_assets(self):
        value, image, tools, shares, key = m.read_manifest(self.config)
        self.assertEqual(image, self.base)
        self.assertEqual(value['disk_size_gib'], 128)
        self.assertTrue(key.startswith('ssh-ed25519 '))
        self.assertEqual(shares, [(self.share, 'ae_assets')])

    def test_reviewed_disk_capacities_are_allowed(self):
        for disk_gib in (80, 128, 156):
            with self.subTest(disk_gib=disk_gib):
                self.value['disk_size_gib'] = disk_gib
                self.write_manifest()
                self.assertEqual(m.read_manifest(self.config)[0]['disk_size_gib'], disk_gib)

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

    def test_writable_base_denied(self):
        self.base.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'read-only'):
            m.read_manifest(self.config)

    def test_symlink_asset_denied(self):
        link = self.work / 'link.qcow2'
        link.symlink_to(self.base)
        self.mutate(lambda d: d['base_image'].update(path=str(link)))

    def test_unowned_asset_denied(self):
        os.chown(self.base, 65534, 65534)
        with self.assertRaises(ValueError):
            m.read_manifest(self.config)

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

    def test_share_nested_root_denied(self):
        sub = self.share / 'nested'
        sub.mkdir()
        self.value['shares'].append({'path':str(sub), 'tag':'ae_nested', 'files':[]})
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'overlap'):
            m.read_manifest(self.config)

    def test_share_traversal_denied(self):
        self.mutate(lambda d: d['shares'][0]['files'][0].update(path='../asset'))

    def test_duplicate_share_tag_denied(self):
        self.value['shares'].append(dict(self.value['shares'][0]))
        self.write_manifest()
        with self.assertRaises(ValueError):
            m.read_manifest(self.config)

    def test_private_key_cannot_be_public_key(self):
        with self.assertRaisesRegex(ValueError, 'public SSH key'):
            m.read_manifest(replace(self.config, public_key=self.private))

    def test_public_key_wire_algorithm_mismatch_denied(self):
        self.public.write_text('ssh-rsa '+self.public.read_text().split()[1])
        with self.assertRaisesRegex(ValueError, 'Malformed'):
            m.read_manifest(self.config)

    def test_private_permissions_denied(self):
        self.private.chmod(0o644)
        with self.assertRaises(ValueError):
            m.read_manifest(self.config)

    def test_private_bytes_never_read_or_hashed(self):
        original_open = Path.open
        def guarded(path, *args, **kw):
            if path == self.private:
                raise AssertionError('Private key bytes were accessed')
            return original_open(path, *args, **kw)
        with patch.object(Path, 'open', guarded):
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

    def test_complete_lifecycle_fixed_qemu_no_lock_reacquisition(self):
        self.fake_runtime()
        with m.owned_l1(self.config) as vm:
            self.assertEqual(vm.state['status'], 'ready')
            self.assertEqual(vm.state['purpose'], 'measurement')
            vm.verify()
            data = (vm.folder/'user-data').read_text()
            self.assertIn(self.public.read_text().strip(), data)
            self.assertNotIn('PRIVATE-BYTES', data)
        args, kw = self.popen_commands[0]
        self.assertEqual(args[args.index('-smp')+1], '4')
        self.assertEqual(args[args.index('-m')+1], '16G')
        self.assertIn('q35,accel=kvm', args)
        self.assertIn('host', args)
        self.assertTrue(kw['start_new_session'])
        self.assertTrue(all('readonly=on' in args[i+1] for i,a in enumerate(args) if a=='-virtfs'))
        self.assertFalse(any('flock' in str(command) for command,_ in self.commands))
        self.assertGreaterEqual(self.resource_call.call_count, 4)
        self.assertEqual(self.process.signals, [signal.SIGTERM])
        self.assertEqual(self.last_state()['status'], 'completed')

    def test_hosted_umask_keeps_lifecycle_and_known_hosts_private_through_cleanup(self):
        self.fake_runtime()
        old = os.umask(0o002)
        try:
            with m.owned_l1(self.config) as vm:
                folder = vm.folder
                for name in ('lifecycle.json', 'known_hosts'):
                    self.assertEqual((folder/name).stat().st_mode & 0o777, 0o600)
                    self.assertEqual(m.trusted(folder/name), folder/name)
                (folder/'known_hosts').write_text('verified SSH host key fixture\n')
                vm.verify()
                self.assertEqual(m.trusted(vm.manifest_path), vm.manifest_path)
                self.assertEqual(vm.manifest_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual((folder/'lifecycle.json').stat().st_mode & 0o777, 0o600)
            self.assertEqual((folder/'known_hosts').stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads((folder/'lifecycle.json').read_text())['status'], 'completed')
            self.assertEqual(list(folder.glob('lifecycle.json.*.tmp')), [])
        finally:
            os.umask(old)

    def test_keyboard_interrupt_stops_reaps_preserves_overlay(self):
        self.fake_runtime()
        with self.assertRaises(KeyboardInterrupt):
            with m.owned_l1(self.config) as vm:
                raise KeyboardInterrupt('cancel')
        self.assertEqual(self.process.signals, [signal.SIGTERM])
        self.assertTrue(list(self.workspace.glob('l1-*/l1.qcow2')))
        self.assertEqual(self.last_state()['status'], 'failed')

    def test_body_exception_cleanup(self):
        self.fake_runtime()
        with self.assertRaisesRegex(ValueError, 'job failed'):
            with m.owned_l1(self.config):
                raise ValueError('job failed')
        self.assertEqual(self.process.signals, [signal.SIGTERM])

    def test_guest_mismatch_never_yields(self):
        self.fake_runtime()
        self.guest['kernel'] = 'wrong-kernel'
        with self.assertRaisesRegex(RuntimeError, 'Guest'):
            with m.owned_l1(self.config):
                self.fail('must not yield')
        self.assertEqual(self.process.signals, [signal.SIGTERM])

    def test_measurement_requires_kvm(self):
        self.fake_runtime()
        self.guest['kvm'] = False
        with self.assertRaises(RuntimeError):
            with m.owned_l1(self.config):
                self.fail('measurement must require KVM')
        self.assertEqual(self.process.signals, [signal.SIGTERM])

    def test_prepare_bootstrap_then_strict_kvm(self):
        self.fake_runtime()
        self.guest['kvm'] = False
        with m.prepare_l1(self.config) as vm:
            self.assertEqual(vm.state['status'], 'bootstrap-ready')
            self.assertEqual(vm.state['purpose'], 'preparation')
            self.guest['kvm'] = True
        self.assertEqual(self.last_state()['status'], 'completed')
        self.assertTrue(self.last_state()['guest']['kvm'])

    def test_prepare_cannot_finish_without_kvm(self):
        self.fake_runtime()
        self.guest['kvm'] = False
        with self.assertRaises(RuntimeError):
            with m.prepare_l1(self.config):
                pass
        self.assertEqual(self.last_state()['status'], 'failed')

    def test_changed_starttime_does_not_signal(self):
        self.fake_runtime()
        with self.assertRaisesRegex(RuntimeError, 'identity changed|Identity'):
            with m.owned_l1(self.config):
                self.identity['starttime'] += 1
        self.assertEqual(self.process.signals, [])
        self.assertIn('no signal', self.last_state()['cleanup_error'])

    def test_changed_cgroup_does_not_signal(self):
        self.fake_runtime()
        with self.assertRaises(RuntimeError):
            with m.owned_l1(self.config):
                self.identity['cgroup'] = '/foreign'
        self.assertEqual(self.process.signals, [])

    def test_exited_child_reaped_without_signal(self):
        self.fake_runtime()
        with self.assertRaises(RuntimeError):
            with m.owned_l1(self.config):
                self.process.returncode = 9
        self.assertEqual(self.process.signals, [])
        self.assertTrue(self.process.waits)

    def test_term_timeout_kills_only_owned_pidfd(self):
        self.fake_runtime()
        self.process.term_timeout = True
        with m.owned_l1(self.config):
            pass
        self.assertEqual(self.process.signals, [signal.SIGTERM, signal.SIGKILL])

    def test_identity_changed_during_term_timeout_no_kill(self):
        self.fake_runtime()
        self.process.term_timeout = True
        def wait(timeout=None):
            self.identity['starttime'] += 1
            raise subprocess.TimeoutExpired('qemu', timeout)
        with patch.object(self.process, 'wait', side_effect=wait):
            with self.assertRaisesRegex(RuntimeError, 'identity changed'):
                with m.owned_l1(self.config):
                    pass
        self.assertEqual(self.process.signals, [signal.SIGTERM])
        self.assertEqual(self.last_state()['status'], 'failed')

    def test_failed_spawn_preserves_files_without_signaling(self):
        self.fake_runtime(popen_error=OSError('exec failed'))
        with self.assertRaises(OSError):
            with m.owned_l1(self.config):
                self.fail('no yield')
        self.assertEqual(self.process.signals, [])
        self.assertTrue(list(self.workspace.glob('l1-*/l1.qcow2')))

    def test_proc_capture_failure_uses_kernel_child_proof(self):
        self.fake_runtime()
        self.identity_call.side_effect = OSError('proc temporarily inaccessible')
        with patch.object(m.os, 'waitid', return_value=None) as childproof:
            with self.assertRaises(OSError):
                with m.owned_l1(self.config):
                    self.fail('no yield')
        childproof.assert_called_once_with(os.P_PID, self.process.pid, os.WEXITED|os.WNOHANG|os.WNOWAIT)
        self.assertEqual(self.process.signals, [signal.SIGTERM])
        self.assertEqual(self.last_state()['startup_cleanup'], 'kernel-confirmed direct unreaped child')

    def test_pidfd_open_failure_still_reaps_exact_unreaped_child(self):
        self.fake_runtime()
        with patch.object(m.os, 'pidfd_open', side_effect=OSError('fd limit')), \
             patch.object(m.os, 'waitid', return_value=None), \
             patch.object(m.os, 'kill', side_effect=lambda pid,sig:self.process.send(None,sig)) as kill:
            with self.assertRaises(OSError):
                with m.owned_l1(self.config):
                    self.fail('no yield')
        kill.assert_called_once_with(self.process.pid, signal.SIGTERM)

    def test_kernel_denies_child_ownership_no_signal(self):
        self.fake_runtime()
        self.identity_call.side_effect = OSError('proc unavailable')
        with patch.object(m.os, 'waitid', side_effect=ChildProcessError('not our child')), \
             patch.object(m.os, 'kill') as kill:
            with self.assertRaisesRegex(OSError, 'proc unavailable'):
                with m.owned_l1(self.config):
                    self.fail('no yield')
        kill.assert_not_called()
        self.assertEqual(self.process.signals, [])

    def test_signal_during_launch_deferred_then_owned_cleanup(self):
        self.fake_runtime()
        def launch(args, **kw):
            self.signal_handlers[signal.SIGTERM](signal.SIGTERM, None)
            return self.process
        with patch.object(m.subprocess, 'Popen', side_effect=launch):
            with self.assertRaises(KeyboardInterrupt):
                with m.owned_l1(self.config):
                    self.fail('no yield after cancellation')
        self.assertEqual(self.process.signals, [signal.SIGTERM])

    def test_unbounded_timeouts_denied_before_subprocess(self):
        self.fake_runtime()
        for kw in ({'readiness_timeout':0},{'readiness_timeout':601},{'stop_grace':61}):
            with self.assertRaises(ValueError):
                with m.owned_l1(replace(self.config, **kw)):
                    self.fail('no yield')
        self.assertEqual(self.commands, [])

    def test_swap_guest_denied(self):
        self.fake_runtime()
        self.guest['swap_kib'] = 1024
        with self.assertRaises(RuntimeError):
            with m.owned_l1(self.config):
                self.fail('no yield')

    def test_frozen_asset_mutation_aborts_and_cleans_owned_vm(self):
        self.fake_runtime()
        with self.assertRaisesRegex(RuntimeError, 'Frozen asset'):
            with m.owned_l1(self.config):
                (self.share/'asset').write_text('mutated')
        self.assertEqual(self.process.signals, [signal.SIGTERM])

    def test_nondefault_sigchld_denied(self):
        self.fake_runtime()
        with patch.object(m.signal, 'getsignal', return_value=signal.SIG_IGN):
            with self.assertRaisesRegex(RuntimeError, 'SIGCHLD'):
                with m.owned_l1(self.config):
                    self.fail('no yield')
        self.assertEqual(self.popen_commands, [])

    def test_ssh_timeout_is_bounded_and_reaps(self):
        self.fake_runtime(ssh=lambda args,**kw:subprocess.CompletedProcess(args,255,'','connect failed'))
        counter = iter(range(0,20))
        with patch.object(m.time, 'monotonic', side_effect=lambda:next(counter)), \
             patch.object(m.time, 'sleep'):
            with self.assertRaises(TimeoutError):
                with m.owned_l1(replace(self.config, readiness_timeout=5)):
                    self.fail('no yield')
        self.assertEqual(self.process.signals, [signal.SIGTERM])

    def test_actual_qemu_swap_denied(self):
        self.fake_runtime()
        original = Path.read_text
        def read(path,*a,**kw):
            if str(path)=='/proc/123456/status': return 'VmSwap:\t4 kB\n'
            return original(path,*a,**kw)
        with patch.object(Path,'read_text',read):
            with self.assertRaisesRegex(RuntimeError,'swap'):
                with m.owned_l1(self.config): self.fail('no yield')
        self.assertEqual(self.process.signals,[signal.SIGTERM])

    def test_qemu_pages_on_other_node_denied(self):
        self.fake_runtime()
        original = Path.read_text
        def read(path,*a,**kw):
            if str(path)=='/proc/123456/numa_maps': return '001 bind:1 anon=20 N0=1 N1=19\n'
            return original(path,*a,**kw)
        with patch.object(Path,'read_text',read):
            with self.assertRaisesRegex(RuntimeError,'NUMA1'):
                with m.owned_l1(self.config): self.fail('no yield')
        self.assertEqual(self.process.signals,[signal.SIGTERM])

    def test_missing_qemu_numa_evidence_denied(self):
        self.fake_runtime()
        original = Path.read_text
        def read(path,*a,**kw):
            if str(path)=='/proc/123456/numa_maps': return ''
            return original(path,*a,**kw)
        with patch.object(Path,'read_text',read):
            with self.assertRaisesRegex(RuntimeError,'positive NUMA1'):
                with m.owned_l1(self.config): self.fail('no yield')

    def test_qemu_thread_affinity_denied(self):
        self.fake_runtime()
        with patch.object(m.os,'sched_getaffinity',return_value={0,1}):
            with self.assertRaisesRegex(RuntimeError,'affinity'):
                with m.owned_l1(self.config): self.fail('no yield')

    def test_initial_wrong_parent_does_not_signal(self):
        self.fake_runtime()
        self.identity['ppid'] = 98765
        with self.assertRaises(RuntimeError):
            with m.owned_l1(self.config): self.fail('no yield')
        self.assertEqual(self.process.signals,[])

    def test_json_ready_malformed_fails_without_measurement(self):
        self.fake_runtime(ssh=lambda args,**kw:subprocess.CompletedProcess(args,0,'not JSON'))
        with self.assertRaises(json.JSONDecodeError):
            with m.owned_l1(self.config): self.fail('no yield')
        self.assertEqual(self.process.signals,[signal.SIGTERM])

    def test_current_port_must_belong_to_own_qemu(self):
        self.fake_runtime()
        clock = iter(range(20))
        with patch.object(m,'owned_listener',return_value=False), \
             patch.object(m.time,'monotonic',side_effect=lambda:next(clock)), patch.object(m.time,'sleep'):
            with self.assertRaises(TimeoutError):
                with m.owned_l1(replace(self.config,readiness_timeout=5)): self.fail('no yield')
        self.assertFalse(any(args[0]=='/usr/bin/ssh' for args,_ in self.commands))
        self.assertEqual(self.process.signals,[signal.SIGTERM])


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

    def test_clean_prepared_poweroff_requires_exact_exit_zero_and_reaps(self):
        self.poweroff_runtime()
        with m.prepare_l1(self.config) as vm:
            vm.poweroff_prepared()
            self.assertEqual(vm.state['status'],'powered-off')
            self.assertTrue(vm.state['poweroff_verified']['reaped'])
            self.assertEqual(vm.state['poweroff_verified']['ssh_returncode'],255)
        state=self.last_state()
        self.assertEqual(state['status'],'completed')
        self.assertEqual(state['qemu_returncode'],0)
        self.assertIn('post_poweroff_resources',state)
        self.assertEqual(self.process.signals,[])
        self.assertEqual(len([a for a,k in self.commands if a[0]=='/usr/bin/ssh']),3)

    def test_shutdown_disconnect_without_exit_times_out_and_forces_owned_cleanup(self):
        self.poweroff_runtime('noexit')
        with self.assertRaisesRegex(TimeoutError,'did not power off'):
            with m.prepare_l1(replace(self.config,stop_grace=.01)) as vm:
                with patch.object(m.time,'sleep'):
                    vm.poweroff_prepared()
        self.assertNotIn('poweroff_verified',self.last_state())
        self.assertEqual(self.process.signals,[signal.SIGTERM])

    def test_shutdown_nonzero_qemu_exit_is_not_clean_proof(self):
        self.poweroff_runtime('badexit')
        with self.assertRaisesRegex(RuntimeError,'did not exit cleanly'):
            with m.prepare_l1(self.config) as vm:
                vm.poweroff_prepared()
        self.assertNotIn('poweroff_verified',self.last_state())
        self.assertEqual(self.process.signals,[])

    def test_shutdown_identity_drift_does_not_signal(self):
        self.poweroff_runtime('identity')
        with self.assertRaisesRegex(RuntimeError,'identity changed'):
            with m.prepare_l1(self.config) as vm:
                vm.poweroff_prepared()
        self.assertNotIn('poweroff_verified',self.last_state())
        self.assertEqual(self.process.signals,[])

    def test_shutdown_remote_command_error_fails_and_cleans(self):
        self.poweroff_runtime('commandfail')
        with self.assertRaisesRegex(RuntimeError,'poweroff command failed'):
            with m.prepare_l1(self.config) as vm:
                vm.poweroff_prepared()
        self.assertNotIn('poweroff_verified',self.last_state())
        self.assertEqual(self.process.signals,[signal.SIGTERM])

    def test_shutdown_is_prepare_only(self):
        self.poweroff_runtime()
        with self.assertRaisesRegex(RuntimeError,'preparation-only'):
            with m.owned_l1(self.config) as vm:
                vm.poweroff_prepared()
        self.assertEqual(self.process.signals,[signal.SIGTERM])

    def test_shutdown_one_shot(self):
        self.poweroff_runtime()
        with self.assertRaisesRegex(RuntimeError,'one-shot'):
            with m.prepare_l1(self.config) as vm:
                vm.poweroff_prepared()
                vm.poweroff_prepared()
        self.assertEqual(self.process.signals,[])

    def test_state_field_cannot_forge_clean_shutdown(self):
        self.fake_runtime()
        with self.assertRaisesRegex(RuntimeError,'QEMU exited'):
            with m.prepare_l1(self.config) as vm:
                vm.state['poweroff_verified']={'qemu_returncode':0}
                self.process.returncode=0
        self.assertEqual(self.last_state()['status'],'failed')


    def test_poweroff_exit_between_outer_poll_and_identity_poll_is_success(self):
        self.poweroff_runtime('noexit')
        original=self.process.poll
        shutdown=False
        polls=0
        # Begin the interleaving only after strict readiness has completed.
        original_run=m.subprocess.run.side_effect
        def run(args,**kw):
            nonlocal shutdown
            result=original_run(args,**kw)
            if args[0]=='/usr/bin/ssh' and args[-1]==m.SHUTDOWN:shutdown=True
            return result
        def poll():
            nonlocal polls
            if shutdown:
                polls+=1
                if polls>=2:self.process.returncode=0
            return original()
        with patch.object(m.subprocess,'run',side_effect=run), patch.object(self.process,'poll',side_effect=poll):
            with m.prepare_l1(self.config) as vm:
                vm.poweroff_prepared()
        self.assertEqual(self.process.signals,[])
        self.assertEqual(self.last_state()['qemu_returncode'],0)
        self.assertTrue(self.last_state()['poweroff_verified']['reaped'])

    def test_poweroff_exe_disappears_then_exact_child_wait_exit_zero(self):
        self.poweroff_runtime('noexit')
        vanished=False
        original_run=m.subprocess.run.side_effect
        def run(args,**kw):
            nonlocal vanished
            result=original_run(args,**kw)
            if args[0]=='/usr/bin/ssh' and args[-1]==m.SHUTDOWN:vanished=True
            return result
        def proc(pid):
            if vanished:raise FileNotFoundError('/proc/owned/exe')
            return dict(self.identity)
        self.identity_call.side_effect=proc
        with patch.object(m.subprocess,'run',side_effect=run):
            with m.prepare_l1(self.config) as vm:
                vm.poweroff_prepared()
        self.assertTrue(any(t is not None and 0<t<=self.config.stop_grace for t in self.process.waits))
        self.assertEqual(self.process.signals,[])
        self.assertEqual(self.last_state()['qemu_returncode'],0)

    def test_poweroff_exe_disappears_but_nonzero_exit_stays_failure(self):
        self.poweroff_runtime('noexit')
        vanished=False
        original_run=m.subprocess.run.side_effect
        original_wait=self.process.wait
        def run(args,**kw):
            nonlocal vanished
            result=original_run(args,**kw)
            if args[0]=='/usr/bin/ssh' and args[-1]==m.SHUTDOWN:vanished=True
            return result
        def proc(pid):
            if vanished:raise FileNotFoundError('/proc/owned/exe')
            return dict(self.identity)
        def wait(timeout=None):
            if vanished:self.process.returncode=9
            return original_wait(timeout)
        self.identity_call.side_effect=proc
        with patch.object(m.subprocess,'run',side_effect=run), patch.object(self.process,'wait',side_effect=wait):
            with self.assertRaisesRegex(RuntimeError,'did not exit cleanly: 9'):
                with m.prepare_l1(self.config) as vm:
                    vm.poweroff_prepared()
        self.assertNotIn('poweroff_verified',self.last_state())
        self.assertEqual(self.process.signals,[])

    def test_missing_exe_while_still_live_is_not_success_or_permission_to_signal(self):
        self.poweroff_runtime('noexit')
        vanished=False
        original_run=m.subprocess.run.side_effect
        def run(args,**kw):
            nonlocal vanished
            result=original_run(args,**kw)
            if args[0]=='/usr/bin/ssh' and args[-1]==m.SHUTDOWN:vanished=True
            return result
        def proc(pid):
            if vanished:raise FileNotFoundError('/proc/owned/exe')
            return dict(self.identity)
        def wait(timeout=None):
            raise subprocess.TimeoutExpired('still-live',timeout)
        self.identity_call.side_effect=proc
        with patch.object(m.subprocess,'run',side_effect=run), patch.object(self.process,'wait',side_effect=wait):
            with self.assertRaisesRegex(RuntimeError,'identity unavailable while still live'):
                with m.prepare_l1(self.config) as vm:
                    vm.poweroff_prepared()
        state=self.last_state()
        self.assertNotIn('poweroff_verified',state)
        self.assertIn('still live',state['error'])
        self.assertIn('still live',state['cleanup_error'])
        self.assertEqual(self.process.signals,[])

    def test_first_body_error_preserved_when_cleanup_has_identity_failure(self):
        self.fake_runtime()
        with self.assertRaisesRegex(ValueError,'original action error'):
            with m.owned_l1(self.config):
                self.identity['starttime']+=1
                raise ValueError('original action error')
        state=self.last_state()
        self.assertEqual(state['error'],'ValueError: original action error')
        self.assertIn('identity changed',state['cleanup_error'])
        self.assertEqual(self.process.signals,[])

    def test_clean_exit_during_close_identity_check_is_reaped_not_signaled(self):
        self.fake_runtime()
        with m.owned_l1(self.config) as vm:
            old_verify=vm.verify
            def verified_then_exit():
                old_verify()
                self.identity_call.side_effect=FileNotFoundError('/proc/owned/exe')
            vm.verify=verified_then_exit
        self.assertEqual(self.process.signals,[])
        self.assertEqual(self.last_state()['qemu_returncode'],0)

    def test_missing_pid_during_pidfd_signal_must_have_reaped_exit(self):
        self.fake_runtime()
        def signal_exit(fd,sig):
            self.process.returncode=0
            raise ProcessLookupError('already gone')
        with patch.object(m.signal,'pidfd_send_signal',side_effect=signal_exit):
            with m.owned_l1(self.config):
                pass
        self.assertEqual(self.last_state()['qemu_returncode'],0)
        self.assertEqual(self.last_state()['status'],'completed')

    def test_all_l1_ssh_calls_ignore_user_config(self):
        self.poweroff_runtime()
        with m.prepare_l1(self.config) as vm:
            vm.poweroff_prepared()
        commands=[args for args,kw in self.commands if args[0]=='/usr/bin/ssh']
        self.assertTrue(commands)
        self.assertTrue(all(args[1:3]==['-F','/dev/null'] for args in commands))


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

    def test_private_mode_at_atomic_publication_with_permissive_umask(self):
        replace = Path.replace
        seen = []
        def checked(path, dest):
            seen.append(path.stat().st_mode & 0o777)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            return replace(path, dest)
        old = os.umask(0o000)
        try:
            with patch.object(Path, 'replace', checked):
                m.write_lifecycle(self.folder, {'status':'preparing'})
                m.write_lifecycle(self.folder, {'status':'ready'})
            self.assertEqual(seen, [0o600, 0o600])
            self.assertEqual(m.trusted(self.folder/'lifecycle.json'), self.folder/'lifecycle.json')
            self.assertEqual(json.loads((self.folder/'lifecycle.json').read_text()), {'status':'ready'})
        finally:
            os.umask(old)

    def test_serialization_failure_preserves_previous_state_and_cleans_owned_temp(self):
        m.write_lifecycle(self.folder, {'status':'ready'})
        before = (self.folder/'lifecycle.json').read_bytes()
        with patch.object(m.json, 'dump', side_effect=ValueError('serialization fixture')):
            with self.assertRaisesRegex(ValueError, 'serialization fixture'):
                m.write_lifecycle(self.folder, {'status':'running'})
        self.assertEqual((self.folder/'lifecycle.json').read_bytes(), before)
        self.assertEqual(list(self.folder.glob('lifecycle.json.*.tmp')), [])

    def test_symlink_folder_rejected_without_writing(self):
        target = self.folder/'target';target.mkdir()
        alias = self.folder/'alias';alias.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            m.write_lifecycle(alias, {'status':'ready'})
        self.assertEqual(list(target.iterdir()), [])

if __name__ == '__main__':
    unittest.main()
