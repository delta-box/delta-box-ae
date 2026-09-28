"""Finite recorded replay must stop only after every real recorded expansion."""
import ast
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
PAYLOAD = ROOT / "ae/vendor/spr_payload"
sys.path.insert(0, str(PAYLOAD))
spec = importlib.util.spec_from_file_location("recorded_boundary_driver", PAYLOAD / "replay_driver.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
SEARCH = PAYLOAD / "moatless-det-src/moatless/search_tree.py"


def node(node_id, *, duplicate=False, terminal=False):
    steps = [] if node_id == 0 else [{
        "action": {"action_args_class": "example.Find", "pattern": str(node_id), "thoughts": None},
        "observation": None if duplicate else {"message": "real result"},
    }]
    return dict(node_id=node_id, children=[], action_steps=steps,
                completions={} if node_id == 0 else {"build_action": {"response": {"id": str(node_id)}}},
                terminal=terminal, is_duplicate=True if duplicate else None, error=None)


def recording(count=4):
    root = node(0)
    parent = root
    for index in range(1, count):
        child = node(index)
        parent["children"].append(child)
        parent = child
    return dict(root=root, max_iterations=30, max_depth=41, max_expansions=2,
                unique_id=count - 1, agent={})


class FakeNode:
    def __init__(self, data, parent=None):
        self.data = deepcopy(data)
        self.node_id = data["node_id"]
        self.parent = parent
        self.reward = None
        self.children = [FakeNode(child, self) for child in data.get("children", [])]
    @property
    def terminal(self): return self.data.get("terminal", False)
    @property
    def is_duplicate(self): return self.data.get("is_duplicate")
    @property
    def error(self): return self.data.get("error")
    @property
    def action_steps(self):
        steps = []
        for step in self.data.get("action_steps", []):
            raw = deepcopy(step["action"])
            module, name = raw["action_args_class"].rsplit(".", 1)
            action_type = type(name, (), {"__module__": module, "model_dump": lambda obj: deepcopy(obj.data)})
            action = action_type()
            action.data = raw
            steps.append(SimpleNamespace(action=action, observation=step.get("observation")))
        return steps
    def get_all_nodes(self):
        return [self] + [item for child in self.children for item in child.get_all_nodes()]
    def model_dump(self):
        result = deepcopy(self.data)
        result["children"] = [child.model_dump() for child in self.children]
        return result


def run_search_method():
    cls = next(n for n in ast.parse(SEARCH.read_text()).body if isinstance(n, ast.ClassDef) and n.name == "SearchTree")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "run_search")
    namespace = dict(Node=FakeNode, logger=Mock(), time=time, os=__import__("os"),
        generate_ascii_tree=lambda *args: "tree", _proc_kb_snapshot=lambda: {},
        _clear_soft_dirty_refs=lambda: None, _count_soft_dirty_pages=lambda: {"soft_dirty_bytes": 4096},
        _repo_patch_stats=lambda repo: {}, _action_write_stats=lambda node: {})
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(SEARCH), "exec"), namespace)
    return namespace["run_search"]


