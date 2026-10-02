import importlib.util
import json
import os
from pathlib import Path
import struct
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch,Mock
import uuid

source=Path(__file__).resolve().parents[2]/'ae/scripts/e2b_working_storage.py'
if not source.is_file():source=Path(__file__).with_name('e2b_working_storage.py')
spec=importlib.util.spec_from_file_location('e2b_working_storage',source)
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
real_readlink=os.readlink


def short_workspace():
    # A canonical short path also exercises the Linux UDS limit on macOS.
    return tempfile.TemporaryDirectory(prefix='e',dir='/tmp')


def host_namespace(path):
    if str(path) in ('/proc/self/ns/mnt','/proc/1/ns/mnt'):
        return 'mnt:host'
    return real_readlink(path)


def header(build, refs=(), version=3, size=2*s.GIB):
    row=struct.pack('<QQQQ16s16s',version,4096,size,0,uuid.UUID(build).bytes,uuid.UUID(build).bytes)
    for i,ref in enumerate(refs or (build,)):
        row+=struct.pack('<QQ16sQ',i*4096,4096,uuid.UUID(ref).bytes,0)
    return row


class WorkingStorageTests(unittest.TestCase):
    def assets(self,templates,build,refs=()):
        folder=templates/build;folder.mkdir(parents=True)
        for kind in ('memfile','rootfs.ext4'):
            (folder/(kind+'.header')).write_bytes(header(build,refs))
            (folder/kind).write_bytes(b'asset')
        (folder/'metadata.json').write_text('{}');(folder/'snapfile').write_bytes(b'snapshot')
        return folder
    def fixture(self,root):
        root=root.resolve()
        (root/'ae/work').mkdir(parents=True)
        paths={key:root/'old'/key for key in s.PATH_KEYS}
        for path in paths.values():path.mkdir(parents=True)
        self.assets(paths['LOCAL_TEMPLATE_STORAGE_BASE_PATH'],s.BASE)
        cfg={'e2b':{'template':s.TEMPLATE}}
        (root/'cgroup').mkdir()
        with patch.object(s.os,'geteuid',return_value=0):
            value=s.WorkingStorage(cfg,root/'job/environment/e2b-storage',0,'0-3','a'*64,
                root=root,units=('registered.service',),vm_root=root/'cgroup')
        with patch.object(s,'paths_from_process',return_value=paths),patch.object(s.os,'readlink',side_effect=host_namespace), \
                patch.object(s,'process_tmpdir',return_value=None),patch.object(s,'visible_mount',return_value={'mount':{'fstype':'ext4','source':'/dev/old','maj:min':'1:0'}}):
            value.admit({'units':{'ae-e2b-orchestrator.service':{'process':{'pid':7}}},
                         'vm_root':{'inode':(root/'cgroup').stat().st_ino}})
        return value,paths
    def test_exact_transitive_v3_closure_excludes_unrelated_old_snapshots(self):
        with short_workspace() as d:
            root=Path(d);parent=str(uuid.uuid4());unrelated=str(uuid.uuid4())
            self.assets(root,s.BASE,(s.BASE,parent));self.assets(root,parent);self.assets(root,unrelated)
            value=s.dependency_closure(root)
            self.assertEqual(set(value['build_ids']),{s.BASE,parent})
            self.assertEqual(len(value['builds']),2)
    def test_unknown_header_format_missing_parent_or_symlink_is_rejected(self):
        with short_workspace() as d:
            root=Path(d);folder=self.assets(root,s.BASE)
            for raw in (header(s.BASE,version=4),header(s.BASE)[:-1],header(s.BASE,(str(uuid.uuid4()),))):
                (folder/'memfile.header').write_bytes(raw)
                with self.assertRaises((RuntimeError,FileNotFoundError)):s.dependency_closure(root)
            (folder/'memfile.header').write_bytes(header(s.BASE));(folder/'memfile').unlink();(folder/'memfile').symlink_to(folder/'snapfile')
            with self.assertRaisesRegex(RuntimeError,'symlink'):s.dependency_closure(root)
    def test_geometry_and_reserve_are_not_reduced_to_fit_small_volume(self):
        with short_workspace() as d:
            value,paths=self.fixture(Path(d));value.size=4
            with patch.object(s,'paths_from_process',return_value=paths),patch.object(s.os,'readlink',side_effect=host_namespace), \
                    patch.object(s,'process_tmpdir',return_value=None),patch.object(s,'visible_mount',return_value={'mount':{'fstype':'ext4','source':'/dev/old','maj:min':'1:0'}}):
                with self.assertRaisesRegex(RuntimeError,'full next memory snapshot'):value.admit({'units':{'ae-e2b-orchestrator.service':{'process':{'pid':7}}},'vm_root':{'inode':value.vm_root.stat().st_ino}})
    def test_all_four_nodes_use_the_same_storage_method_and_volume(self):
        with patch.object(s.os,'geteuid',return_value=0):
            for node,cpus in ((0,'0-3'),(1,'28-31'),(2,'48-51'),(3,'72-75')):
                value=s.WorkingStorage({'e2b':{'template':s.TEMPLATE}},Path('/fake'),node,cpus,'a'*64,
                    root=Path('/fake'),units=(),vm_root=Path('/fake'))
                self.assertEqual((value.node,value.cpus,value.size),(node,cpus,24))
        for uid,node in ((1000,1),(0,4),(0,True)):
            with patch.object(s.os,'geteuid',return_value=uid),self.assertRaisesRegex(RuntimeError,'requires root'):
                s.WorkingStorage({'e2b':{'template':s.TEMPLATE}},Path('/fake'),node,'28-31','a'*64,
                    root=Path('/fake'),units=(),vm_root=Path('/fake'))

    def test_hosted_socket_path_fits_without_creating_directories(self):
        root=Path('/home/atc-ae/delta-box-ae')
        out=root/'ae/results/selected'/('long-result-name-'*20)/'job/environment/e2b-storage'
        with patch.object(s.os,'geteuid',return_value=0),patch.object(Path,'mkdir') as mkdir:
            value=s.WorkingStorage({'e2b':{'template':s.TEMPLATE}},out,0,'0-3','a'*64,
                root=root,units=(),vm_root=root/'cg')
        mkdir.assert_not_called()
        self.assertEqual(value.ram.parent,root/'ae/work')
        self.assertRegex(value.ram.name,r'^et-[0-9a-f]{10}$')
        worst=value.ram/'tmp'/s.UFFD_SOCKET_NAME
        self.assertEqual(len(str(worst).encode('utf-8')),105)
        self.assertLessEqual(len(str(worst).encode('utf-8')),107)
        self.assertEqual(value.out,out)

    def test_oversize_ascii_and_utf8_roots_fail_before_any_creation(self):
        for root in (Path('/'+'r'*40),Path('/'+'é'*14)):
            with patch.object(s.os,'geteuid',return_value=0),patch.object(Path,'mkdir') as mkdir:
                with self.assertRaisesRegex(RuntimeError,'107-byte limit'):
                    s.WorkingStorage({'e2b':{'template':s.TEMPLATE}},root/'job',0,'0-3','a'*64,
                        root=root,units=(),vm_root=root/'cg')
            mkdir.assert_not_called()

    def test_existing_short_workspace_is_not_overwritten(self):
        with short_workspace() as d:
            value,paths=self.fixture(Path(d));value.ram.mkdir();marker=value.ram/'keep';marker.write_text('old')
            helper,cap,run=self.prepare_mocks(value,paths)
            with patch.dict(sys.modules,{'ae.scripts.run_memory_job':helper,'ae.vendor.finalbench.fc_diff_dm.fc_capacity':cap}),patch.object(value,'assert_stopped'),patch.object(s.subprocess,'run',side_effect=run):
                with self.assertRaises(FileExistsError):value.prepare_stopped()
            self.assertEqual(marker.read_text(),'old')
            self.assertEqual(value.mounts,[])

    def test_short_ram_workspace_uses_existing_identity_cleanup(self):
        with short_workspace() as d:
            value,paths=self.fixture(Path(d));helper,cap,run=self.prepare_mocks(value,paths)
            with patch.dict(sys.modules,{'ae.scripts.run_memory_job':helper,'ae.vendor.finalbench.fc_diff_dm.fc_capacity':cap}),patch.object(value,'assert_stopped'),patch.object(s.subprocess,'run',side_effect=run):
                value.prepare_stopped()
            self.assertIn(str(value.ram),value.directories)
            def unmount(record):
                path=Path(record['target'])
                if path in (value.ram,value.alias):
                    for child in path.iterdir():
                        if child.is_dir():shutil.rmtree(child)
                        else:child.unlink()
                record['unmounted']=True
            with patch.dict(sys.modules,{'ae.scripts.run_memory_job':types.SimpleNamespace(unmount_owned=unmount)}),patch.object(value,'assert_stopped'):
                value.restore_stopped()
            self.assertFalse(value.ram.exists());self.assertFalse(value.alias.exists())
            self.assertTrue(value.ram.parent.is_dir())
            before=json.loads((value.out/'before.json').read_text())
            restored=json.loads((value.out/'restored.json').read_text())
            self.assertEqual(before['ephemeral_paths'],[str(value.ram),str(value.alias)])
            self.assertEqual(restored['ephemeral_paths'],before['ephemeral_paths'])
            self.assertTrue(restored['ephemeral_paths_gone'])
    def test_stop_failure_prevents_prepare_shared_writes(self):
        with short_workspace() as d:
            value,paths=self.fixture(Path(d))
            with patch.object(value,'assert_stopped',side_effect=RuntimeError('stop failed')),patch.object(s.subprocess,'run') as run:
                with self.assertRaisesRegex(RuntimeError,'stop failed'):value.prepare_stopped()
                run.assert_not_called();self.assertFalse(value.ram.exists())
    def test_restore_stop_failure_does_not_unmount_or_remove_paths(self):
        with short_workspace() as d:
            value,paths=self.fixture(Path(d));value.ram.mkdir();(value.ram/'marker').write_text('keep')
            with patch.object(value,'assert_stopped',side_effect=RuntimeError('still populated')),patch.object(s.subprocess,'run') as run:
                with self.assertRaisesRegex(RuntimeError,'populated'):value.restore_stopped()
                run.assert_not_called();self.assertEqual((value.ram/'marker').read_text(),'keep')
    def test_first_failed_restore_layer_preserves_parent_and_original_disk(self):
        with short_workspace() as d:
            value,paths=self.fixture(Path(d));value.mounts=[{'target':'alias'},{'target':'ram'},{'target':'top'}]
            old=paths['SANDBOX_CACHE_DIR']/'foreign-marker';old.write_text('untouched')
            unmount=Mock(side_effect=RuntimeError('busy'))
            helper=types.SimpleNamespace(unmount_owned=unmount)
            with patch.dict(sys.modules,{'ae.scripts.run_memory_job':helper}),patch.object(value,'assert_stopped'):
                with self.assertRaisesRegex(RuntimeError,'busy'):value.restore_stopped()
            self.assertEqual(unmount.call_count,1);self.assertEqual(unmount.call_args.args[0]['target'],'top')
            self.assertEqual(old.read_text(),'untouched');self.assertEqual(json.loads((value.out/'retained.json').read_text())['status'],'recovery-required')
    def test_original_directory_inode_drift_rejects_cleanup(self):
        with short_workspace() as d:
            value,paths=self.fixture(Path(d));path=paths['SANDBOX_CACHE_DIR'];path.rename(path.with_name('original-retained'));path.mkdir()
            with patch.dict(sys.modules,{'ae.scripts.run_memory_job':types.SimpleNamespace(unmount_owned=Mock())}),patch.object(value,'assert_stopped'):
                with self.assertRaisesRegex(RuntimeError,'did not restore'):value.restore_stopped()
            self.assertTrue(path.with_name('original-retained').is_dir())
    def test_original_directory_contents_mtime_is_not_falsely_treated_as_inode_drift(self):
        with short_workspace() as d:
            value,paths=self.fixture(Path(d));path=paths['ORCHESTRATOR_BASE_PATH'];(path/'normal-stop-cache-change').write_text('x')
            self.assertTrue(s.same_directory(path,value.original[str(path)]))
    def test_unknown_preexisting_alias_is_never_recursively_removed(self):
        with short_workspace() as d:
            value,paths=self.fixture(Path(d));value.alias.mkdir();(value.alias/'foreign').write_text('x')
            with patch.dict(sys.modules,{'ae.scripts.run_memory_job':types.SimpleNamespace(unmount_owned=Mock())}),patch.object(value,'assert_stopped'):
                with self.assertRaisesRegex(RuntimeError,'Unexpected E2B RAM/alias'):value.restore_stopped()
            self.assertEqual((value.alias/'foreign').read_text(),'x')

    def test_existing_receipt_directory_or_symlink_is_rejected_before_mutation(self):
        with short_workspace() as d:
            root=Path(d).resolve();out=root/'existing';out.mkdir();(out/'marker').write_text('keep')
            with patch.object(s.os,'geteuid',return_value=0):
                value=s.WorkingStorage({'e2b':{'template':s.TEMPLATE}},out,0,'0-3','a'*64,
                    root=root,units=(),vm_root=root/'cg')
            with self.assertRaisesRegex(RuntimeError,'Existing or symlinked'):value.save('before.json',{})
            self.assertEqual((out/'marker').read_text(),'keep')
            link=root/'link';link.symlink_to(out)
            value.out=link
            with self.assertRaisesRegex(RuntimeError,'Existing or symlinked'):value.save('before.json',{})

    def test_receipt_directory_drift_does_not_write_foreign_target(self):
        with short_workspace() as d:
            value,paths=self.fixture(Path(d));old=value.out.with_name('retained');value.out.rename(old);value.out.mkdir()
            with self.assertRaisesRegex(RuntimeError,'directory identity changed'):value.save('prepared.json',{})
            self.assertFalse((value.out/'prepared.json').exists())

    def test_vm_root_inode_drift_blocks_before_daemon_queries(self):
        with short_workspace() as d:
            value,paths=self.fixture(Path(d));value.vm_root.rename(value.vm_root.with_name('old-cgroup'));value.vm_root.mkdir()
            with patch.object(s.os,'readlink',side_effect=host_namespace),patch.object(s.subprocess,'check_output') as query:
                with self.assertRaisesRegex(RuntimeError,'VM root identity changed'):value.assert_stopped()
                query.assert_not_called()

    def test_original_template_content_drift_rejects_restart_permission(self):
        with short_workspace() as d:
            value,paths=self.fixture(Path(d));path=paths['LOCAL_TEMPLATE_STORAGE_BASE_PATH']/s.BASE/'snapfile'
            path.write_bytes(b'changed')
            with patch.dict(sys.modules,{'ae.scripts.run_memory_job':types.SimpleNamespace(unmount_owned=Mock())}),patch.object(value,'assert_stopped'):
                with self.assertRaisesRegex(RuntimeError,'template file identity changed'):value.restore_stopped()

    def test_actual_tmpdir_must_be_own_ram_not_default_disk(self):
        with short_workspace() as d:
            value,paths=self.fixture(Path(d))
            with patch.object(s,'paths_from_process',return_value=paths),patch.object(s,'process_tmpdir',return_value=None),patch.object(s,'visible_mount') as observe:
                with self.assertRaisesRegex(RuntimeError,'TMPDIR differs'):value.verify_active({'process':{'pid':7,'start_ticks':99}})
                observe.assert_not_called()

    def prepare_mocks(self,value,paths,fail_binding=False):
        def bind(source,target,records):
            records.append({'target':str(target),'source':str(source)})
            if target==value.alias:
                for build in value.closure['builds']:
                    folder=target/build['build_id'];folder.mkdir()
                    for file in build['files']:os.link(file['path'],folder/Path(file['path']).name)
            elif fail_binding:
                raise RuntimeError('partial bind failed')
        def mount(argv,target,records):records.append({'target':str(target),'source':'tmpfs'})
        def mount_info(path):
            return {'fstype':'tmpfs','options':'ro' if path==value.alias else 'rw,noswap,mpol=bind:0'}
        def capacity(path,phase,required,output,**kwargs):
            with Path(output).open('a') as stream:stream.write(json.dumps({'phase':phase,'required':required,'node':kwargs['node']})+'\n')
        def run(argv,**kwargs):
            if argv[0]=='cp':shutil.copyfile(argv[-2],argv[-1])
        helper=types.SimpleNamespace(bind_private=bind,mount_private=mount,mount_info=mount_info)
        cap=types.SimpleNamespace(check_capacity=capacity)
        return helper,cap,run

    def test_staging_copies_only_closure_without_changing_old_bytes_and_keeps_capacity_geometry(self):
        with short_workspace() as d:
            value,paths=self.fixture(Path(d));helper,cap,run=self.prepare_mocks(value,paths)
            old={str(p):p.read_bytes() for p in paths['LOCAL_TEMPLATE_STORAGE_BASE_PATH'].rglob('*') if p.is_file()}
            with patch.dict(sys.modules,{'ae.scripts.run_memory_job':helper,'ae.vendor.finalbench.fc_diff_dm.fc_capacity':cap}),patch.object(value,'assert_stopped'),patch.object(s.subprocess,'run',side_effect=run):
                value.prepare_stopped()
            copied=value.ram/'local_template_storage_base_path'/s.BASE
            self.assertEqual(set(p.name for p in copied.iterdir()),set(Path(p).name for p in old))
            for text,raw in old.items():self.assertEqual(Path(text).read_bytes(),raw)
            prepared=json.loads((value.out/'prepared.json').read_text())
            self.assertEqual(prepared['job'],str(value.out.parent.parent))
            self.assertEqual([r['node'] for r in prepared['capacity_checks']],[0,0])
            self.assertEqual(prepared['capacity_checks'][1]['required'],2*s.GIB)
            self.assertTrue(all('sha256' in f for b in prepared['dependency_closure']['builds'] for f in b['files']))

    def test_partial_bind_failure_retains_all_owned_layer_records_for_safe_restore(self):
        with short_workspace() as d:
            value,paths=self.fixture(Path(d));helper,cap,run=self.prepare_mocks(value,paths,fail_binding=True)
            with patch.dict(sys.modules,{'ae.scripts.run_memory_job':helper,'ae.vendor.finalbench.fc_diff_dm.fc_capacity':cap}),patch.object(value,'assert_stopped'),patch.object(s.subprocess,'run',side_effect=run):
                with self.assertRaisesRegex(RuntimeError,'partial bind'):value.prepare_stopped()
            self.assertEqual(len(value.mounts),3)
            self.assertEqual(value.mounts[0]['target'],str(value.alias))
            self.assertEqual(value.mounts[1]['target'],str(value.ram))
            self.assertEqual(value.mounts[2]['target'],str(paths['LOCAL_BUILD_CACHE_STORAGE_BASE_PATH']))
            self.assertFalse((value.out/'prepared.json').exists())


if __name__=='__main__':unittest.main()
