"""Owned activation contracts over real temporary files; no services/VMs/RPCs."""
from contextlib import ExitStack
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('uffd_diag_contract', BASE/'scripts/e2b_uffd_diagnostic.py')
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)


class Files(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.output = self.root/'ae/results/selected/diagnostic-test'
        self.out = self.output/'job/environment/e2b-placement'
        self.out.mkdir(parents=True)
        self.control = self.root/'ae/work/numa03-validation/recovery'
        self.control.mkdir(parents=True)
        self.proc = self.root/'proc';self.proc.mkdir()
        self.stack = ExitStack();self.addCleanup(self.stack.close)
        # Only root ownership is emulated on developer macOS. Type, mode, nlink,
        # bytes, mtime, device/inode, symlink checks and I/O are real.
        self.real_fstat, self.real_lstat = os.fstat, Path.lstat
        def root_stat(st):
            keys = ('st_dev','st_ino','st_mode','st_uid','st_nlink','st_size','st_mtime_ns')
            return SimpleNamespace(**{k: 0 if k == 'st_uid' else getattr(st,k) for k in keys})
        self.stack.enter_context(patch.object(d.os, 'fstat', side_effect=lambda fd: root_stat(self.real_fstat(fd))))
        self.stack.enter_context(patch.object(Path, 'lstat', lambda p: root_stat(self.real_lstat(p))))
        self.stack.enter_context(patch.object(d, 'PROC', self.proc))
        self.stack.enter_context(patch.dict(sys.modules, {'release.lock': SimpleNamespace(runtime_identity=lambda root: {'source_sha256': 'a'*64})}))
        self.request = {'schema': 'deltabox.owned-uffd-request.v1', 'nonce': '1'*32,
            'output': str(self.output), 'runtime_sha256':'a'*64, 'executable_sha256':'d'*64,
            'fixture':self.ref('fixture.py', b'# fixture\n'), 'node':3, 'cpus':'72-75',
            'control_sha256':d.CONTROL, 'refs': {key:self.ref(key+'.json', b'{}\n') for key in sorted(d.REFS)}}
        self.reference = self.ref('request.json', d._json(self.request))
        self.before = {'units': {d.UNIT:self.daemon(41, 401)}}

    def ref(self, name, data):
        path = self.control/name
        path.write_bytes(data);path.chmod(0o600)
        return dict(path=str(path),sha256=d.digest(data),bytes=len(data))

    def rewrite_request(self):
        self.reference = self.ref('request.json', d._json(self.request))

    def daemon(self, pid, ticks, selected=None):
        base = self.proc/str(pid);base.mkdir(exist_ok=True)
        (base/'stat').write_text(str(pid)+' (test) '+' '.join(['S']+['0']*18+[str(ticks)]))
        (base/'status').write_text('Uid:\t0\t0\t0\t0\n')
        (base/'cgroup').write_text('0::/system.slice/'+d.UNIT+'\n')
        if not (base/'exe').is_symlink():(base/'exe').symlink_to(d.EXE)
        if not (base/'root').exists():(base/'root').symlink_to('/')
        env = b'API_KEY=secret-never-record\0'
        if selected is not None:env += d.SELECTED_ENV.encode()+b'='+selected.encode()+b'\0'
        (base/'environ').write_bytes(env)
        return {'process': {'pid':pid,'start_ticks':ticks,'exe':d.EXE},'binary_sha256':'d'*64}

    def session(self):
        return d.Session(json.dumps(self.reference), root=self.root, out=self.out,
            source_sha256='a'*64,node=3,cpus='72-75',before=self.before,
            admission={'numa_lease_owner':1,'results_lease_owner':2})

    def guarded(self):
        s = self.session()
        s.guard.write_bytes(d._json({'reason':'E2B placement transaction in progress','evidence':str(self.out)}))
        s.guard.chmod(0o600);s.bind_guard()
        return s

    def published(self):
        s = self.guarded()
        ram = self.root/'ae/work/et-0123456789';(ram/'tmp').mkdir(parents=True)
        active = {'units':{d.UNIT:self.daemon(42,402,str(s.config))}}
        (self.out/'actual.json').write_bytes(d._json(active));(self.out/'actual.json').chmod(0o600)
        s.publish(active,SimpleNamespace(ram=ram))
        return s

    def test_shared_request_reads_same_protected_bytes_without_future_state(self):
        r, rec = d.validate_request(self.reference,root=self.root,output=self.output,
            source_sha256='a'*64,node=3,cpus='72-75')
        self.assertEqual(r,self.request)
        self.assertEqual({k:rec[k] for k in self.reference},self.reference)
        self.assertFalse((self.output/'job/environment/uffd-aggregate').exists())

    def test_request_wrong_scope_or_reference_hash_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'namespace'):
            d.validate_request(self.reference,root=self.root,output=self.output.with_name('x'*101),
                source_sha256='a'*64,node=3,cpus='72-75')
        self.request['node']=0;self.rewrite_request()
        with self.assertRaisesRegex(ValueError,'scope'):self.session()
        self.request['node']=3;self.request['refs']['backend_install']['sha256']='0'*64;self.rewrite_request()
        with self.assertRaisesRegex(ValueError,'bytes mismatch'):self.session()

    def test_protected_ref_rejects_symlink_hardlink_writable_and_modified_bytes(self):
        path=Path(self.reference['path'])
        link=self.control/'alias';link.symlink_to(path)
        with self.assertRaises(ValueError):d.protected(link,16384)
        link.unlink();os.link(path,link)
        with self.assertRaises(ValueError):d.protected(path,16384)
        link.unlink();path.chmod(0o622)
        with self.assertRaises(ValueError):d.protected(path,16384)
        path.chmod(0o600);path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(ValueError):d.protected(path,16384,expected=self.reference)

    def test_before_nonempty_environment_rejects_before_directory_or_guard(self):
        self.daemon(41,401,'unknown-config')
        with self.assertRaisesRegex(ValueError,'already enabled'):self.session()
        self.assertFalse((self.out.parent/'uffd-aggregate').exists())
        self.assertFalse((self.root/'ae/work/E2B_SERVICE_RECOVERY_REQUIRED.json').exists())

    def test_daemon_reuse_nonroot_or_foreign_exe_is_rejected(self):
        self.daemon(41,999)
        with self.assertRaisesRegex(ValueError,'identity'):self.session()
        self.daemon(41,401);(self.proc/'41/status').write_text('Uid:\t1\t1\t1\t1\n')
        with self.assertRaisesRegex(ValueError,'root-private'):self.session()
        self.daemon(41,401);(self.proc/'41/exe').unlink();(self.proc/'41/exe').symlink_to('/foreign')
        with self.assertRaisesRegex(ValueError,'identity'):self.session()

    def test_publication_exact_go_schema_and_exclusive_bytes_no_credentials(self):
        s=self.published();row=json.loads(s.config.read_bytes())
        self.assertEqual(set(row),{'schema','nonce','output','socket_root','directory','pid','start_ticks',
            'executable_sha256','runtime_sha256','control_sha256','guard_sha256'})
        self.assertEqual(row['pid'],42);self.assertEqual(row['start_ticks'],402)
        self.assertEqual(s.config.stat().st_mode & 0o777,0o600)
        self.assertEqual(row['guard_sha256'],d.digest(s.guard.read_bytes()))
        self.assertEqual((s.directory/'initial-guard.json').read_bytes(),s.guard.read_bytes())
        self.assertNotIn('secret-never-record', ''.join(p.read_text() for p in s.directory.iterdir()))
        before=s.config.read_bytes()
        with self.assertRaises(FileExistsError):d.exclusive_json(s.config,{'overwrite':True},8192)
        self.assertEqual(s.config.read_bytes(),before)

    def test_publish_refuses_wrong_selected_environment(self):
        s=self.guarded()
        active={'units':{d.UNIT:self.daemon(42,402,'wrong')}}
        with self.assertRaisesRegex(ValueError,'environment mismatch'):
            s.publish(active,SimpleNamespace(ram=self.root/'ae/work/et-0123456789'))
        self.assertFalse(s.config.exists())

    def test_same_guard_bytes_replaced_inode_are_rejected(self):
        s=self.guarded();other=s.guard.with_suffix('.other');other.write_bytes(s.guard.read_bytes());other.chmod(0o600)
        other.replace(s.guard)
        with self.assertRaisesRegex(ValueError,'identity changed'):s.check_guard()

    def test_missing_and_censored_finals_are_pending_with_bounded_population(self):
        s=self.published()
        ident={'schema':'deltabox.owned-uffd-identity.v1','nonce':self.request['nonce'],'instance':1,
            'pid':42,'start_ticks':402,'config_sha256':s.config_record['sha256']}
        d.exclusive_json(s.directory/'instance-001-identity.json',ident,2048)
        ident2=dict(ident,instance=2)
        d.exclusive_json(s.directory/'instance-002-identity.json',ident2,2048)
        d.exclusive_json(s.directory/'instance-002-final.json',{'schema':'deltabox.owned-uffd-aggregate.v1',
            'identity':ident2,'statistics_complete':False},12288)
        receipt=s.seal();index=json.loads(Path(receipt['path']).read_bytes())
        self.assertTrue(index['owned_stop_confirmed'])
        self.assertIn({'instance':1,'reason':'missing-or-different-identity-final'},index['pending'])
        self.assertIn({'file':'instance-002-final.json','reason':'statistics-incomplete'},index['pending'])
        self.assertEqual(len(index['files']),7)
        self.assertEqual({Path(x['path']).name for x in index['files']} &
            {'prepared.json','published.json','initial-guard.json'},
            {'prepared.json','published.json','initial-guard.json'})
        self.assertIn('restored.json',index['terminal_fixed_files'])

    def test_selected_environment_absence_and_empty_restore_distinctly(self):
        self.daemon(41,401,'');s=self.published()
        shutil.rmtree(self.proc/'42')
        restored={'units':{d.UNIT:self.daemon(43,403)}}
        with self.assertRaisesRegex(ValueError,'not restored'):s.verify_restored(restored)
        restored={'units':{d.UNIT:self.daemon(43,403,'')}}
        s.verify_restored(restored)
        self.assertTrue((s.directory/'restored.json').exists())

    def test_restored_metadata_failure_is_only_pending(self):
        s=self.published();shutil.rmtree(self.proc/'42')
        restored={'units':{d.UNIT:self.daemon(43,403)}}
        with patch.object(d,'exclusive_json',side_effect=OSError('do not log credentials')):
            s.verify_restored(restored)
        self.assertEqual(s.pending,[{'phase':'restored-record','reason':'OSError'}])