class FakeTree:
    run_search = run_search_method()
    def __init__(self, original):
        self.original = deepcopy(original)
        initial = driver.strip_recorded_tree(original)
        self.root = FakeNode(initial["root"])
        self.max_iterations = initial["max_iterations"]
        self.max_depth = initial["max_depth"]
        self.max_expansions = initial["max_expansions"]
        self.metadata = {"instance_id": "test__repo-1"}
        self.repository = None
        self.rows, self.events, self.simulations = [], [], []
        self.early_finish_at = None
        self.failure_at = None
        self.failure = RuntimeError("action failed")
        self.planned = []
        def walk(data, parent_id=None):
            if parent_id is not None:
                self.planned.append((data, parent_id))
            for child in data["children"]:
                walk(child, data["node_id"])
        walk(original["root"])
        self.planned.sort(key=lambda pair: pair[0]["node_id"])
    def assert_runnable(self): pass
    def _step_metrics_path(self): return "fake-metrics"
    def log(self, *args, **kwargs): pass
    def is_finished(self):
        count = len(self.root.get_all_nodes())
        return count >= self.max_iterations or (self.early_finish_at is not None and count >= self.early_finish_at)
    def total_usage(self): return SimpleNamespace(completion_cost=0.0)
    def _select(self, root):
        index = len(self.root.get_all_nodes()) - 1
        parent_id = self.planned[index][1] if index < len(self.planned) else 0
        return next(item for item in self.root.get_all_nodes() if item.node_id == parent_id)
    def _expand(self, parent):
        index = len(self.root.get_all_nodes()) - 1
        node_id = self.planned[index][0]["node_id"] if index < len(self.planned) else index + 1
        fresh = FakeNode(dict(node_id=node_id, children=[], action_steps=[], completions={},
                              terminal=False, is_duplicate=None), parent)
        parent.children.append(fresh)
        return fresh
    def _simulate(self, fresh):
        self.simulations.append(fresh.node_id)
        if fresh.node_id == self.failure_at:
            raise self.failure
        recorded = next((n for n, _ in self.planned if n["node_id"] == fresh.node_id), None)
        if recorded is None:
            raise RuntimeError("cursor_out_of_range: recording has no next response")
        fresh.data = deepcopy(recorded)
        fresh.data["children"] = []
    def _backpropagate(self, node): pass
    def maybe_persist(self): pass
    def get_finished_nodes(self): return []
    def get_best_trajectory(self): return self.root.get_all_nodes()[-1]
    def emit_event(self, *args):
        self.events.append(("emit", len(self.rows)))
    def _write_step_metrics(self, row):
        self.rows.append(row)
        self.events.append(("metrics", row["node_id"]))


class RecordedPlanTests(unittest.TestCase):
    def test_plan_preserves_all_nodes_edges_and_action_arguments_without_mutation(self):
        data = recording()
        original = deepcopy(data)
        plan = driver.recorded_replay_plan(data)
        self.assertEqual(data, original)
        self.assertEqual(plan["node_count"], 4)
        self.assertEqual([(n["node_id"], n["parent_id"]) for n in plan["nodes"]],
                         [(0, None), (1, 0), (2, 1), (3, 2)])
        self.assertEqual(plan["nodes"][3]["actions"][0]["action"]["pattern"], "3")
        self.assertNotIn("thoughts", plan["nodes"][3]["actions"][0]["action"])

    def test_rejects_missing_or_duplicate_nodes_and_incomplete_actions(self):
        bad = [recording(1)]
        duplicate = recording()
        duplicate["root"]["children"][0]["node_id"] = 0
        bad.append(duplicate)
        missing = recording()
        missing["root"]["children"][0]["action_steps"] = []
        bad.append(missing)
        pending = recording()
        pending["root"]["children"][0]["completions"] = {}
        bad.append(pending)
        for data in bad:
            with self.subTest(data=data), self.assertRaises(ValueError):
                driver.recorded_replay_plan(data)

    def test_structure_actions_duplicate_and_execution_drift_are_fatal(self):
        data = recording()
        expected = driver.recorded_replay_plan(data)
        for change in ("parent", "argument", "class", "duplicate", "observation", "missing", "extra"):
            actual = deepcopy(data)
            first = actual["root"]["children"][0]
            last = first["children"][0]["children"][0]
            if change == "parent":
                first["children"][0]["children"] = []
                actual["root"]["children"].append(last)
            elif change == "argument": last["action_steps"][0]["action"]["pattern"] = "wrong"
            elif change == "class": last["action_steps"][0]["action"]["action_args_class"] = "example.Wrong"
            elif change == "duplicate": last["is_duplicate"] = True
            elif change == "observation": last["action_steps"][0]["observation"] = None
            elif change == "missing": first["children"] = []
            else: last["children"].append(node(4))
            with self.subTest(change=change), self.assertRaises(ValueError):
                driver.validate_replayed_tree(expected, SimpleNamespace(root=FakeNode(actual["root"])))

    def test_recorded_stale_error_is_annotated_but_fresh_error_is_fatal(self):
        data = recording()
        data["root"]["children"][0]["error"] = "old JSON parse rejection before successful rerun"
        expected = driver.recorded_replay_plan(data)
        self.assertEqual(driver.recorded_error_annotations(data)[0]["node_id"], 1)
        fresh = deepcopy(data)
        fresh["root"]["children"][0]["error"] = None
        driver.validate_replayed_tree(expected, SimpleNamespace(root=FakeNode(fresh["root"])))
        with self.assertRaisesRegex(ValueError, "execution error"):
            driver.validate_replayed_tree(expected, SimpleNamespace(root=FakeNode(data["root"])))

    def test_actual_validation_does_not_serialize_full_tree(self):
        data = recording()
        root = FakeNode(data["root"])
        root.model_dump = Mock(side_effect=AssertionError("no large whole-tree dump"))
        driver.validate_replayed_tree(driver.recorded_replay_plan(data), SimpleNamespace(root=root))
        root.model_dump.assert_not_called()

    def test_nonduplicate_missing_observation_is_incomplete_recording(self):
        data = recording()
        data["root"]["children"][0]["action_steps"][0]["observation"] = None
        with self.assertRaisesRegex(ValueError, "not executed"):
            driver.recorded_replay_plan(data)

    def test_reasoning_text_is_not_an_executable_argument(self):
        data = recording()
        expected = driver.recorded_replay_plan(data)
        data["root"]["children"][0]["action_steps"][0]["action"]["thoughts"] = "different reasoning"
        self.assertEqual(driver.validate_replayed_tree(expected, SimpleNamespace(root=FakeNode(data["root"]))), expected)


