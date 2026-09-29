"""The profile adapter preserves one complete input and joins its worker."""
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from ae.runners.profile_replay import run_driver

class ProfileWorkerContextTests(unittest.TestCase):
    def test_one_worker_forwards_arguments_and_keeps_audit_on_main(self):
        main_tid=threading.get_native_id();events=[]
        def original(*a,**kw):
            events.append(('worker',threading.get_native_id(),a,kw));return 0
        driver=SimpleNamespace(run_replay=original)
        def main():
            self.assertEqual(threading.get_native_id(),main_tid)
            result=driver.run_replay('one-input',limit=29)
            events.append(('audit',threading.get_native_id()))
            return result
        driver.main=main
        with tempfile.TemporaryDirectory() as tmp:
            record=Path(tmp)/'execution.json'
            self.assertEqual(run_driver(driver,record),0)
            data=json.loads(record.read_text())
        self.assertIs(driver.run_replay,original)
        self.assertNotEqual(events[0][1],main_tid)
        self.assertEqual(events[0][2:],(('one-input',),{'limit':29}))
        self.assertEqual(events[1],('audit',main_tid))
        self.assertEqual(data['status'],'complete')
        self.assertNotIn(data['worker_native_tid'],[t.native_id for t in threading.enumerate()])

    def test_failure_is_preserved_and_worker_joined(self):
        def original():raise ValueError('recorded action mismatch')
        driver=SimpleNamespace(run_replay=original)
        driver.main=lambda:driver.run_replay()
        with tempfile.TemporaryDirectory() as tmp:
            record=Path(tmp)/'execution.json'
            with self.assertRaisesRegex(ValueError,'action mismatch'):run_driver(driver,record)
            data=json.loads(record.read_text())
        self.assertEqual(data['status'],'failed')
        self.assertIs(driver.run_replay,original)
        self.assertNotIn(data['worker_native_tid'],[t.native_id for t in threading.enumerate()])

    def test_nonzero_result_is_not_marked_complete(self):
        driver=SimpleNamespace(run_replay=lambda:7);driver.main=lambda:driver.run_replay()
        with tempfile.TemporaryDirectory() as tmp:
            record=Path(tmp)/'execution.json';self.assertEqual(run_driver(driver,record),7)
            self.assertEqual(json.loads(record.read_text())['status'],'failed')

    def test_repeated_dispatch_is_rejected(self):
        driver=SimpleNamespace(run_replay=lambda:0)
        def main():driver.run_replay();return driver.run_replay()
        driver.main=main
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError,'one complete recorded input'):
                run_driver(driver,Path(tmp)/'execution.json')

class ProfileThreadChildrenTests(unittest.TestCase):
    def test_rss_includes_worker_thread_children_once(self):
        from unittest.mock import patch
        import importlib.util,sys
        root=Path(__file__).resolve().parents[2]
        sys.path.insert(0,str(root/'ae/runners'))
        spec=importlib.util.spec_from_file_location('test_profile_thread_children',root/'ae/runners/profile.py')
        profile=importlib.util.module_from_spec(spec);spec.loader.exec_module(profile)
        with tempfile.TemporaryDirectory() as tmp:
            proc=Path(tmp)
            for pid,rss,tasks in [(100,1024,{100:'',101:'200',102:'200'}),(200,512,{200:''})]:
                (proc/str(pid)).mkdir();(proc/str(pid)/'status').write_text(f'VmRSS: {rss} kB\n')
                for tid,children in tasks.items():
                    path=proc/str(pid)/'task'/str(tid);path.mkdir(parents=True)
                    (path/'children').write_text(children)
            with patch.object(profile,'PROC_ROOT',proc):
                samples=dict(profile.sample(100))
            self.assertEqual(samples,{100:1024,200:512})

    def test_memory_profile_plan_reuses_bounded_noswap_runner(self):
        from tests.paper import test_ae_entrypoints as entry
        from unittest.mock import patch
        fixture=entry.ReviewTests();original=entry.review.run_lock
        with tempfile.TemporaryDirectory() as tmp:
            def isolated_lock(path,**kwargs):return original(Path(tmp)/Path(path).name,**kwargs)
            with patch.object(entry.review,'run_lock',side_effect=isolated_lock):
                code,record,commands,files=fixture.exercise(['--experiment','figure-02-memory'],
                    config_extra={'baseline_storage':'tmpfs','memory_job_size_gib':4})
        self.assertEqual(code,0)
        plan=json.loads(files['plans/attempt-001/figure-02-memory.json'])
        self.assertEqual(plan['memory_measurement']['size_gib'],4)

if __name__=='__main__':unittest.main()
