"""Validate inherited E2B memory numerically, including fragmented/bad replies."""
import contextlib
import importlib.util
import io
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "e2b_numeric_fixture", Path(__file__).with_name("test_e2b_fanout_failure_evidence.py"))
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)
D = FIXTURE.DRIVER
TOKEN = "fixture-token"


class Connection:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []
        self.closed = False
        self.reads = 0

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, count):
        self.reads += 1
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if len(chunk) > count:
            self.chunks.insert(0, chunk[count:])
        return chunk[:count]

    def close(self):
        self.closed = True


def reply(*, mem_mib=64, token=TOKEN, byte_count=None, checksum=None, requests=2, pid=14):
    # Deliberately calculate from page values rather than the production formula.
    size = mem_mib * 1024 * 1024
    check = sum(index % 251 for index in range(size // 4096)) & 0xFFFFFFFF
    return (f"OK token={token} bytes={size if byte_count is None else byte_count} "
            f"checksum={check if checksum is None else checksum} requests={requests} pid={pid}\n").encode()


class NumericMemoryTests(unittest.TestCase):
    def guest(self, chunks, *, mem_mib=64, marker=None, optimize=0):
        shell = D.verify_mem_server_shell(token=TOKEN, mem_mib=mem_mib)
        code = shell.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
        connection = Connection(chunks)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state"
            path.write_text(marker if marker is not None else
                f"official-fork-state token={TOKEN} bytes={mem_mib * 1024 * 1024} pid=14\n")
            code = code.replace("'/tmp/official_fork_state.txt'", repr(str(path)))
            output = io.StringIO()
            with patch.object(socket, "create_connection", return_value=connection) as connect, \
                    contextlib.redirect_stdout(output):
                exec(compile(code, "<numeric-memory-verifier>", "exec", optimize=optimize), {})
            connect.assert_called_once_with(("127.0.0.1", 38765), timeout=None)
        self.assertEqual(connection.sent, [b"touch\n"])
        self.assertTrue(connection.closed)
        return output.getvalue(), connection

    def test_expected_checksum_matches_server_page_pattern(self):
        self.assertEqual(D.expected_memory_state(64), {"bytes": 67108864, "checksum": 2041721})
        for mem_mib in (1, 2, 64, 251):
            with self.subTest(mem_mib=mem_mib):
                output, _ = self.guest([reply(mem_mib=mem_mib)], mem_mib=mem_mib)
                self.assertEqual(output.encode(), reply(mem_mib=mem_mib))

    def test_rejects_invalid_memory_sizes(self):
        for bad in (0, -1, 1.5, "64", True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                D.verify_mem_server_shell(token=TOKEN, mem_mib=bad)

    def test_split_response_is_reassembled_without_repeating_touch(self):
        data = reply()
        output, connection = self.guest([data[:3], data[3:19], data[19:-1], data[-1:]])
        self.assertEqual(output.encode(), data)
        self.assertEqual(connection.reads, 4)

    def test_incorrect_numeric_values_and_token_fail_even_under_python_optimization(self):
        for bad in (reply(byte_count=1), reply(checksum=0), reply(token=TOKEN+"-other"),
                    reply(requests=0), reply(pid=0)):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "inherited-memory mismatch"):
                self.guest([bad], optimize=2)

    def test_numeric_failure_contains_actual_and_expected_values(self):
        with self.assertRaises(ValueError) as caught:
            self.guest([reply(byte_count=1, checksum=0)])
        error = str(caught.exception)
        self.assertIn('"expected": {"bytes": 67108864, "checksum": 2041721}', error)
        self.assertIn('"observed": {"bytes": 1, "checksum": 0}', error)

    def test_rejects_missing_duplicate_and_malformed_fields(self):
        data = reply()
        bad_replies = (
            data.replace(b"checksum=2041721 ", b""),
            data.replace(b"checksum=2041721", b"checksum=0 checksum=2041721"),
            data.replace(b"checksum=2041721", b"checksum=garbage"),
            data.replace(b"checksum=2041721", b"checksum=-1"),
            data.replace(b"bytes=67108864", b"bytes=6.7108864e7"),
            b"prefix " + data,
            data + b"trailing",
            data.replace(b"\n", b"\nOK\n"),
        )
        for bad in bad_replies:
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "malformed"):
                self.guest([bad])

    def test_truncated_and_overlong_responses_fail(self):
        for bad in (b"", reply()[:-1], b"x" * 4097):
            with self.subTest(size=len(bad)), self.assertRaisesRegex(ValueError, "truncated|overlong"):
                self.guest([bad])
        with self.assertRaises(UnicodeDecodeError):
            self.guest([b"\xff\n"])

    def test_marker_token_and_byte_count_are_checked_exactly(self):
        markers = (
            f"official-fork-state token={TOKEN}-other bytes=67108864 pid=14\n",
            f"official-fork-state token={TOKEN} bytes=1 pid=14\n",
            f"official-fork-state token={TOKEN}\n",
            f"official-fork-state token={TOKEN} bytes=67108864 bytes=1 pid=14\n",
        )
        for marker in markers:
            with self.subTest(marker=marker), self.assertRaisesRegex(ValueError, "marker"):
                self.guest([reply()], marker=marker)

    def test_child_result_records_values_parsed_from_response(self):
        evidence = D.verify_child_memory(lambda *args, **kwargs: reply(pid=735, requests=7).decode(),
                                        None, token=TOKEN, mem_mib=64, timeout=120)
        self.assertEqual(evidence["expected"], {"bytes": 67108864, "checksum": 2041721})
        self.assertEqual(evidence["observed"], {"bytes": 67108864, "checksum": 2041721})
        self.assertEqual((evidence["requests"], evidence["pid"]), (7, 735))
        for bad in ("OK", reply(checksum=0).decode(), reply(byte_count=1).decode()):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                D.verify_child_memory(lambda *args, **kwargs: bad, None,
                                      token=TOKEN, mem_mib=64, timeout=120)

    def test_full_grid_keeps_numeric_evidence_for_every_child(self):
        rc, rows = FIXTURE.FanoutFailureEvidenceTests().invoke(FIXTURE.FakeSDK(), "1,4,16,64")
        self.assertEqual(rc, 0)
        self.assertEqual(sum(len(row["children"]) for row in rows), 85)
        for row in rows:
            for child in row["children"]:
                evidence = child["memory_validation"]
                self.assertEqual(evidence["observed"], {"bytes": 67108864, "checksum": 2041721})
                self.assertEqual(evidence["token"], row["run_id"])

    def test_bad_child_fails_point_without_retry_or_discarding_other_children(self):
        original = D.e2b_run_shell
        def corrupt(sb, command, *, timeout):
            output = original(sb, command, timeout=timeout)
            return output.replace("checksum=2041721", "checksum=0") if sb.sandbox_id.endswith("-2") else output
        sdk = FIXTURE.FakeSDK()
        with patch.object(D, "e2b_run_shell", side_effect=corrupt):
            rc, rows = FIXTURE.FanoutFailureEvidenceTests().invoke(sdk, "16")
        self.assertEqual(rc, 1)
        self.assertEqual(rows[0]["success_count"], 15)
        self.assertEqual(len(sdk.verifies), 16)
        failed = [child for child in rows[0]["children"] if not child["verify"]["ok"]]
        self.assertEqual(len(failed), 1)
        self.assertIn('"observed": {"bytes": 67108864, "checksum": 0}', failed[0]["verify"]["error"])
        self.assertNotIn("ready_e2e_ms", rows[0])


if __name__ == "__main__":
    unittest.main()