class SearchBoundaryTests(unittest.TestCase):
    def test_complete_short_recording_keeps_last_metrics_and_online_parameters(self):
        data = recording()
        tree = FakeTree(data)
        tree.run_search(recorded_node_limit=4)
        self.assertEqual(tree.simulations, [1, 2, 3])
        self.assertEqual([row["node_id"] for row in tree.rows], [1, 2, 3])
        self.assertEqual(tree.events[-1], ("metrics", 3))
        self.assertEqual((tree.max_iterations, tree.max_depth, tree.max_expansions), (30, 41, 2))
        driver.validate_replayed_tree(driver.recorded_replay_plan(data), tree)

    def test_complete_30_node_recording_still_executes_all_29_expansions(self):
        data = recording(30)
        tree = FakeTree(data)
        tree.run_search(recorded_node_limit=30)
        self.assertEqual(len(tree.rows), 29)
        driver.validate_replayed_tree(driver.recorded_replay_plan(data), tree)

    def test_default_online_search_is_not_silently_bounded(self):
        tree = FakeTree(recording())
        with self.assertRaisesRegex(RuntimeError, "cursor_out_of_range"):
            tree.run_search()
        self.assertEqual(tree.simulations, [1, 2, 3, 4])

    def test_natural_early_finish_cannot_pass_incomplete_recording(self):
        data = recording()
        tree = FakeTree(data)
        tree.early_finish_at = 3
        tree.run_search(recorded_node_limit=4)
        with self.assertRaises(ValueError):
            driver.validate_replayed_tree(driver.recorded_replay_plan(data), tree)

    def test_last_action_failure_is_not_swallowed_even_after_response_consumed(self):
        tree = FakeTree(recording())
        tree.failure_at = 3
        error = ValueError("last action failed after receiving final recorded response")
        tree.failure = error
        with self.assertRaises(ValueError) as raised:
            tree.run_search(recorded_node_limit=4)
        self.assertIs(raised.exception, error)
        self.assertEqual([row["node_id"] for row in tree.rows], [1, 2])

    def test_extra_identify_inside_last_action_is_still_a_protocol_failure(self):
        tree = FakeTree(recording())
        tree.failure_at = 3
        tree.failure = RuntimeError("cursor_out_of_range: unexpected Identify")
        with self.assertRaisesRegex(RuntimeError, "unexpected Identify"):
            tree.run_search(recorded_node_limit=4)

    def test_duplicate_recorded_expansion_is_included_with_its_real_parent(self):
        data = recording(1)
        first, duplicate, last = node(1), node(2, duplicate=True), node(3)
        first["children"] = [last]
        data["root"]["children"] = [first, duplicate]
        data["unique_id"] = 3
        tree = FakeTree(data)
        tree.run_search(recorded_node_limit=4)
        self.assertEqual([(r["node_id"], r["parent_node_id"]) for r in tree.rows], [(1, 0), (2, 0), (3, 1)])
        driver.validate_replayed_tree(driver.recorded_replay_plan(data), tree)

    def test_invalid_boundary_is_rejected_before_any_action(self):
        for limit in (True, 1, 0, "4"):
            tree = FakeTree(recording())
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                tree.run_search(recorded_node_limit=limit)
            self.assertEqual(tree.simulations, [])


