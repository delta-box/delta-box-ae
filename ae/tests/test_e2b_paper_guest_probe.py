"""Tiny offline V3 fixture proofs; no real snapshots, VM, mounts or SSH used.

Layout agrees with b1646ca header/{metadata,serialization_v3,header}.go:
64-byte little-endian metadata followed by 40-byte V3 mapping rows.
Only 4 KiB aligned, 2 GiB logical memory is accepted for this profile.
The memory fixture stores one 4 KiB block and maps the remaining range to ZERO;
it does not allocate/hash a real 2 GiB benchmark snapshot.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from ae.scripts import e2b_paper_guest_probe as m

A='11111111-1111-4111-8111-111111111111'
B='22222222-2222-4222-8222-222222222222'
C='33333333-3333-4333-8333-333333333333'
Z=str(uuid.UUID(int=0))
MEM=2048*1024**2


def header(build=A, *, size=8192, block=4096, version=3, generation=0, base=None, rows=None):
    raw=struct.pack('<QQQQ16s16s',version,block,size,generation,uuid.UUID(build).bytes,uuid.UUID(base or build).bytes)
    return raw+b''.join(struct.pack('<QQ16sQ',offset,length,uuid.UUID(source).bytes,stored)
                         for offset,length,source,stored in (rows or []))


class Header(unittest.TestCase):
    def test_exact_v3_layout_and_implicit_full_file(self):
        raw=header()
        self.assertEqual(len(raw),64)
        parsed=m.parse_header(raw,A)
        self.assertEqual(parsed['mapping_count'],1)
        self.assertEqual(parsed['referenced_builds'],{A:{'mapped_bytes':8192,'required_file_bytes':8192}})

    def test_mapping_stored_offsets_require_full_physical_extent(self):
        raw=header(rows=[(0,4096,B,12288),(4096,4096,B,8192)])
        self.assertEqual(len(raw),144)
        parsed=m.parse_header(raw,A)
        self.assertEqual(parsed['referenced_builds'][B],{'mapped_bytes':8192,'required_file_bytes':16384})

    def test_zero_mapping_is_not_a_parent_file(self):
        parsed=m.parse_header(header(rows=[(0,8192,Z,0)]),A)
        self.assertFalse(parsed['referenced_builds'])

    def test_generation_and_base_identity_are_retained(self):
        parsed=m.parse_header(header(generation=19,base=B),A)
        self.assertEqual(parsed['generation'],19)
        self.assertEqual(parsed['metadata_base_build'],B)
        self.assertNotIn(B,parsed['referenced_builds'])

    def test_bad_lengths_denied(self):
        for raw in (b'',b'x'*63,header()+b'x',header()+b'x'*39):
            with self.subTest(n=len(raw)),self.assertRaises(ValueError):m.parse_header(raw,A)

    def test_wrong_version_block_build_size_denied(self):
        for kw in ({'version':4},{'block':512},{'size':0},{'size':4097},{'build':B}):
            with self.subTest(kw=kw),self.assertRaises(ValueError):m.parse_header(header(**kw),A)

    def test_gap_denied(self):
        with self.assertRaises(ValueError):
            m.parse_header(header(rows=[(0,4096,A,0),(8192,4096,B,0)]),A)

    def test_overlap_denied(self):
        with self.assertRaises(ValueError):
            m.parse_header(header(rows=[(0,8192,A,0),(4096,4096,B,0)]),A)

    def test_incomplete_coverage_denied(self):
        with self.assertRaises(ValueError):
            m.parse_header(header(rows=[(0,4096,A,0)]),A)

    def test_zero_length_and_misaligned_mapping_denied(self):
        for rows in ([(0,0,A,0)],[(0,8192,A,1)],[(0,8191,A,0)],[(1,8192,A,0)]):
            with self.subTest(rows=rows),self.assertRaises(ValueError):
                m.parse_header(header(rows=rows),A)


class ClosureFixture(unittest.TestCase):
    def setUp(self):
        self.root=Path('/var/tmp')/('e2b-paper-'+uuid.uuid4().hex)
        self.storage=self.root/'storage'
        self.templates=self.storage/'templates'
        self.templates.mkdir(parents=True)
        self.addCleanup(shutil.rmtree,self.root)
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        self.mount={'target':'/','source':'/dev/vda1','fstype':'ext4','options':'rw,relatime'}
        self.run=self.stack.enter_context(patch.object(m.subprocess,'run',side_effect=self.findmnt))
        self.stack.enter_context(patch.object(m.os,'statvfs',return_value=type('FS',(),{'f_bavail':100,'f_frsize':4096})()))

    def findmnt(self,args,**kw):
        self.assertEqual(args,['findmnt','-J','-T',str(self.storage),'-o','TARGET,SOURCE,FSTYPE,OPTIONS'])
        return subprocess.CompletedProcess(args,0,json.dumps({'filesystems':[self.mount]}),'')

    def add(self,build,*,parent=None):
        p=self.templates/build
        p.mkdir()
        (p/'metadata.json').write_text(json.dumps({'template':{'build_id':build,
                'kernel_version':'vmlinux-6.1.158','firecracker_version':'v1.14.1_458ca91'},'from_image':True}))
        (p/'snapfile').write_bytes(b'fixture FC state')
        (p/'memfile').write_bytes(b'm'*4096)
        (p/'rootfs.ext4').write_bytes(b'r'*8192)
        (p/'memfile.header').write_bytes(header(build,size=MEM,base=parent or build,
                  rows=[(0,4096,parent or build,0),(4096,MEM-4096,Z,0)]))
        (p/'rootfs.ext4.header').write_bytes(header(build,base=parent or build,
                  rows=[(0,8192,parent or build,0)]))
        return p

    def snapshot(self):
        return {str(p.relative_to(self.root)):(p.read_bytes(),p.stat().st_mtime_ns,p.stat().st_ino)
                for p in self.root.rglob('*') if p.is_file()}

    def capture(self,builds=(A,)):
        return m.capture(self.storage,list(builds))


class Closure(ClosureFixture):
    def test_single_base_all_six_files_proven_unchanged(self):
        self.add(A)
        before=self.snapshot()
        result=self.capture()
        self.assertEqual(result['status'],'verified')
        self.assertEqual(result['build_count'],1)
        self.assertEqual(len(result['files']),6)
        self.assertEqual(set(Path(r['path']).name for r in result['files']),set(m.FILES))
        for row in result['files']:
            self.assertEqual(row['sha256'],hashlib.sha256(Path(row['path']).read_bytes()).hexdigest())
        self.assertEqual(before,self.snapshot())

    def test_transitive_mapping_parents_have_all_six_files(self):
        self.add(B)
        self.add(A,parent=B)
        self.add(C,parent=A)
        before=self.snapshot()
        result=self.capture((C,))
        self.assertEqual(set(result['builds']),{A,B,C})
        self.assertEqual(result['build_count'],3)
        self.assertEqual(len(result['files']),18)
        self.assertEqual(result['requested_builds'],[C])
        self.assertEqual(before,self.snapshot())

    def test_multiple_roots_do_not_duplicate_parent_files(self):
        self.add(B)
        self.add(A,parent=B)
        self.add(C,parent=B)
        result=self.capture((A,C))
        self.assertEqual(len(result['files']),18)

    def test_empty_closure_denied(self):
        with self.assertRaisesRegex(ValueError,'Empty'):self.capture(())

    def test_missing_transitive_parent_denied(self):
        self.add(A,parent=B)
        with self.assertRaises((ValueError,FileNotFoundError)):self.capture()

    def test_parent_extent_too_short_denied(self):
        p=self.add(B)
        self.add(A,parent=B)
        (p/'rootfs.ext4').write_bytes(b'short')
        with self.assertRaisesRegex(ValueError,'shorter'):self.capture()

    def test_missing_any_of_six_files_denied(self):
        p=self.add(A)
        (p/'snapfile').unlink()
        with self.assertRaises(FileNotFoundError):self.capture()

    def test_metadata_build_and_runtime_mismatch_denied(self):
        p=self.add(A)
        for key,value in (('build_id',B),('kernel_version','new-kernel'),('firecracker_version','latest')):
            original=json.loads((p/'metadata.json').read_text())
            changed=json.loads(json.dumps(original))
            changed['template'][key]=value
            (p/'metadata.json').write_text(json.dumps(changed))
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'metadata mismatch'):self.capture()
            (p/'metadata.json').write_text(json.dumps(original))

    def test_wrong_memory_size_denied(self):
        p=self.add(A)
        (p/'memfile.header').write_bytes(header(A,size=4096,rows=[(0,4096,A,0)]))
        with self.assertRaisesRegex(ValueError,'2048MiB'):self.capture()

    def test_noncanonical_requested_uuid_denied(self):
        self.add(A)
        with self.assertRaises(ValueError):self.capture((A.replace('-',''),))

    def test_storage_root_canonical_hex_required(self):
        bad=Path('/var/tmp/e2b-paper-'+str(uuid.uuid4()))/'storage'
        with self.assertRaises(ValueError):m.capture(bad,[A])

    def test_wrong_storage_root_denied(self):
        with self.assertRaises(ValueError):m.capture('/tmp/e2b-paper-'+uuid.uuid4().hex+'/storage',[A])

    def test_snapshot_directory_symlink_denied(self):
        real=self.add(B)
        (self.templates/A).symlink_to(real)
        with self.assertRaises(ValueError):self.capture()

    def test_snapshot_file_symlink_denied(self):
        p=self.add(A)
        (p/'snapfile').unlink()
        (p/'snapfile').symlink_to(p/'memfile')
        with self.assertRaises(ValueError):self.capture()

    def test_snapshot_hardlink_denied(self):
        p=self.add(A)
        (p/'snapfile').unlink()
        os.link(p/'memfile',p/'snapfile')
        with self.assertRaises(ValueError):self.capture()

    def test_snapshot_fifo_denied_without_open(self):
        p=self.add(A)
        (p/'snapfile').unlink()
        os.mkfifo(p/'snapfile')
        with self.assertRaises(ValueError):self.capture()

    def test_ram_and_share_filesystems_rejected(self):
        self.add(A)
        for kind in ('tmpfs','ramfs','9p'):
            self.mount['fstype']=kind
            with self.subTest(kind=kind),self.assertRaises(ValueError):self.capture()

    def test_parse_after_hash_change_is_detected(self):
        p=self.add(A)
        original=m.parse_header
        changed=False
        def parse(raw,build):
            nonlocal changed
            result=original(raw,build)
            if not changed:
                changed=True
                metadata=json.loads((p/'metadata.json').read_text())
                metadata['from_image']=False
                (p/'metadata.json').write_text(json.dumps(metadata))
            return result
        with patch.object(m,'parse_header',side_effect=parse):
            with self.assertRaisesRegex(ValueError,'changed|differs|stable'):
                self.capture()

    def test_write_same_bytes_after_hash_is_detected(self):
        p=self.add(A)
        original=m.parse_header
        changed=False
        def parse(raw,build):
            nonlocal changed
            result=original(raw,build)
            if not changed:
                changed=True
                file=p/'snapfile'
                raw_bytes=file.read_bytes()
                prior=file.stat()
                file.write_bytes(raw_bytes)
                # Make the timestamp change deterministic even on coarse clocks.
                os.utime(file,ns=(prior.st_atime_ns,prior.st_mtime_ns+1000000))
            return result
        with patch.object(m,'parse_header',side_effect=parse):
            with self.assertRaisesRegex(ValueError,'changed|differs|stable'):
                self.capture()

    def test_snapshot_modification_during_hash_detected(self):
        p=self.add(A)
        target=p/'snapfile'
        original=Path.open
        class MutatingReader:
            def __init__(self,wrapped):self.wrapped=wrapped;self.did=False
            def __enter__(self):self.wrapped.__enter__();return self
            def __exit__(self,*args):return self.wrapped.__exit__(*args)
            def read(self,n):
                data=self.wrapped.read(n)
                if data and not self.did:
                    self.did=True
                    with original(target,'ab') as f:f.write(b'changed')
                return data
        def opened(path,*a,**kw):
            f=original(path,*a,**kw)
            return MutatingReader(f) if path==target and a and a[0]=='rb' else f
        with patch.object(Path,'open',opened):
            with self.assertRaisesRegex(ValueError,'changed while hashing'):m.file_record(target)

    def test_only_expected_writable_guest_virtual_root_disk_accepted(self):
        self.add(A)
        for change in ({'fstype':'overlay'},{'source':'/dev/sda1'},
                       {'source':'/dev/vda2'},{'target':str(self.storage)},
                       {'options':'ro,relatime'}):
            before=dict(self.mount)
            self.mount.update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):self.capture()
            self.mount=before



if __name__=='__main__':
    unittest.main()
