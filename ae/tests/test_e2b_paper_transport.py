"""Offline transport tests. All SSH/SCP and owned-process queries are faked."""
from contextlib import ExitStack
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from ae.scripts import e2b_l1_context as l1
from ae.scripts import e2b_paper_transport as m


class TransportFixture(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.repo=self.root/'repo'
        self.output=self.repo/'ae/results/selected/run/input'
        self.output.mkdir(parents=True)
        self.life=self.root/'lifecycle.json'
        self.identity={'pid':123456,'ppid':99999,'starttime':1,'cgroup':'/owned','exe':'/frozen/qemu'}
        self.life.write_text(json.dumps({'status':'ready','ownership':self.identity}))
        self.private=self.root/'id'
        self.private.write_text('PRIVATE-DO-NOT-READ')
        self.private.chmod(0o600)
        self.hosts=self.root/'known_hosts'
        self.hosts.write_text('[127.0.0.1]:57785 ssh-ed25519 known')
        self.manifest=self.root/'transport.json'
        self.value={'kind':'e2b-paper-owned-transport-v1','host_output_root':str(self.output),
                    'lifecycle':str(self.life),'qemu_identity':self.identity,'ssh_port':57785,
                    'ssh_identity':str(self.private),'known_hosts':str(self.hosts),
                    'guest_root':'/var/tmp/e2b-paper-'+uuid.uuid4().hex}
        self.write_manifest()
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(m,'REPO',self.repo))
        self.proc=self.stack.enter_context(patch.object(l1,'proc_identity',side_effect=self.ident))
        self.anc=self.stack.enter_context(patch.object(l1,'ancestors',return_value={99999:{'pid':99999}}))
        self.listener=self.stack.enter_context(patch.object(l1,'owned_listener',return_value=True))
        self.exec=self.stack.enter_context(patch.object(m.subprocess,'run',side_effect=AssertionError('No real external command allowed')))

    def ident(self,pid):
        return dict(self.identity) if pid==self.identity['pid'] else {'cgroup':'/owned'}

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.value))

    def transport(self):
        return m.Transport(self.manifest)


