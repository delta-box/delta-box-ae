"""Real-FIFO coverage of the live worker reconnect/ready protocol."""
import errno
import json
import os
from pathlib import Path
import select
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent.host.worker_client import WorkerClient, WorkerError


class WorkerClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.pipe_in, self.pipe_out = root / "in", root / "out"
        for path in (self.pipe_in, self.pipe_out):
            os.mkfifo(path)
        self.in_fd = os.open(self.pipe_in, os.O_RDWR | os.O_NONBLOCK)
        self.out_fd = os.open(self.pipe_out, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, self.in_fd)
        self.addCleanup(os.close, self.out_fd)
        self.client = WorkerClient(str(self.pipe_in), str(self.pipe_out))
        self.addCleanup(self.client.close)
        self.client.connect()

    def serve(self, responses):
        requests, errors = [], []
        stop = threading.Event()

        def worker():
            buffered = b""
            try:
                for respond in responses:
                    while b"\n" not in buffered:
                        if stop.is_set():
                            return
                        ready, _, _ = select.select([self.in_fd], [], [], .05)
                        if ready:
                            buffered += os.read(self.in_fd, 65536)
                    line, buffered = buffered.split(b"\n", 1)
                    request = json.loads(line)
                    requests.append(request)
                    for reply in respond(request):
                        os.write(self.out_fd, json.dumps(reply).encode() + b"\n")
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        def finish():
            stop.set()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
        self.addCleanup(finish)
        return requests

    @staticmethod
    def reply(request):
        return [{"ok": True, "ctrl_id": request["ctrl_id"], "state": {"branch": "restored"}}]

    def test_reopen_performs_a_ready_handshake(self):
        requests = self.serve([self.reply])
        self.client.reopen()
        self.assertEqual([r["ctrl"] for r in requests], ["state"])

    def test_empty_drain_never_waits_for_an_idle_window(self):
        with patch("agent.host.worker_client.select.select", side_effect=AssertionError("idle wait")):
            self.client._drain()

    def test_ready_reopen_has_no_fixed_sleep(self):
        self.serve([self.reply])
        with patch("agent.host.worker_client.time.sleep", side_effect=AssertionError("fixed sleep")):
            self.client.reopen()

    def test_reopen_discards_old_buffer_and_delayed_unlabelled_replies(self):
        self.client._buf = b'{"half_read":'
        os.write(self.out_fd, b'{"stale_buffered":true}\n')
        def delayed_old_replies(request):
            return [{"ok": True, "state": {"branch": "old-unlabelled"}},
                    {"ok": True, "ctrl_id": "old-branch", "state": {"branch": "old"}},
                    *self.reply(request)]
        requests = self.serve([delayed_old_replies, self.reply])
        self.client.reopen()
        result = self.client.state(timeout=1)
        self.assertEqual(result["state"]["branch"], "restored")
        self.assertEqual(len(requests), 2)
        self.assertNotEqual(requests[0]["ctrl_id"], requests[1]["ctrl_id"])

    def test_call_requires_the_matching_ctrl_id(self):
        def replies(request):
            return [{"ok": True, "state": {"branch": "stale"}}, *self.reply(request)]
        self.serve([replies])
        result = self.client.state(timeout=1)
        self.assertEqual(result["state"]["branch"], "restored")

    def test_reopen_timeout_closes_partial_connection(self):
        self.serve([lambda request: []])
        with self.assertRaises(WorkerError):
            self.client.reopen(timeout=.03)
        self.assertIsNone(self.client._fin)
        self.assertIsNone(self.client._fout_fd)

    def test_reopen_rejects_failed_ready_reply(self):
        self.serve([lambda request: [{"ok": False, "ctrl_id": request["ctrl_id"]}]])
        with self.assertRaisesRegex(WorkerError, "ready"):
            self.client.reopen(timeout=1)
        self.assertIsNone(self.client._fin)
        self.assertIsNone(self.client._fout_fd)

    def test_full_input_fifo_cannot_block_past_ready_deadline(self):
        while True:
            try:
                os.write(self.in_fd, b"x" * 4096)
            except BlockingIOError:
                break
        with self.assertRaises(WorkerError):
            self.client.reopen(timeout=.03)
        self.assertIsNone(self.client._fin)
        self.assertIsNone(self.client._fout_fd)

    def test_large_request_is_written_completely(self):
        requests = self.serve([self.reply])
        payload = {"ctrl": "init", "task": "task " * 50000}
        self.assertTrue(self.client.call(payload, timeout=2)["ok"])
        self.assertEqual(requests[0]["task"], payload["task"])

    def test_connect_missing_reader_is_bounded_and_closes_read_fd(self):
        self.client.close()
        real_open = os.open
        def no_reader(path, flags, *args, **kwargs):
            if str(path) == str(self.pipe_in):
                raise OSError(errno.ENXIO, "no reader")
            return real_open(path, flags, *args, **kwargs)
        with patch("agent.host.worker_client.os.open", side_effect=no_reader):
            with self.assertRaises(WorkerError):
                self.client.connect(wait_s=.02)
        self.assertIsNone(self.client._fin)
        self.assertIsNone(self.client._fout_fd)


if __name__ == "__main__":
    unittest.main()
