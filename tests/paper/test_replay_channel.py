"""Real FIFO regression: abort and a new request in one pre-epoch read."""
import concurrent.futures
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "replay/guest"))
from control_channel import AgentProbe, restore_with_ready
from lifecycle import ReplayResources

AGENT_WRAPPER = r'''
import importlib.util, os, pathlib, sys, time
source, directory = sys.argv[1:]
p = pathlib.Path(directory)
spec = importlib.util.spec_from_file_location("test_agent", source)
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
for name in ("PIPE_IN", "PIPE_OUT", "NPD_REQ_FIFO", "NPD_NOTIFY_FIFO",
             "NPD_REQ_DIR", "NPD_RESP_DIR", "NPD_EPOCH_FILE", "LOG_FILE", "TRACE_PATH"):
    setattr(a, name, str(p / name))
a.WARM_TEMPLATE = False
os.environ["DELTABOX_REOPEN_AGENT_FIFOS_ON_EPOCH"] = "1"
os.environ["DELTABOX_REPLAY_STRICT_EPOCH"] = "1"
original = a.select.select
gated = False
def selected(*args):
    global gated
    if not gated:
        gated = True
        (p / "selected").touch()
        deadline = time.monotonic() + 5
        while not (p / "release").exists():
            if time.monotonic() > deadline: raise RuntimeError("test gate timed out")
            time.sleep(0.001)
    return original(*args)
a.select.select = selected
a.main()
'''


