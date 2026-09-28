"""Allocation accounting may settle after content is immutable."""
import copy
import json
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch
from ae.scripts import e2b_paper_suite as suite
from ae.scripts import e2b_paper_guest_probe as probe
from ae.tests.test_e2b_paper_suite import SuiteFixture, closure_fixture
from ae.tests.test_e2b_paper_guest_probe import ClosureFixture, A

class SnapshotIdentity(unittest.TestCase):
    def setUp(self):
        self.base=str(uuid.uuid4())
        self.storage='/var/tmp/e2b-paper-'+'a'*32+'/storage'
        self.before=closure_fixture(self.storage,self.base)
        for row in self.before['files']:
            row.update(allocated_bytes=4096,mode=384,inode=123)
        self.after=copy.deepcopy(self.before)
        self.after['files'][2]['allocated_bytes']+=4096
        self.all=copy.deepcopy(self.after)
        self.all['requested_builds']=list(self.all['builds'])
    def test_only_allocation_change_keeps_raw_observations(self):
        saved=copy.deepcopy((self.before,self.after,self.all))
        result=suite.validate_post_closure(self.before,self.after,self.all,self.storage,self.base,[])
        self.assertEqual(len(result['all_build_ids']),3)
        self.assertEqual((self.before,self.after,self.all),saved)
    def test_every_content_and_identity_field_still_rejected(self):
        for key,value in [('sha256','f'*64),('bytes',8192),('mtime_ns',2),('ctime_ns',2),('mode',420),('inode',124)]:
            with self.subTest(key=key):
                after=copy.deepcopy(self.after);after['files'][2][key]=value
                with self.assertRaises(ValueError):
                    suite.validate_post_closure(self.before,after,self.all,self.storage,self.base,[])
    def test_all_builds_cannot_hide_content_change_with_allocation_change(self):
        self.all['files'][2]['sha256']='f'*64
        with self.assertRaises(ValueError):
            suite.validate_post_closure(self.before,self.after,self.all,self.storage,self.base,[])

class ProbeAllocation(ClosureFixture):
    def test_second_hash_allocation_change_does_not_mask_or_overwrite_evidence(self):
        self.add(A);original=probe.file_record;calls={};first={}
        def observed(path):
            row=original(path);key=str(path);calls[key]=calls.get(key,0)+1
            if calls[key]==1:first[key]=copy.deepcopy(row)
            else:row['allocated_bytes']+=4096
            return row
        with patch.object(probe,'file_record',side_effect=observed):
            result=self.capture()
        self.assertEqual(result['files'],list(first.values()))
        self.assertTrue(all(n==2 for n in calls.values()))

class SuiteAllocation(SuiteFixture):
    def test_sync_precedes_probe_even_when_probe_rejects_base(self):
        self.after_change_at=1;events=[];capture=self.capture;ssh=self.ssh
        def recording_capture(vm,guest_root,builds,output):
            if Path(output).name=='base-after.json':events.append('probe')
            return capture(vm,guest_root,builds,output)
        def recording_ssh(vm,command,**kwargs):
            if command=='sudo -n sync':events.append('sync')
            return ssh(vm,command,**kwargs)
        with patch.object(suite,'capture',side_effect=recording_capture),patch.object(suite,'ssh',side_effect=recording_ssh):
            self.assertEqual(suite.run(self.planpath),1)
        self.assertEqual(events,['sync','probe'])
        self.assert_statuses(['failed']+['not-run']*7)

    def test_real_callback_continues_after_allocation_only_difference(self):
        original=self.capture
        def captured(vm,guest_root,builds,output):
            raw=original(vm,guest_root,builds,output)
            if Path(output).name=='base-before.json':
                for row in raw['files']:row['allocated_bytes']=4096
                self.base_records[guest_root]=copy.deepcopy(raw)
            else:raw['files'][2]['allocated_bytes']+=4096
            Path(output).write_text(json.dumps(raw))
            return raw
        with patch.object(suite,'capture',side_effect=captured):
            self.assertEqual(suite.run(self.planpath),0)
        self.assert_statuses(['ok']*8)
        self.assertEqual(self.producer.call_count,8)

if __name__=='__main__':unittest.main()