class Guard(TransportFixture):
    def test_valid_owned_transport(self):
        self.assertEqual(self.transport().port,57785)

    def test_creator_manifest_under_hosted_umask_passes_unchanged_trust_gate(self):
        old = os.umask(0o002)
        try:
            l1.write_lifecycle(self.root, {'status':'ready','ownership':self.identity})
        finally:
            os.umask(old)
        self.assertEqual(self.life.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.transport().identity, self.identity)

    def test_externally_writable_lifecycle_still_denied(self):
        for mode in (0o664, 0o646):
            with self.subTest(mode=mode):
                self.life.chmod(mode)
                with self.assertRaisesRegex(ValueError, 'root-owned and not externally writable'):
                    self.transport()

    def test_externally_writable_known_hosts_still_denied(self):
        self.hosts.chmod(0o664)
        with self.assertRaisesRegex(ValueError, 'root-owned and not externally writable'):
            self.transport()

    def test_bad_kind_rejected(self):
        self.value['kind']='ordinary-ssh'
        self.write_manifest()
        with self.assertRaises(ValueError):self.transport()

    def test_output_outside_selected_denied(self):
        self.value['host_output_root']=str(self.root)
        self.write_manifest()
        with self.assertRaises(ValueError):self.transport()

    def test_output_symlink_denied(self):
        alias=self.output.parent/'alias'
        alias.symlink_to(self.output)
        self.value['host_output_root']=str(alias)
        self.write_manifest()
        with self.assertRaises(ValueError):self.transport()

    def test_private_key_permissions_denied(self):
        self.private.chmod(0o644)
        with self.assertRaises(ValueError):self.transport()

    def test_private_key_bytes_never_read(self):
        original=Path.open
        def guarded(path,*a,**kw):
            if path==self.private:raise AssertionError('private bytes accessed')
            return original(path,*a,**kw)
        with patch.object(Path,'open',guarded):
            self.transport()

    def test_pid_identity_change_denied(self):
        t=self.transport()
        self.proc.side_effect=lambda pid: dict(self.identity,starttime=3) if pid==123456 else {'cgroup':'/owned'}
        with self.assertRaises(RuntimeError):t.guard()

    def test_finished_lifecycle_denied(self):
        t=self.transport()
        self.life.write_text(json.dumps({'status':'completed','ownership':self.identity}))
        with self.assertRaises(RuntimeError):t.guard()

    def test_nonancestor_owner_denied(self):
        self.anc.return_value={}
        with self.assertRaises(RuntimeError):self.transport()

    def test_wrong_cgroup_denied(self):
        self.proc.side_effect=lambda pid:dict(self.identity) if pid==123456 else {'cgroup':'/foreign'}
        with self.assertRaises(RuntimeError):self.transport()

    def test_wrong_listener_denied(self):
        self.listener.return_value=False
        with self.assertRaises(RuntimeError):self.transport()

    def test_guest_work_must_be_canonical_owned_uuid(self):
        for root in ('/tmp/e2b-paper-'+uuid.uuid4().hex,
                     '/var/tmp/e2b-paper-../escape',
                     '/var/tmp/e2b-paper-'+'a'*31):
            with self.subTest(root=root):
                self.value['guest_root']=root
                self.write_manifest()
                with self.assertRaises(ValueError):self.transport()

    def test_remote_fixed_endpoint_and_strict_known_host(self):
        t=self.transport()
        self.exec.side_effect=None
        self.exec.return_value=subprocess.CompletedProcess([],0,'ok','')
        t.remote('literal fixed command')
        args=self.exec.call_args.args[0]
        self.assertEqual(args[0],'/usr/bin/ssh')
        self.assertIn('StrictHostKeyChecking=yes',args)
        self.assertIn('ForwardAgent=no',args)
        self.assertEqual(args[-2:],[ 'ubuntu@127.0.0.1','literal fixed command'])
        self.assertIn(str(self.private),args)

    def test_copy_remote_escape_denied(self):
        t=self.transport()
        (self.output/'a').write_text('source')
        for path in ('/etc/passwd',t.guest_root+'/../escape'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                t.copy(self.output/'a',path,upload=True)
        self.exec.assert_not_called()

    def test_selected_root_itself_rejected(self):
        self.value['host_output_root']=str(self.repo/'ae/results/selected')
        self.write_manifest()
        with self.assertRaises(ValueError):self.transport()

    def test_direct_copy_local_upload_escape_rejected(self):
        outside=self.root/'outside'
        outside.write_text('outside')
        t=self.transport()
        with self.assertRaises(ValueError):
            t.copy(outside,t.guest_root+'/inside',upload=True)
        self.exec.assert_not_called()

    def test_direct_copy_local_download_escape_rejected(self):
        t=self.transport()
        with self.assertRaises(ValueError):
            t.copy(t.guest_root+'/inside',self.root/'outside',upload=False)
        self.exec.assert_not_called()

    def test_ssh_and_scp_disable_config_loading(self):
        t=self.transport()
        source=self.output/'payload'
        source.write_text('actual payload')
        self.exec.side_effect=None
        self.exec.return_value=subprocess.CompletedProcess([],0,'','')
        t.remote('fixed command')
        t.copy(source,t.guest_root+'/payload',upload=True)
        args=[call.args[0] for call in self.exec.call_args_list]
        self.assertEqual(len(args),2)
        for command in args:
            i=command.index('-F')
            self.assertEqual(command[i+1],'/dev/null')

    def test_sidecars_health_contract_and_no_replace_receipt(self):
        self.value.update(worker_mock_port=50123,index_port=50124)
        self.write_manifest()
        t=self.transport()
        t.remote=Mock(return_value=subprocess.CompletedProcess([],0,'{"ok":true}',''))
        evidence=t.sidecars_ready(50123,50124)
        self.assertEqual([x['port'] for x in evidence],[50123,50124])
        self.assertEqual(t.remote.call_count,2)
        self.assertIn('http://10.0.2.2:50123/admin/healthz',t.remote.call_args_list[0].args[0])
        self.assertIn('http://10.0.2.2:50124/healthz',t.remote.call_args_list[1].args[0])
        receipt=self.output/'l1-sidecar-reachability.json'
        raw=receipt.read_bytes()
        with self.assertRaises(FileExistsError):t.sidecars_ready(50123,50124)
        self.assertEqual(receipt.read_bytes(),raw)

    def test_sidecars_wrong_port_or_false_response_fails(self):
        self.value.update(worker_mock_port=50123,index_port=50124)
        self.write_manifest()
        t=self.transport()
        t.remote=Mock(return_value=subprocess.CompletedProcess([],0,'{"ok":false}',''))
        with self.assertRaises(ValueError):t.sidecars_ready(50124,50123)
        t.remote.assert_not_called()
        with self.assertRaises(RuntimeError):t.sidecars_ready(50123,50124)
        self.assertFalse((self.output/'l1-sidecar-reachability.json').exists())



class LocalBounds(TransportFixture):
    def test_lexical_parent_escape_denied(self):
        outside=self.output.parent/'outside'
        outside.write_text('outside')
        sub=self.output/'sub'
        sub.mkdir()
        with self.assertRaises(ValueError):
            m.local_file(sub/'../../outside',self.output,existing=True)

    def test_download_parent_escape_denied(self):
        (self.output/'sub').mkdir()
        with self.assertRaises(ValueError):
            m.local_file(self.output/'sub/../../new',self.output,existing=False)

    def test_upload_hardlink_denied(self):
        source=self.output/'source'
        source.write_text('original')
        os.link(source,self.output/'alias')
        with self.assertRaises(ValueError):m.local_file(source,self.output,existing=True)

    def test_upload_fifo_denied_without_open(self):
        path=self.output/'fifo'
        os.mkfifo(path)
        with self.assertRaises(ValueError):m.local_file(path,self.output,existing=True)

    def test_download_existing_not_overwritten(self):
        path=self.output/'existing'
        path.write_text('keep')
        with self.assertRaises(ValueError):m.local_file(path,self.output,existing=False)
        self.assertEqual(path.read_text(),'keep')

    def test_symlink_component_denied(self):
        outside=self.root/'outside'
        outside.mkdir()
        (self.output/'alias').symlink_to(outside)
        with self.assertRaises(ValueError):m.local_file(self.output/'alias/new',self.output,existing=False)


class Step(TransportFixture):
    def setUp(self):
        super().setUp()
        self.t=self.transport()
        self.payload=self.output/'payload.json'
        self.payload.write_text('{"input":"whole trajectory"}')
        self.timing=self.output/'timing.json'
        self.response=self.output/'response.json'
        self.guest={}
        self.job_rc=0
        self.timing_data={'ok':True,'checkpoint_ms':3.5,'restore_ms':2.1}
        self.missing=set()
        self.corrupt=set()
        self.command_args=None
        self.trace=[]
        self.driver=types.SimpleNamespace(e2b_resume_build_cmd=self.build_command)
        self.t.remote=Mock(side_effect=self.remote)
        self.t.copy=Mock(side_effect=self.copy)
        self.kw={'from_build':str(uuid.uuid4()),'to_build':str(uuid.uuid4()),
                 'storage':self.t.storage,'command':'python3 /tmp/fixed.py',
                 'timings_path':self.timing,'uploads':[(self.payload,'/tmp/payload')],
                 'downloads':[('/tmp/response',self.response)]}

    def build_command(self,**kw):
        self.command_args=kw
        self.guest[str(kw['finalbench_json'])]=json.dumps(self.timing_data).encode()
        for source,target in kw['downloads']:
            self.guest[str(target)]=b'{"ok":true,"real_response":1}'
        return 'RUN_DRIVER'

    def remote(self,command,**kwargs):
        self.trace.append(('remote',command))
        if command.startswith('mkdir -m 700 -p '):
            return subprocess.CompletedProcess([],0,'','')
        if command.startswith('sha256sum '):
            import shlex
            path=shlex.split(command)[1]
            if any(path.endswith(x) for x in self.missing):
                raise subprocess.CalledProcessError(1,command)
            data=self.guest[path]
            return subprocess.CompletedProcess([],0,hashlib.sha256(data).hexdigest()+'  '+path+'\n','')
        self.assertEqual(command,'RUN_DRIVER')
        self.assertFalse(kwargs['check'])
        return subprocess.CompletedProcess([],self.job_rc,'actual stdout','actual stderr')

    def copy(self,source,destination,*,upload,**kwargs):
        self.trace.append(('copy',upload,str(source),str(destination)))
        if upload:
            self.guest[str(destination)]=Path(source).read_bytes()
        else:
            data=self.guest[str(source)]
            if any(str(source).endswith(x) for x in self.corrupt):data+=b'changed'
            Path(destination).write_bytes(data)
        return subprocess.CompletedProcess([],0,'','')

    def step(self,**overrides):
        return self.t.step(self.driver,**{**self.kw,**overrides})

    def test_step_hashes_all_files_and_preserves_inner_metrics(self):
        result=self.step()
        self.assertTrue(result['ok'])
        self.assertEqual(result['checkpoint_ms'],3.5)
        self.assertEqual(result['restore_ms'],2.1)
        receipt=json.loads(Path(result['transport_receipt']).read_text())
        self.assertEqual(len(receipt['uploads']),1)
        self.assertEqual(len(receipt['downloads']),2)
        self.assertFalse(receipt['transfer_errors'])
        self.assertEqual(self.response.stat().st_nlink,1)
        self.assertEqual(self.command_args['storage'],self.t.storage)
        self.assertNotEqual(self.command_args['finalbench_json'],self.timing)

    def test_nonzero_process_does_not_become_success_even_with_good_timing(self):
        self.job_rc=1
        result=self.step()
        self.assertFalse(result['ok'])
        self.assertEqual(result['host_rc'],1)
        self.assertTrue(result['timing_present'])

    def test_inner_ok_false_does_not_become_success(self):
        self.timing_data['ok']=False
        self.assertFalse(self.step()['ok'])

    def test_nonboolean_inner_ok_does_not_become_success(self):
        self.timing_data['ok']=1
        self.assertFalse(self.step()['ok'])

    def test_missing_timing_is_failure(self):
        self.missing.add('timing.json')
        result=self.step()
        self.assertFalse(result['ok'])
        self.assertFalse(result['timing_present'])
        self.assertFalse(self.timing.exists())

    def test_missing_response_is_failure_even_if_timing_ok(self):
        self.missing.add('download-0')
        result=self.step()
        self.assertFalse(result['ok'])
        self.assertTrue(result['timing_present'])
        self.assertFalse(self.response.exists())

    def test_corrupt_download_keeps_partial_and_no_final_result(self):
        self.corrupt.add('download-0')
        result=self.step()
        self.assertFalse(result['ok'])
        self.assertFalse(self.response.exists())
        self.assertTrue(list(self.output.glob('response.json.partial-*')))

    def test_existing_result_refused_before_remote_action(self):
        self.response.write_text('previous')
        with self.assertRaises(ValueError):self.step()
        self.t.remote.assert_not_called()
        self.assertEqual(self.response.read_text(),'previous')

    def test_result_created_during_transfer_is_not_overwritten(self):
        original=self.copy
        def copy(*args,**kw):
            result=original(*args,**kw)
            if not kw['upload'] and str(args[0]).endswith('download-0'):
                self.response.write_text('concurrent independent output')
            return result
        self.t.copy.side_effect=copy
        result=self.step()
        self.assertFalse(result['ok'])
        self.assertEqual(self.response.read_text(),'concurrent independent output')

    def test_changed_upload_source_rejected(self):
        original=self.copy
        def copy(*args,**kw):
            result=original(*args,**kw)
            if kw['upload']:Path(args[0]).write_text('changed')
            return result
        self.t.copy.side_effect=copy
        with self.assertRaisesRegex(ValueError,'changed during transfer'):self.step()
        self.assertIsNone(self.command_args)

    def test_guest_upload_hash_mismatch_rejected(self):
        original=self.copy
        def copy(*args,**kw):
            result=original(*args,**kw)
            if kw['upload']:self.guest[str(args[1])]+=b'corrupt'
            return result
        self.t.copy.side_effect=copy
        with self.assertRaisesRegex(ValueError,'Guest upload SHA mismatch'):self.step()
        self.assertIsNone(self.command_args)

    def test_driver_timeout_propagates_no_result_success(self):
        original=self.remote
        def remote(command,**kw):
            if command=='RUN_DRIVER':raise subprocess.TimeoutExpired(command,kw['timeout'],output=b'partial stdout',stderr=b'partial stderr')
            return original(command,**kw)
        self.t.remote.side_effect=remote
        with self.assertRaises(subprocess.TimeoutExpired):self.step()
        self.assertFalse(self.timing.exists())
        receipts=list(self.output.glob('*.transport/receipt.json'))
        self.assertEqual(len(receipts),1)
        receipt=json.loads(receipts[0].read_text())
        self.assertFalse(receipt['ok'])
        self.assertTrue(receipt['partial_output'])
        self.assertEqual((receipts[0].parent/'stdout.log').read_text(),'partial stdout')
        self.assertEqual((receipts[0].parent/'stderr.log').read_text(),'partial stderr')
        self.assertEqual((receipts[0].parent/'command.txt').read_text(),'RUN_DRIVER\n')

    def test_storage_and_uuid_gate_before_transfer(self):
        for override in ({'storage':'/foreign/storage'},{'from_build':'not-a-uuid'},{'timeout':2401}):
            with self.subTest(override=override),self.assertRaises(ValueError):self.step(**override)
        self.t.remote.assert_not_called()



class Installation(TransportFixture):
    def test_install_freezes_resume_binary_private_sandbox_and_sidecar_ip(self):
        driver=types.SimpleNamespace(e2b_env_prefix=lambda:'export ORIGINAL=yes; ',
            create_base_build=lambda **kw:None, e2b_step=lambda **kw:None)
        with patch.dict(os.environ,{'AE_E2B_PAPER_TRANSPORT':str(self.manifest),
                'E2B_RESUME_BINARY':'foreign','E2B_SANDBOX_DIR':'foreign'}):
            transport=m.install(driver)
            self.assertEqual(driver.E2B,Path(transport.runtime))
            self.assertEqual(os.environ['E2B_RESUME_BINARY'],transport.runtime+'/bin/resume-build')
            self.assertEqual(os.environ['E2B_SANDBOX_DIR'],transport.guest_root+'/sandboxes')
            self.assertEqual(driver.SIDE_CAR_IP_FOR_SANDBOX,'10.0.2.2')
            prefix=driver.e2b_env_prefix()
            self.assertIn('unset LAUNCH_DARKLY_API_KEY;',prefix)
            self.assertIn('HOST_ENVD_PATH='+transport.runtime+'/envd/envd',prefix)
            self.assertIn('HOST_KERNELS_DIR='+transport.runtime+'/kernels',prefix)
            with self.assertRaises(ValueError):driver.create_base_build()
            transport.step=Mock(return_value={'ok':False})
            self.assertEqual(driver.e2b_step(token='sentinel'),{'ok':False})
            transport.step.assert_called_once_with(driver,token='sentinel')
        self.exec.assert_not_called()

    def test_install_without_explicit_profile_transport_fails(self):
        with patch.dict(os.environ,{},clear=True),self.assertRaises(ValueError):
            m.install(types.SimpleNamespace())


if __name__=='__main__':
    unittest.main()
