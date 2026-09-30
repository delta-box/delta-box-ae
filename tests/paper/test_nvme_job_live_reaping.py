"""Linux process regressions for CRIU-style PID release under a subreaper.

No CRIU benchmark, mount, service, CPU/memory policy or shared resource changes.
Each fixture is its own process and kills/reaps only its proven descendants.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]

SCENE=r'''import json, os, pathlib, signal, sys, time
folder=pathlib.Path(sys.argv[1]); helper=int(sys.argv[2]); desired=int(sys.argv[3])
intermediate=os.fork()
if intermediate==0:
    target=os.fork()
    if target==0:
        os.setsid()
        while True: time.sleep(60)
    (folder/'target.pid').write_text(str(target))
    os._exit(0)
os.waitpid(intermediate,0)
while not (folder/'target.pid').exists(): time.sleep(.005)
pid=int((folder/'target.pid').read_text())
def state():
    path=pathlib.Path('/proc/%d/stat' % pid)
    if not path.exists(): return None
    try:
        text=path.read_text(); fields=text[text.rfind(')')+2:].split()
    except FileNotFoundError: return None
    return dict(pid=pid,start_ticks=int(fields[19]),state=fields[0],ppid=int(fields[1]))
deadline=time.monotonic()+3
while (state() or {}).get('ppid')!=helper:
    if time.monotonic()>deadline: raise RuntimeError('owned orphan adoption did not complete')
    time.sleep(.005)
before=state(); os.kill(pid,signal.SIGTERM)
deadline=time.monotonic()+2
while state() is not None and time.monotonic()<deadline: time.sleep(.005)
after=state()
(folder/'during-producer.json').write_text(json.dumps(dict(before=before,after=after)))
sys.exit(desired if after is None else 8)
'''


def worker(mode):
    spec=importlib.util.spec_from_file_location('owned_live_fixture',ROOT/'ae/scripts/run_nvme_job.py')
    owned=importlib.util.module_from_spec(spec);spec.loader.exec_module(owned)
    work=ROOT/'ae/work';work.mkdir(parents=True,exist_ok=True)
    previous=signal.getsignal(signal.SIGCHLD)
    flag=owned.subreaper_flag()
    with tempfile.TemporaryDirectory(prefix='live-reap-test-',dir=work) as folder:
        children=owned.OwnedChildren()
        producer=subprocess.Popen([sys.executable,'-c',SCENE,folder,str(os.getpid()),'7' if mode=='status' else '0'],start_new_session=True)
        children.register(producer.pid)
        try:
            if mode!='control': children.start_live_reaping(producer.pid)
            code=producer.wait(timeout=8)
        finally:
            children.stop_live_reaping()
            pending=sorted(owned.direct_children())
            children.cleanup(producer)
            children.restore()
        helper=subprocess.run([sys.executable,'-c','raise SystemExit(9)'],check=False)
        record=json.loads((Path(folder)/'during-producer.json').read_text())
        result=dict(mode=mode,producer_returncode=code,helper_returncode=helper.returncode,
                    during_producer=record,pending_before_cleanup=pending,
                    proof=children.evidence,remaining=sorted(owned.direct_children()),
                    sigchld_restored=signal.getsignal(signal.SIGCHLD)==previous,
                    subreaper_restored=owned.subreaper_flag()==flag)
        print(json.dumps(result))
    return 0


@unittest.skipUnless(sys.platform.startswith('linux') and hasattr(os,'pidfd_open'),
                     'Linux pidfd/subreaper fixture')
class LiveReapingLinuxTests(unittest.TestCase):
    def fixture(self,mode):
        process=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker',mode],
                               capture_output=True,text=True,timeout=12)
        self.assertEqual(process.returncode,0,process.stderr)
        result=json.loads(process.stdout)
        self.assertEqual(result['remaining'],[])
        self.assertTrue(result['sigchld_restored'])
        self.assertTrue(result['subreaper_restored'])
        return result

    def test_old_no_live_reap_leaves_owned_zombie_until_producer_finishes(self):
        result=self.fixture('control')
        self.assertEqual(result['producer_returncode'],8)
        self.assertEqual(result['during_producer']['after']['state'],'Z')
        self.assertTrue(result['pending_before_cleanup'])

    def test_terminal_adopted_pid_disappears_before_producer_finishes(self):
        result=self.fixture('active')
        self.assertEqual(result['producer_returncode'],0)
        self.assertIsNone(result['during_producer']['after'])
        target=result['during_producer']['before']
        proof=next(row for row in result['proof'] if row['pid']==target['pid'] and row['start_ticks']==target['start_ticks'])
        self.assertTrue(proof['reaped_during_producer'])
        self.assertEqual(result['pending_before_cleanup'],[])

    def test_producer_nonzero_status_is_not_stolen_by_sigchld_handler(self):
        result=self.fixture('status')
        self.assertEqual(result['producer_returncode'],7)
        self.assertEqual(result['proof'][0]['returncode'],7)

    def test_sync_archive_helper_nonzero_status_is_preserved_after_handler_restore(self):
        result=self.fixture('helper')
        self.assertEqual(result['helper_returncode'],9)
        self.assertTrue(result['sigchld_restored'])


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--worker':
        raise SystemExit(worker(sys.argv[2]))
    unittest.main()