class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse the original service transaction fixture (not its tests). The
        # installed regression path and this candidate harness use the same path.
        original=BASE.parents[0]/'tests/paper/test_e2b_service_context.py'
        spec=importlib.util.spec_from_file_location('uffd_original_workflow',original)
        cls.old=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.old)

    def work(self, *, requested=True, seal_error=False, body_error=False, guard_changed=False,
             stop_failure=False, bind_error=False, replacement=False):
        original=self.old;case=original.E2BServiceTests()
        diag=Mock()
        diag.dropin.return_value='Environment="E2B_OWNED_UFFD_AGGREGATE_CONFIG=/fixed/config.json"\n'
        if seal_error:diag.seal.side_effect=OSError('observation write failed')
        if guard_changed:diag.check_guard.side_effect=ValueError('guard identity changed')
        events=[]
        diag.bind_guard.side_effect=lambda:events.append('guard')
        if bind_error:diag.bind_guard.side_effect=RuntimeError('initial guard admission failed')
        diag.publish.side_effect=lambda *args:events.append('publish')
        if not seal_error:diag.seal.side_effect=lambda:events.append('seal')
        diag.verify_restored.side_effect=lambda *args:events.append('restored')
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'AE_E2B_UFFD_AGGREGATE_REQUEST':'{}'} if requested else {}, clear=True))
            ctor=Mock(return_value=diag)
            if replacement:
                def replace_guard():
                    (Path(folder)/'RECOVERY_REQUIRED').write_bytes(b'foreign guard; preserve exact bytes')
                    events.append('guard')
                diag.bind_guard.side_effect=replace_guard
            stack.enter_context(patch.dict(sys.modules,{'ae.scripts.e2b_uffd_diagnostic':SimpleNamespace(Session=ctor)}))
            result=case.workflow(folder,storage=True,body_error=body_error,restore_stop_failure=stop_failure)
            commands,guard,error,root=result
            saved=json.loads((root/'evidence/transaction-result.json').read_text())
            if replacement:
                self.assertEqual((root/'RECOVERY_REQUIRED').read_bytes(),b'foreign guard; preserve exact bytes')
            return commands,guard,error,saved,diag,ctor,events

    def test_default_off_does_not_import_or_call_diagnostic_session(self):
        commands,guard,error,saved,diag,ctor,events=self.work(requested=False)
        self.assertIsNone(error);self.assertFalse(guard);ctor.assert_not_called();diag.assert_not_called()
        self.assertEqual(events,[])

    def test_hook_order_and_original_service_counts(self):
        commands,guard,error,saved,diag,ctor,events=self.work()
        self.assertIsNone(error);self.assertFalse(guard)
        self.assertEqual(events,['guard','publish','seal','restored'])
        self.assertEqual(sum(c[:2]==('systemctl','stop') for c in commands),2)
        self.assertEqual(sum(c[:2]==('systemctl','start') for c in commands),4)
        diag.dropin.assert_called_once();diag.check_guard.assert_called_once()

    def test_seal_failure_cannot_replace_original_error_or_block_restoration(self):
        commands,guard,error,saved,diag,ctor,events=self.work(seal_error=True,body_error=True)
        self.assertEqual(error,'producer failed');self.assertFalse(guard)
        self.assertEqual(saved['restoration_errors'],[])
        diag.note_pending.assert_called_once();diag.verify_restored.assert_called_once()

    def test_seal_never_runs_when_owned_stop_fails(self):
        commands,guard,error,saved,diag,ctor,events=self.work(stop_failure=True)
        self.assertTrue(guard);self.assertIn('daemon stop failed',error);diag.seal.assert_not_called()

    def test_changed_guard_is_retained_and_cannot_be_accepted_as_restored(self):
        commands,guard,error,saved,diag,ctor,events=self.work(guard_changed=True,replacement=True)
        self.assertTrue(guard);self.assertIn('guard identity changed',error)
        self.assertTrue(saved['placement_retained'])

    def test_guard_admission_failure_never_enters_restart_restoration(self):
        commands,guard,error,saved,diag,ctor,events=self.work(bind_error=True)
        self.assertEqual(error,'initial guard admission failed');self.assertTrue(guard)
        self.assertEqual(commands,[]);self.assertFalse(saved['services_changed'])
        diag.publish.assert_not_called();diag.seal.assert_not_called();diag.verify_restored.assert_not_called()

    def test_unknown_guard_is_not_overwritten_on_early_cleanup_failure(self):
        commands,guard,error,saved,diag,ctor,events=self.work(guard_changed=True,replacement=True,stop_failure=True)
        self.assertTrue(guard);self.assertIn('daemon stop failed',error)
        diag.note_pending.assert_called_once();diag.seal.assert_not_called()


if __name__=='__main__':
    unittest.main()
