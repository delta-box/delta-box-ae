"""Verify non-idempotent Cube snapshot calls are never blindly redispatched."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / (
    "ae/vendor/finalbench/cube_cow_peagle_mcts30_2x_numa12_realrtt/"
    "scripts/cube_cow_schedule_replay.py"
)


class CubeSnapshotDispatch(unittest.TestCase):
    def setUp(self):
        names = {"snapshot_create", "snapshot_rollback"}
        nodes = [
            node for node in ast.parse(DRIVER.read_text()).body
            if isinstance(node, ast.FunctionDef) and node.name in names
        ]
        self.assertEqual({node.name for node in nodes}, names)
        self.events = []
        self.clock = SimpleNamespace(
            time_ns=Mock(side_effect=[1000, 2000]),
            perf_counter=Mock(side_effect=[10.0, 10.125]),
            sleep=Mock(side_effect=AssertionError("retry sleep must not run")),
        )
        def settle():
            # Settle belongs after the reported API window.
            self.assertEqual(self.clock.time_ns.call_count, 2)
            self.assertEqual(self.clock.perf_counter.call_count, 2)
            self.events.append("settle")
        self.settle = Mock(side_effect=settle)
        self.reconnect = Mock(side_effect=AssertionError("mutation must not reconnect/retry"))
        self.ns = {
            "Sandbox": object, "Any": object, "time": self.clock,
            "post_api_settle": self.settle,
            "reconnect_sandbox": self.reconnect,
            # The failure must propagate even when the old classifier would retry it.
            "is_retryable_connection_error": lambda error: True,
            "retry_delay": Mock(side_effect=AssertionError("retry delay must not run")),
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(DRIVER), "exec"), self.ns)

    def check_failure(self, operation, error, committed):
        state = []
        def dispatch(*args):
            self.events.append("dispatch")
            if committed:
                state.append("server mutation committed")
            raise error
        method = Mock(side_effect=dispatch)
        sb = SimpleNamespace(sandbox_id="sandbox-1", **{operation: method})
        function = self.ns["snapshot_create" if operation == "create_snapshot" else "snapshot_rollback"]
        with self.assertRaises(type(error)) as raised:
            function(sb, "snapshot-1")
        self.assertIs(raised.exception, error)
        self.assertEqual(state, ["server mutation committed"] if committed else [])
        method.assert_called_once_with(*(() if operation == "create_snapshot" else ("snapshot-1",)))
        self.assertEqual(self.events, ["dispatch"])
        self.settle.assert_not_called()
        self.reconnect.assert_not_called()
        self.clock.sleep.assert_not_called()
        self.assertEqual(self.clock.time_ns.call_count, 1)
        self.assertEqual(self.clock.perf_counter.call_count, 1)

    def test_create_committed_but_response_lost_is_not_repeated(self):
        self.check_failure("create_snapshot", TimeoutError("response lost after server commit"), True)

    def test_rollback_committed_but_response_lost_is_not_repeated(self):
        self.check_failure("rollback", ConnectionError("connection reset after server commit"), True)

    def test_create_conflict_preserves_first_exception(self):
        self.check_failure(
            "create_snapshot", RuntimeError("130409: already has an active snapshot operation"), False
        )

    def test_rollback_conflict_preserves_first_exception(self):
        self.check_failure(
            "rollback", RuntimeError("130409: template attempt is already in progress"), False
        )

    def test_create_success_preserves_fields_and_api_only_timing(self):
        snapshot = SimpleNamespace(snapshot_id="snapshot-1", template_id="template-1")
        def dispatch():
            self.events.append("dispatch")
            return snapshot
        sb = SimpleNamespace(sandbox_id="sandbox-1", create_snapshot=Mock(side_effect=dispatch))
        returned, record = self.ns["snapshot_create"](sb, "logical-name")
        self.assertIs(returned, sb)
        self.assertEqual(record, {
            "snapshot_id": "snapshot-1", "template_id": "template-1", "name": "logical-name",
            "sandbox_id": "sandbox-1", "api_start_unix_ns": 1000, "api_end_unix_ns": 2000,
            "checkpoint_wall_ms": 125.0, "api_retries": 0, "last_retry_error": "",
        })
        sb.create_snapshot.assert_called_once_with()
        self.assertEqual(self.events, ["dispatch", "settle"])
        self.settle.assert_called_once_with()
        self.reconnect.assert_not_called()
        self.clock.sleep.assert_not_called()

    def test_rollback_success_preserves_response_and_api_only_timing(self):
        response = {"restored": True, "snapshot_id": "snapshot-1"}
        def dispatch(snapshot_id):
            self.events.append("dispatch")
            return response
        sb = SimpleNamespace(sandbox_id="sandbox-1", rollback=Mock(side_effect=dispatch))
        returned, record = self.ns["snapshot_rollback"](sb, "snapshot-1")
        self.assertIs(returned, sb)
        self.assertIs(record["rollback_response"], response)
        self.assertEqual(record, {
            "ok": True, "snapshot_id": "snapshot-1", "rollback_response": response,
            "sandbox_id": "sandbox-1", "api_start_unix_ns": 1000, "api_end_unix_ns": 2000,
            "restore_wall_ms": 125.0, "api_retries": 0, "last_retry_error": "",
        })
        sb.rollback.assert_called_once_with("snapshot-1")
        self.assertEqual(self.events, ["dispatch", "settle"])
        self.settle.assert_called_once_with()
        self.reconnect.assert_not_called()
        self.clock.sleep.assert_not_called()

    def test_create_success_without_optional_template_id(self):
        sb = SimpleNamespace(
            sandbox_id="sandbox-1",
            create_snapshot=Mock(return_value=SimpleNamespace(snapshot_id="snapshot-1")),
        )
        _, record = self.ns["snapshot_create"](sb, "logical-name")
        self.assertEqual(record["template_id"], "")
        self.assertEqual(record["api_retries"], 0)
        sb.create_snapshot.assert_called_once_with()

    def test_settle_failure_cannot_redispatch_committed_mutation(self):
        error = RuntimeError("connection error during settle")
        self.settle.side_effect = error
        sb = SimpleNamespace(
            sandbox_id="sandbox-1",
            create_snapshot=Mock(return_value=SimpleNamespace(snapshot_id="snapshot-1")),
        )
        with self.assertRaises(RuntimeError) as raised:
            self.ns["snapshot_create"](sb, "logical-name")
        self.assertIs(raised.exception, error)
        sb.create_snapshot.assert_called_once_with()
        self.settle.assert_called_once_with()
        self.reconnect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
