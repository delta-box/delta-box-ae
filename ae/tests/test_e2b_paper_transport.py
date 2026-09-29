"""Offline transport tests. All SSH/SCP and owned-process queries are faked."""
from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import shlex
import struct
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
        self.capacity_calls=[]
        def admitted(build,pending,evidence,*,download_count=0):
            self.capacity_calls.append((build,pending,str(evidence),download_count))
            self.trace.append(('capacity',pending))
            Path(evidence).write_text(json.dumps({'status':'verified','pending_upload_bytes':pending}))
            return {'status':'verified'}
        self.t.capacity_guard=Mock(side_effect=admitted)
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


class Capacity(TransportFixture):
    def setUp(self):
        super().setUp()
        self.asset=self.root/'assets.json';self.asset.write_text(json.dumps({'disk_size_gib':128}))
        self.life.write_text(json.dumps(dict(status='ready',ownership=self.identity,
            asset_manifest=dict(path=str(self.asset),sha256=hashlib.sha256(self.asset.read_bytes()).hexdigest()),
            reconstruction={'disk_size_gib':128})))
        (self.root/'l1.qcow2').write_bytes(b'fixed tiny overlay')
        self.t=self.transport()
        self.build=str(uuid.uuid4());self.guest=self.root/'guest-storage';self.guest.mkdir()
        self.expected={'memfile.header':2048*1024**2,'rootfs.ext4.header':5899*1024**2}
        self.fresh=self.root/'fresh.json'
        self.fresh.write_text(json.dumps(dict(guest_storage=str(self.guest),fresh_base_build_id=self.build,
            snapshot_closure={'builds':{self.build:{'headers':[dict(file=k,logical_bytes=v) for k,v in self.expected.items()]}}})))
        self.t.storage=str(self.guest);self.t.manifest.update(fresh_base_manifest=str(self.fresh),fresh_base_build_id=self.build)
        self.headerdir=self.guest/'templates'/self.build;self.headerdir.mkdir(parents=True)
        for name,size in self.expected.items():
            (self.headerdir/name).write_bytes(struct.pack('<QQQQ16s16s',3,4096,size,1,uuid.UUID(self.build).bytes,uuid.UUID(self.build).bytes))
        self.cache_bytes=0
        for name,size in (('memfile',32100),('rootfs.ext4',412000),('metadata.json',2),('snapfile',8)):
            (self.headerdir/name).write_bytes(b'x'*size)
            if name in ('memfile','rootfs.ext4'):self.cache_bytes+=size
        self.available=100*1024**3
        def statvfs(path):
            return types.SimpleNamespace(f_bavail=self.available if Path(path)==self.guest else 200*1024**3,f_frsize=1)
        self.stack.enter_context(patch.object(m.os,'statvfs',side_effect=statvfs))
        self.host=self.stack.enter_context(patch.object(l1,'capacity',return_value={'available_bytes':200*1024**3,'required_bytes':138*1024**3}))
        self.t.remote=Mock(side_effect=self.remote_probe)
        self.evidence=self.output/'capacity.json'

    def remote_probe(self,command,**kw):
        parts=shlex.split(command)
        self.assertEqual(parts[:5],['sudo','-n','python3','-B','-c'])
        self.assertEqual(kw['timeout'],30)
        buf=io.StringIO()
        with redirect_stdout(buf):exec(compile(parts[5],'<guest-capacity>','exec'),{})
        return subprocess.CompletedProcess([],0,buf.getvalue(),'')

    def check(self,pending=0,downloads=0):
        return self.t.capacity_guard(self.build,pending,self.evidence,download_count=downloads)

    def test_two_copies_and_full_parent_data_headers_upload_and_reserve(self):
        pending=1234567;proof=self.check(pending)
        g=proof['guest']
        self.assertEqual(g['full_copy_count'],2)
        self.assertEqual(g['required_bytes'],2*sum(self.expected.values())+self.cache_bytes+256*1024**2+pending+10*1024**3)
        self.assertEqual(g['logical_bytes'],self.expected)
        self.assertEqual(proof['status'],'verified')
        self.assertEqual(self.evidence.stat().st_mode&0o777,0o600)
        self.host.assert_called_once_with(self.root,128,allocated=(self.root/'l1.qcow2').stat().st_blocks*512)

    def test_parent_data_larger_than_logical_is_fully_charged(self):
        p=self.headerdir/'memfile'
        size=2*sum(self.expected.values())
        with p.open('r+b') as stream:stream.truncate(size)
        self.cache_bytes=size+412000
        proof=self.check()
        self.assertEqual(proof['guest']['parent_cache_bound_bytes'],self.cache_bytes)
        self.assertGreater(proof['guest']['parent_cache_bound_bytes'],sum(self.expected.values()))
        self.assertEqual(proof['guest']['required_bytes'],2*sum(self.expected.values())+self.cache_bytes+256*1024**2+10*1024**3)

    def test_parent_data_symlink_rejected(self):
        p=self.headerdir/'memfile';p.unlink();p.symlink_to(self.root/'l1.qcow2')
        with self.assertRaisesRegex(AssertionError,'independent'):self.check()

    def test_missing_parent_file_rejected(self):
        (self.headerdir/'memfile').unlink()
        with self.assertRaisesRegex(AssertionError,'file set'):self.check()

    def test_unknown_build_directory_rejected(self):
        (self.guest/'templates/not-a-uuid').mkdir()
        with self.assertRaises(ValueError):self.check()

    def test_explicit156_capacity_is_supported_without_changing_inner_sizes(self):
        self.asset.write_text(json.dumps({'disk_size_gib':156}))
        d=json.loads(self.life.read_text());d['asset_manifest']['sha256']=hashlib.sha256(self.asset.read_bytes()).hexdigest();d['reconstruction']['disk_size_gib']=156;self.life.write_text(json.dumps(d))
        self.assertEqual(self.check()['guest']['logical_bytes'],self.expected)
        self.assertEqual(self.host.call_args.args[1],156)

    def test_larger_logical_disk_increases_metadata_budget(self):
        self.expected['rootfs.ext4.header']=64*1024**3
        d=json.loads(self.fresh.read_text())
        d['snapshot_closure']['builds'][self.build]['headers']=[dict(file=k,logical_bytes=v) for k,v in self.expected.items()]
        self.fresh.write_text(json.dumps(d))
        p=self.headerdir/'rootfs.ext4.header'
        p.write_bytes(struct.pack('<QQQQ16s16s',3,4096,self.expected['rootfs.ext4.header'],1,uuid.UUID(self.build).bytes,uuid.UUID(self.build).bytes))
        self.available=200*1024**3
        g=self.check()['guest']
        expected=2*sum((n//4096)*40+64 for n in self.expected.values())+32*1024**2
        self.assertGreater(expected,256*1024**2)
        self.assertEqual(g['metadata_reserve_bytes'],expected)

    def test_lifecycle_disk_manifest_mismatch_rejected(self):
        d=json.loads(self.life.read_text());d['reconstruction']['disk_size_gib']=156
        self.life.write_text(json.dumps(d))
        with self.assertRaisesRegex(ValueError,'disk capacity identity'):self.check()
        self.t.remote.assert_not_called()

    def test_cache_hardlink_rejected(self):
        os.link(self.headerdir/'memfile',self.root/'foreign-link')
        with self.assertRaisesRegex(AssertionError,'independent'):self.check()

    def test_declared_response_outputs_reserve_one_rootfs_each(self):
        g=self.check(17,downloads=2)['guest']
        self.assertEqual(g['download_count'],2)
        self.assertEqual(g['download_bound_bytes'],2*self.expected['rootfs.ext4.header'])
        self.assertEqual(g['required_bytes'],2*sum(self.expected.values())+self.cache_bytes+2*self.expected['rootfs.ext4.header']+256*1024**2+17+10*1024**3)

    def test_download_reserve_one_byte_short_rejects(self):
        self.available=2*sum(self.expected.values())+self.cache_bytes+self.expected['rootfs.ext4.header']+256*1024**2+10*1024**3-1
        with self.assertRaisesRegex(ValueError,'Insufficient'):self.check(downloads=1)

    def test_noninteger_download_count_rejected_before_guest(self):
        with self.assertRaisesRegex(ValueError,'download count'):self.check(downloads=True)
        self.t.remote.assert_not_called()

    def test_host_capacity_failure_precedes_guest(self):
        self.host.side_effect=ValueError('Insufficient full-growth capacity')
        with self.assertRaisesRegex(ValueError,'full-growth'):self.check()
        self.t.remote.assert_not_called()
        data=json.loads(self.evidence.read_text())
        self.assertEqual(data['status'],'failed');self.assertIn('host_observed',data)

    def test_guest_one_byte_short_rejected_with_actual_values(self):
        self.available=2*sum(self.expected.values())+self.cache_bytes+256*1024**2+10*1024**3-1
        with self.assertRaisesRegex(ValueError,'Insufficient'):self.check()
        d=json.loads(self.evidence.read_text());self.assertEqual(d['guest']['status'],'insufficient')
        self.assertEqual(d['guest']['required_bytes']-d['guest']['available_bytes'],1)

    def test_guest_exact_boundary_passes(self):
        self.available=2*sum(self.expected.values())+self.cache_bytes+256*1024**2+10*1024**3
        self.assertEqual(self.check()['status'],'verified')

    def test_pending_upload_is_charged(self):
        self.available=2*sum(self.expected.values())+self.cache_bytes+256*1024**2+10*1024**3
        with self.assertRaisesRegex(ValueError,'Insufficient'):self.check(1)

    def test_header_uuid_mismatch_rejected(self):
        p=self.headerdir/'memfile.header';p.write_bytes(struct.pack('<QQQQ16s16s',3,4096,self.expected['memfile.header'],1,uuid.uuid4().bytes,uuid.uuid4().bytes))
        with self.assertRaisesRegex(AssertionError,'identity'):self.check()
        self.assertEqual(json.loads(self.evidence.read_text())['status'],'failed')

    def test_header_logical_mismatch_rejected(self):
        p=self.headerdir/'rootfs.ext4.header';p.write_bytes(struct.pack('<QQQQ16s16s',3,4096,4096,1,uuid.UUID(self.build).bytes,uuid.UUID(self.build).bytes))
        with self.assertRaisesRegex(AssertionError,'dimensions'):self.check()

    def test_header_symlink_rejected_before_read(self):
        p=self.headerdir/'memfile.header';other=self.root/'elsewhere';other.write_bytes(p.read_bytes());p.unlink();p.symlink_to(other)
        with self.assertRaisesRegex(AssertionError,'symlink'):self.check()

    def test_storage_symlink_rejected(self):
        alias=self.root/'alias';alias.symlink_to(self.guest);self.t.storage=str(alias)
        d=json.loads(self.fresh.read_text());d['guest_storage']=str(alias);self.fresh.write_text(json.dumps(d))
        with self.assertRaisesRegex(AssertionError,'canonical'):self.check()

    def test_changed_asset_manifest_rejected_before_guest(self):
        self.asset.write_text('{"disk_size_gib":80}')
        with self.assertRaisesRegex(ValueError,'manifest changed'):self.check()
        self.t.remote.assert_not_called()

    def test_guest_malformed_result_rejected_with_evidence(self):
        self.t.remote.side_effect=None;self.t.remote.return_value=subprocess.CompletedProcess([],0,'{}','')
        with self.assertRaisesRegex(ValueError,'[Ii]nvalid'):self.check()
        self.assertEqual(json.loads(self.evidence.read_text())['status'],'failed')

    def test_guest_timeout_does_not_admit(self):
        self.t.remote.side_effect=subprocess.TimeoutExpired('probe',30)
        with self.assertRaises(subprocess.TimeoutExpired):self.check()
        self.assertEqual(json.loads(self.evidence.read_text())['status'],'failed')


class CapacityDispatchOrder(Step):
    def test_success_checks_before_upload_and_resume_without_double_count(self):
        self.assertTrue(self.step()['ok'])
        self.assertEqual([x[1] for x in self.capacity_calls],[self.payload.stat().st_size,0])
        self.assertEqual([x[3] for x in self.capacity_calls],[1,1])
        self.assertEqual(self.trace[0],('capacity',self.payload.stat().st_size))
        second=self.trace.index(('capacity',0));dispatch=self.trace.index(('remote','RUN_DRIVER'))
        self.assertLess(second,dispatch)
        self.assertTrue(any(x[:2]==('copy',True) for x in self.trace[:second]))

    def test_first_capacity_failure_has_no_upload_or_resume(self):
        self.t.capacity_guard.side_effect=ValueError('not enough guest space')
        with self.assertRaisesRegex(ValueError,'guest space'):self.step()
        self.t.remote.assert_not_called();self.t.copy.assert_not_called()
        self.assertIsNone(self.command_args)

    def test_recheck_failure_keeps_upload_receipt_and_never_builds_physical_command(self):
        admitted=self.t.capacity_guard.side_effect
        def guard(build,pending,path,**kw):
            if pending==0:raise ValueError('space changed before resume')
            return admitted(build,pending,path,**kw)
        self.t.capacity_guard.side_effect=guard
        with self.assertRaisesRegex(ValueError,'before resume'):self.step()
        self.assertIsNone(self.command_args)
        self.assertFalse(any(x==('remote','RUN_DRIVER') for x in self.trace))
        receipt=json.loads(next(self.output.glob('*.transport/receipt.json')).read_text())
        self.assertFalse(receipt['physical_resume_dispatched']);self.assertFalse(receipt['ok'])
        self.assertEqual(len(receipt['uploads']),1)


if __name__=='__main__':
    unittest.main()