class ReplayChannelTests(unittest.TestCase):
    def test_epoch_reopen_preserves_new_request_and_rejects_stale_work(self):
        source = os.environ.get("REPLAY_AGENT_SOURCE", str(ROOT / "replay/guest/agent.py"))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "NPD_EPOCH_FILE").write_text("0")
            with (p / "stdout").open("w") as output:
                proc = subprocess.Popen([sys.executable, "-c", AGENT_WRAPPER, source, tmp],
                                        stdout=output, stderr=output)
                read_fd = write_fd = None
                try:
                    deadline = time.monotonic() + 5
                    while not (p / "selected").exists():
                        self.assertIsNone(proc.poll(), (p / "stdout").read_text())
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.005)
                    read_fd = os.open(p / "PIPE_OUT", os.O_RDWR | os.O_NONBLOCK)
                    write_fd = os.open(p / "PIPE_IN", os.O_WRONLY | os.O_NONBLOCK)
                    # Freeze before read at epoch 0; then one atomic write.
                    (p / "NPD_EPOCH_FILE").write_text("1")
                    requests = [
                        {"ctrl": "abort_pending_all"},
                        {"ctrl": "worker_exec", "ctrl_id": "old", "_replay_epoch": 0,
                         "root": tmp, "ops": [{"type": "bash", "command": "touch stale"}]},
                        {"ctrl": "worker_index_status", "ctrl_id": "new", "_replay_epoch": 1},
                    ]
                    frame = b"".join(json.dumps(r).encode() + b"\n" for r in requests)
                    self.assertLess(len(frame), os.fpathconf(write_fd, "PC_PIPE_BUF"))
                    os.write(write_fd, frame)
                    (p / "release").touch()
                    ready, _, _ = select.select([read_fd], [], [], 2)
                    self.assertTrue(ready, "new epoch request was lost")
                    rows = [json.loads(line) for line in os.read(read_fd, 65536).splitlines()]
                    self.assertEqual([row["ctrl_id"] for row in rows], ["new"])
                    self.assertFalse((p / "stale").exists())
                    self.assertIn('"agent_stale_runner_request"', (p / "TRACE_PATH").read_text())
                    # Recover from a partial stale frame without executing it.
                    os.write(write_fd, b'{"ctrl":"worker_exec",')
                    fresh = {"ctrl": "replay_ready", "ctrl_id": "ready", "_replay_epoch": 1}
                    os.write(write_fd, b"\x1e" + json.dumps(fresh).encode() + b"\n")
                    self.assertTrue(select.select([read_fd], [], [], 2)[0])
                    reply = json.loads(os.read(read_fd, 65536))
                    self.assertEqual((reply["ctrl_id"], reply["epoch"]), ("ready", 1))
                finally:
                    proc.kill()
                    proc.wait(timeout=3)
                    for fd in (read_fd, write_fd):
                        if fd is not None:
                            os.close(fd)

    def test_connect_without_reader_has_bounded_failure_and_closes_fds(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            for name in ("in", "out"):
                os.mkfifo(p / name)
            (p / "epoch").write_text("2")
            probe = AgentProbe(str(p / "in"), str(p / "out"), str(p / "epoch"))
            start = time.monotonic()
            result = probe.control({"ctrl": "worker_exec"}, timeout=0.03)
            self.assertEqual(result["err"], "timeout")
            self.assertLess(time.monotonic() - start, 0.5)
            self.assertIsNone(probe._in_fd)
            self.assertIsNone(probe._out_fd)

    def test_partial_writes_and_stale_responses_do_not_resend_workload(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            for name in ("in", "out"):
                os.mkfifo(p / name)
            (p / "epoch").write_text("3")
            rfd = os.open(p / "in", os.O_RDWR | os.O_NONBLOCK)
            wfd = os.open(p / "out", os.O_RDWR | os.O_NONBLOCK)
            probe = AgentProbe(str(p / "in"), str(p / "out"), str(p / "epoch"))
            write = os.write

            def server():
                data = b""
                while b"\n" not in data:
                    if not select.select([rfd], [], [], 1)[0]:
                        raise TimeoutError("test server")
                    data += os.read(rfd, 65536)
                req = json.loads(data.lstrip(b"\x1e"))
                stale = {"ctrl": req["ctrl"], "ctrl_id": "ep2.ctrl-1", "ok": True}
                good = {"ctrl": req["ctrl"], "ctrl_id": req["ctrl_id"], "ok": True}
                write(wfd, (json.dumps(stale) + "\n" + json.dumps(good) + "\n").encode())
                return req
            try:
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    pending = pool.submit(server)
                    with patch("control_channel.os.write",
                               side_effect=lambda fd, data: write(fd, data[:7])):
                        result = probe.control({"ctrl": "worker_exec", "value": "x" * 600},
                                               timeout=1)
                    request = pending.result(timeout=2)
                self.assertTrue(result["ok"])
                self.assertEqual(request["_replay_epoch"], 3)
                self.assertEqual(request["value"], "x" * 600)
                self.assertFalse(select.select([rfd], [], [], 0)[0])
            finally:
                probe.reset()
                os.close(rfd)
                os.close(wfd)

    def test_write_backpressure_has_one_bounded_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            for name in ("in", "out"):
                os.mkfifo(p / name)
            (p / "epoch").write_text("1")
            reader = os.open(p / "in", os.O_RDWR | os.O_NONBLOCK)
            probe = AgentProbe(str(p / "in"), str(p / "out"), str(p / "epoch"))
            try:
                started = time.monotonic()
                result = probe.control({"ctrl": "worker_exec", "data": "x" * (4 << 20)}, timeout=.1)
                self.assertEqual(result["err"], "timeout")
                self.assertLess(time.monotonic() - started, 1)
                self.assertIsNone(probe._in_fd)
                self.assertIsNone(probe._out_fd)
            finally:
                probe.reset()
                os.close(reader)

    def test_epoch_cleanup_unblocks_cap_and_old_abort_keeps_new_pending(self):
        source = str(ROOT / "replay/guest/agent.py")
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p / "NPD_EPOCH_FILE").write_text("0")
            with (p / "stdout").open("w") as output:
                proc = subprocess.Popen([sys.executable, "-c", AGENT_WRAPPER, source, tmp],
                                        stdout=output, stderr=output)
                rfd = wfd = nfd = None
                def wait_for(condition):
                    deadline = time.monotonic() + 3
                    while not condition():
                        self.assertIsNone(proc.poll(), (p / "stdout").read_text())
                        self.assertLess(time.monotonic(), deadline, (p / "stdout").read_text())
                        time.sleep(.005)
                def send(request):
                    os.write(wfd, b"\x1e" + json.dumps(request).encode() + b"\n")
                try:
                    wait_for(lambda: (p / "selected").exists())
                    rfd = os.open(p / "PIPE_OUT", os.O_RDWR | os.O_NONBLOCK)
                    wfd = os.open(p / "PIPE_IN", os.O_WRONLY | os.O_NONBLOCK)
                    nfd = os.open(p / "NPD_NOTIFY_FIFO", os.O_RDWR | os.O_NONBLOCK)
                    for i in range(2):
                        send({"messages": [{"role": "user", "content": str(i)}], "_replay_epoch": 0})
                    (p / "release").touch()
                    wait_for(lambda: len(list((p / "NPD_REQ_DIR").glob("ep0.*.json"))) == 2)
                    (p / "NPD_EPOCH_FILE").write_text("1")
                    for i in range(2):
                        send({"messages": [{"role": "user", "content": str(i)}], "_replay_epoch": 1})
                    # This late abort must not cancel either newly admitted rid.
                    send({"ctrl": "abort_pending_all", "_replay_epoch": 0})
                    send({"ctrl": "replay_ready", "ctrl_id": "ready", "_replay_epoch": 1})
                    wait_for(lambda: len(list((p / "NPD_REQ_DIR").glob("ep1.*.json"))) == 2)
                    rids = sorted(path.stem for path in (p / "NPD_REQ_DIR").glob("ep1.*.json"))
                    for index, rid in enumerate(rids):
                        (p / "NPD_RESP_DIR" / (rid + ".json")).write_text(json.dumps({
                            "ok": True, "content": json.dumps({"marker": index})}))
                        os.write(nfd, (rid + "\n").encode())
                    data = b""
                    deadline = time.monotonic() + 3
                    while data.count(b"\n") < 3:
                        self.assertLess(time.monotonic(), deadline)
                        if select.select([rfd], [], [], .05)[0]:
                            data += os.read(rfd, 65536)
                    replies = [json.loads(line) for line in data.splitlines()]
                    self.assertEqual(sorted(row["marker"] for row in replies if "marker" in row), [0, 1])
                    ready = [row for row in replies if row.get("ctrl") == "replay_ready"]
                    self.assertEqual(ready[0]["epoch"], 1)
                    events = [json.loads(line) for line in (p / "TRACE_PATH").read_text().splitlines()]
                    self.assertTrue(any(e["kind"] == "agent_epoch_pending_cleanup" and e["n_stale"] == 2 for e in events))
                    self.assertFalse(any(e["kind"] == "agent_abort_pending_all" for e in events))
                finally:
                    proc.kill()
                    proc.wait(timeout=3)
                    for fd in (rfd, wfd, nfd):
                        if fd is not None:
                            os.close(fd)

    def test_cold_ready_is_gated_and_warm_uses_existing_fork_handshake(self):
        controller, channel = Mock(), Mock()
        controller.restore_action.return_value = {"path": "criu"}
        channel._epoch.return_value = 4
        channel.control_fresh.return_value = {"ok": True, "epoch": 4}
        self.assertIn("restore_replay_ready_ms", restore_with_ready(controller, "A", channel))
        channel.control_fresh.return_value = {"ok": True, "epoch": 3}
        with self.assertRaisesRegex(RuntimeError, "activation failed"):
            restore_with_ready(controller, "A", channel)
        channel.reset_mock()
        controller.restore_action.return_value = {"path": "warm-template"}
        restore_with_ready(controller, "A", channel)
        channel.control_fresh.assert_not_called()

    def test_cold_ready_recovers_real_fifo_reader_close_between_open_and_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            for name in ('in', 'out'):
                os.mkfifo(p / name)
            (p / 'epoch').write_text('1')
            readers = [os.open(p / 'in', os.O_RDONLY | os.O_NONBLOCK)]
            output = os.open(p / 'out', os.O_RDWR | os.O_NONBLOCK)
            broken = threading.Event()
            writes = os.write
            injected = []

            class RacingProbe(AgentProbe):
                def _connect_until(self, deadline):
                    super()._connect_until(deadline)
                    if not injected:
                        injected.append(True)
                        os.close(readers.pop())

            probe = RacingProbe(str(p / 'in'), str(p / 'out'), str(p / 'epoch'))

            def writer(fd, data):
                try:
                    return writes(fd, data)
                except BrokenPipeError:
                    # This is a real kernel EPIPE; recreate the reader only
                    # after the failed write, as epoch reopening eventually does.
                    readers.append(os.open(p / 'in', os.O_RDONLY | os.O_NONBLOCK))
                    broken.set()
                    raise

            def server():
                if not broken.wait(2):
                    raise TimeoutError('test did not trigger EPIPE')
                data = b''
                deadline = time.monotonic() + 2
                while b'\n' not in data:
                    if time.monotonic() > deadline:
                        raise TimeoutError('ready reconnect failed')
                    if select.select([readers[0]], [], [], .01)[0]:
                        try:
                            data += os.read(readers[0], 65536)
                        except BlockingIOError:
                            # Linux reports FIFO EOF as readable while no
                            # writer exists. A reconnect between select/read
                            # clears EOF before its first write, so this
                            # nonblocking read can legitimately return EAGAIN.
                            continue
                request = json.loads(data.lstrip(b'\x1e'))
                writes(output, (json.dumps({'ok': True, 'ctrl': request['ctrl'],
                       'ctrl_id': request['ctrl_id'], 'epoch': 1}) + '\n').encode())
                return request

            try:
                controller = Mock()
                controller.restore_action.return_value = {'path': 'criu'}
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    pending = pool.submit(server)
                    with patch('control_channel.os.write', side_effect=writer):
                        result = restore_with_ready(controller, 'A', probe)
                    request = pending.result(timeout=3)
                self.assertTrue(broken.is_set())
                self.assertEqual(request['ctrl'], 'replay_ready')
                self.assertEqual(request['ctrl_id'], 'ep1.ctrl-2')
                self.assertEqual(result['restore_replay_ready_retries'], 1)
                self.assertGreaterEqual(result['restore_replay_ready_ms'], 5)
                self.assertIsNone(probe._in_fd)
                self.assertIsNone(probe._out_fd)
            finally:
                probe.reset()
                for fd in readers + [output]:
                    os.close(fd)

    def test_cold_ready_retries_share_one_deadline_and_reject_other_errors(self):
        controller, channel = Mock(), Mock()
        controller.restore_action.return_value = {'path': 'criu'}
        channel.control_fresh.return_value = {'ok': False, 'err': 'BrokenPipeError'}
        now = [10.0]

        def fail_after_delay(*args, **kwargs):
            now[0] += 2.0
            return {'ok': False, 'err': 'BrokenPipeError'}

        channel.control_fresh.side_effect = fail_after_delay
        with patch('control_channel.time.monotonic', side_effect=lambda: now[0]), \
                patch('control_channel.time.sleep', side_effect=lambda n: now.__setitem__(0, now[0] + n)):
            with self.assertRaisesRegex(RuntimeError, 'activation failed'):
                restore_with_ready(controller, 'A', channel)
        self.assertEqual(channel.control_fresh.call_count, 3)
        timeouts = [call.kwargs['timeout'] for call in channel.control_fresh.call_args_list]
        self.assertEqual(timeouts[0], 5)
        self.assertTrue(0 < timeouts[2] < timeouts[1] < timeouts[0])
        channel.reset_mock()
        channel.control_fresh.side_effect = None
        channel.control_fresh.return_value = {'ok': False, 'err': 'timeout'}
        with self.assertRaisesRegex(RuntimeError, 'activation failed'):
            restore_with_ready(controller, 'A', channel)
        channel.control_fresh.assert_called_once()

    def test_startup_exception_reaps_already_started_child(self):
        with self.assertRaisesRegex(RuntimeError, "initialization"):
            with ReplayResources() as resources:
                proc = resources.popen([sys.executable, "-c", "import time; time.sleep(60)"])
                raise RuntimeError("initialization")
        self.assertIsNotNone(proc.poll())


if __name__ == "__main__":
    unittest.main()
