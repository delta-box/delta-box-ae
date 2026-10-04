"""Offline transport tests. All SSH/SCP and owned-process queries are faked."""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
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

    def test_guest_work_must_be_canonical_owned_uuid(self):
        for root in ('/tmp/e2b-paper-'+uuid.uuid4().hex,
                     '/var/tmp/e2b-paper-../escape',
                     '/var/tmp/e2b-paper-'+'a'*31):
            with self.subTest(root=root):
                self.value['guest_root']=root
                self.write_manifest()
                with self.assertRaises(ValueError):self.transport()

    def test_selected_root_itself_rejected(self):
        self.value['host_output_root']=str(self.repo/'ae/results/selected')
        self.write_manifest()
        with self.assertRaises(ValueError):self.transport()

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


class Installation(TransportFixture):

    def test_install_without_explicit_profile_transport_fails(self):
        with patch.dict(os.environ,{},clear=True),self.assertRaises(ValueError):
            m.install(types.SimpleNamespace())


if __name__=='__main__':
    unittest.main()