class ReplayDriverBoundaryTests(unittest.TestCase):
    def invoke(self, *, cursor=3, protocol=0, action_failure=False):
        data = recording()
        tree = FakeTree(data)
        if action_failure:
            tree.failure_at = 3
        state = dict(ok=True, cursor=cursor, total=3, n_served=3, n_mismatch=0,
                     n_protocol_errors=protocol, message_policy="audit")
        imports = {
            "moatless.search_tree": SimpleNamespace(SearchTree=SimpleNamespace(from_dict=Mock(return_value=tree))),
            "moatless.repository.file": SimpleNamespace(FileRepository=Mock(return_value=object())),
            "moatless.index": SimpleNamespace(CodeIndex=SimpleNamespace(from_index_name=Mock(
                return_value=SimpleNamespace(_blocks_by_class_name={}, _blocks_by_function_name={})))),
            "baseline_runtime": SimpleNamespace(build_runtime=Mock(return_value=None), check_runtime=Mock()),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trace = root / "trajectory.json"
            trace.write_text(json.dumps(data))
            contract = root / "contract.json"
            def http(url, method, body=None):
                if url.endswith("/load"):
                    return {"n_completions": 3, "purpose_mix": {"build_action": 3}}
                return state
            with patch.dict(sys.modules, imports), patch.dict(__import__("os").environ, {"MOCK_MESSAGE_POLICY": "audit"}), \
                    patch.object(driver, "resolve_trajectory", return_value=trace), \
                    patch.object(driver, "_http_json", side_effect=http):
                rc = driver.run_replay("test__repo-1__ms", root, 1, root, root / "index",
                                       recorded_boundary=True, replay_contract_json=contract)
            return rc, json.loads(contract.read_text()), tree

    def test_driver_records_verified_plan_and_complete_cursor(self):
        rc, contract, tree = self.invoke()
        self.assertEqual(rc, 0)
        self.assertEqual(contract["status"], "complete")
        self.assertEqual(contract["recorded_node_limit"], 4)
        self.assertEqual(contract["configured_max_iterations"], 30)
        self.assertTrue(contract["structure_and_actions_verified"])
        self.assertEqual(contract["actual"], contract["expected"])
        self.assertEqual(len(tree.rows), 3)

    def test_cursor_missing_or_protocol_error_still_fails_after_structural_success(self):
        for kwargs in ({"cursor": 2}, {"protocol": 6}):
            with self.subTest(kwargs=kwargs):
                rc, contract, _ = self.invoke(**kwargs)
                self.assertEqual(rc, 1)
                self.assertEqual(contract["status"], "failed")

    def test_consumed_cursor_cannot_turn_action_exception_into_success(self):
        rc, contract, _ = self.invoke(action_failure=True)
        self.assertEqual(rc, 1)
        self.assertEqual(contract["status"], "failed")
        self.assertIn("action failed", contract["error"])
        self.assertNotIn("structure_and_actions_verified", contract)


if __name__ == "__main__":
    unittest.main()
