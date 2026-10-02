"""Real FIFO/process regressions for the reply-vs-log FD checkpoint race."""
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from backends.deltabox.gsd import template_fork as tf, async_resources as ar

CHILD = r'''
import importlib.util,os,sys,select,time
from pathlib import Path
spec=importlib.util.spec_from_file_location('template_fork',sys.argv[1]);tf=importlib.util.module_from_spec(spec);spec.loader.exec_module(tf)
root=Path(sys.argv[2]);fd=os.open(root/'in',os.O_RDWR|os.O_NONBLOCK)
# Model an agent that has already replied but still holds its append log.
log=open(root/'trace.jsonl','a');log.write('pending completion log\n');log.flush()
persistent=open(root/'persistent.data','a') if sys.argv[3]=='persistent' else None
(root/'ready').touch()
while not (root/'release').exists(): time.sleep(.001)
log.close()
while not select.select([fd],[],[],2)[0]: pass
role=tf._handle_template_message(fd,str(root/'out'))
assert role=='parent_resumed'
os.close(fd)
if persistent: persistent.close()
'''

class CheckpointQuiescenceTests(unittest.TestCase):
    def exercise(self, persistent=False, validation_error=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in ('in','out'):os.mkfifo(root/name)
            stderr_log=(root/'stderr.log').open('ab')
            child=subprocess.Popen([sys.executable,'-B','-c',CHILD,tf.__file__,tmp,
                                    'persistent' if persistent else 'transient'],
                                   stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=stderr_log)
            pool=tf.TemplatePool(str(root/'in'),str(root/'out'))
            try:
                deadline=time.monotonic()+3
                while not (root/'ready').exists():
                    if child.poll() is not None: self.fail((root/'stderr.log').read_text())
                    self.assertLess(time.monotonic(),deadline)
                    time.sleep(.005)
                def inspect():
                    return ar.validate_replay_resources(child.pid,overlay_mount_point='/testbed',
                        allowed_fifo_paths=[str(root/'in'),str(root/'out')],
                        allowed_stdio_paths=[str(root/'stderr.log')])
                # The old live inspection sees the trace FD and fails.
                with self.assertRaisesRegex(ar.UnsupportedAsyncResources,'persistent fd'):
                    inspect()
                (root/'release').touch()
                expected=ar.UnsupportedAsyncResources if persistent else ValueError
                def checked():
                    with pool.quiesce_for_checkpoint(child.pid):
                        state=Path(f'/proc/{child.pid}/stat').read_text().rpartition(')')[2].split()[0]
                        self.assertEqual(state,'T')
                        links=[os.readlink(p) for p in Path(f'/proc/{child.pid}/fd').iterdir()]
                        self.assertNotIn(str(root/'trace.jsonl'),links)
                        contract=inspect()
                        self.assertEqual(contract['pid'],child.pid)
                        if validation_error:raise ValueError('preserved validation error')
                if persistent or validation_error:
                    with self.assertRaises(expected):checked()
                else:checked()
                child.wait(timeout=3)
                self.assertEqual(child.returncode,0,(root/'stderr.log').read_text())
            finally:
                pool.reset_channels()
                if child.poll() is None:
                    child.kill();child.wait(timeout=3)
                stderr_log.close()

    def test_transient_log_is_closed_before_real_resource_inspection(self):
        self.exercise()
    def test_persistent_application_fd_still_fails_and_worker_is_resumed(self):
        self.exercise(persistent=True)
    def test_validation_exception_resumes_the_original_worker(self):
        self.exercise(validation_error=True)
    def test_guest_python_without_native_pidfd_uses_existing_syscall_fallback(self):
        with patch.object(os, 'pidfd_open', None), \
             patch.object(signal, 'pidfd_send_signal', None):
            self.exercise()

    def test_multithreaded_worker_rejects_pause_before_stopping(self):
        with patch.object(tf,'_read_ctrl_line',return_value=json.dumps({'op':'checkpoint_pause','token':'x'})), \
             patch.object(tf,'_assert_single_threaded',return_value=2), \
             patch.object(tf,'_write_response') as reply,patch.object(tf.os,'kill') as kill:
            self.assertIsNone(tf._handle_template_message(0,'/unused'))
            self.assertFalse(reply.call_args.args[1]['ok']);kill.assert_not_called()

if __name__=='__main__':unittest.main()
