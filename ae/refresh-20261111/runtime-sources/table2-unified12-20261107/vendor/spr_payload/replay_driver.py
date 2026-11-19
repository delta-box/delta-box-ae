"""replay_driver.py — Drive moatless `SearchTree.run_search()` from a
trajectory.json, with all LLM calls served by `mock_llm_server`.

End-to-end flow (single trace):
  1. Spawn mock_llm_server (TCP, host-local). Wait for /admin/healthz.
  2. POST /admin/load {instance_id, variant} → mock loads + sequences trajectory.
  3. Load trajectory.json, **strip** the recorded MCTS tree (`root.children`,
     `root.action_steps`, `root.completions`) — keep only the config + initial
     root state (user_message + file_context).
  4. Rewrite every action's `completion_model.model_base_url` to point at our
     mock (e.g. `http://127.0.0.1:9999/v1`).
  5. SearchTree.from_dict(rewritten_data, repository=<file repo at trajectory.commit>)
     → tree.run_search().
  6. Watch mock /admin/stats: did all served calls match? final cursor reached
     end of recording? n_mismatch=0?
  7. Report outcome + (eventually) hand control to the bench-harness wrapper that
     interleaves C/R operations between MCTS iters.

Usage:
  python -m benchmarks.finalbench.replay_driver \
      --manifest-line sympy__sympy-20212__ms \
      --traces-root ~/d-overlayfs/traces/swe-search \
      --mock-port 9999

Per [[feedback_full_honest_only]]: this driver does no I/O fakery.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

log = logging.getLogger("replay_driver")


# ---------------- mock client ----------------

def _http_json(url: str, method: str, body: dict | None = None, timeout: float = 30.0) -> dict:
    raw = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=raw, method=method,
                                 headers={"Content-Type": "application/json"} if body else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def wait_for_mock(port: int, timeout: float = 10.0) -> None:
    url = f"http://127.0.0.1:{port}/admin/healthz"
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            r = _http_json(url, "GET", timeout=1.0)
            if r.get("ok"):
                return
        except (urllib.error.URLError, ConnectionRefusedError, socket.timeout, OSError) as e:
            last_err = e
        time.sleep(0.1)
    raise TimeoutError(f"mock not ready in {timeout}s: {last_err}")


# ---------------- trajectory rewrite ----------------

def parse_manifest_line(line: str) -> tuple[str, str]:
    line = line.strip()
    if line.endswith("__p-eagle-ms"):
        return line[: -len("__p-eagle-ms")], "p-eagle-ms"
    if line.endswith("__ms"):
        return line[: -len("__ms")], "ms"
    raise ValueError(f"unknown manifest variant suffix: {line}")


def resolve_trajectory(traces_root: Path, instance_id: str, variant: str) -> Path:
    if variant == "ms":
        return traces_root / "qwen3-coder-30b-ms" / instance_id / "trajectory.json"
    return (traces_root / "qwen3-coder-30b-p-eagle-ms" / "mcts-iter30"
            / instance_id / "trajectory.json")


def strip_recorded_tree(data: dict) -> dict:
    """Return a copy of trajectory data with the recorded MCTS tree wiped,
    keeping only the initial root (user_message + file_context + workspace)
    and the global config (agent, selector, expander, max_*, etc.).

    Note: we do NOT clear `terminal`, `is_duplicate`, etc. because their
    types are strict-bool in the Node Pydantic model. The recorded root's
    values for these are correct for a fresh start anyway (terminal=False,
    visits=0, ...).
    """
    data = json.loads(json.dumps(data))  # deep copy
    root = data.get("root") or {}
    # The recorded trajectory's final SearchTree stores the last allocated node
    # id, but a fresh replay must allocate from the first recorded expansion.
    # Node ids participate in selector tie-breaks and duplicate handling, so
    # preserving the final value (or resetting blindly to root=0) perturbs the
    # trajectory. Infer the first build_action node from the recorded tree before
    # stripping it and set the allocator to one before that.
    first_expansion_node_id = None
    stack = [root]
    while stack:
        node = stack.pop()
        comps = node.get("completions") or {}
        build = comps.get("build_action")
        if build:
            first_expansion_node_id = node.get("node_id")
            break
        stack.extend(reversed(node.get("children") or []))
    root["children"] = []
    root["action_steps"] = []
    root["completions"] = {}
    # Per-recording state we should explicitly reset (these accept None)
    for k in ("assistant_message", "output", "reward", "value",
              "error", "feedback_data"):
        if k in root:
            root[k] = None
    if "visits" in root:
        root["visits"] = 0
    if first_expansion_node_id is not None:
        data["unique_id"] = int(first_expansion_node_id) - 1
    else:
        data["unique_id"] = root.get("node_id", 0)
    data["root"] = root
    return data


def rewrite_model_base_url(data: dict, new_url: str, api_key: str = "dummy") -> int:
    """Mutate every CompletionModel-shaped dict (agent.completion +
    agent.actions[i].completion_model) to point at our mock. Also forces
    `model_api_key` so litellm does not consult the environment for a real key.
    Returns total mutation count.
    """
    n = 0
    agent = data.get("agent", {}) or {}
    if isinstance(agent.get("completion"), dict):
        agent["completion"]["model_base_url"] = new_url
        agent["completion"]["model_api_key"] = api_key
        n += 1
    for a in agent.get("actions", []) or []:
        cm = a.get("completion_model")
        if isinstance(cm, dict):
            cm["model_base_url"] = new_url
            cm["model_api_key"] = api_key
            n += 1
    return n



def recorded_replay_plan(data: dict) -> dict:
    """Capture the complete recorded tree, independently of online search limits."""
    root = data.get("root")
    if not isinstance(root, dict):
        raise ValueError("Recorded replay requires a root node")
    nodes = []
    seen = set()
    stack = [(root, None)]
    while stack:
        node, parent_id = stack.pop()
        if not isinstance(node, dict):
            raise ValueError("Recorded replay has an invalid node")
        node_id = node.get("node_id")
        if type(node_id) is not int or node_id < 0 or node_id in seen:
            raise ValueError("Recorded replay requires unique nonnegative node ids")
        seen.add(node_id)
        children = node.get("children", [])
        steps = node.get("action_steps", [])
        if not isinstance(children, list) or not isinstance(steps, list):
            raise ValueError(f"Invalid recorded children/actions at node {node_id}")
        if parent_id is None:
            if steps or node.get("completions"):
                raise ValueError("Recorded replay root must precede all actions")
        elif not steps or not (node.get("completions") or {}).get("build_action"):
            raise ValueError(f"Recorded node {node_id} has no complete action plan")
        actions = []
        for step in steps:
            action = step.get("action") if isinstance(step, dict) else None
            if not isinstance(action, dict) or not isinstance(action.get("action_args_class"), str):
                raise ValueError(f"Invalid recorded action at node {node_id}")
            if step.get("observation") is None and not node.get("is_duplicate"):
                raise ValueError(f"Recorded action at node {node_id} was not executed")
            actions.append({
                "action": canonical_action(action),
                "observation_present": step.get("observation") is not None,
            })
        nodes.append({
            "node_id": node_id, "parent_id": parent_id, "actions": actions,
            "terminal": bool(node.get("terminal", False)),
            "is_duplicate": bool(node.get("is_duplicate", False)),
        })
        stack.extend((child, node_id) for child in reversed(children))
    if len(nodes) < 2:
        raise ValueError("Recorded replay has no recorded expansions")
    return {
        "root_id": root["node_id"],
        "node_count": len(nodes),
        "nodes": sorted(nodes, key=lambda node: node["node_id"]),
    }



def canonical_action(action: dict) -> dict:
    if not isinstance(action, dict) or not isinstance(action.get("action_args_class"), str):
        raise ValueError("Recorded replay requires typed executable action arguments")
    result = json.loads(json.dumps(action))
    # Thoughts are model reasoning, not executable action arguments.
    result.pop("thoughts", None)
    return result


def recorded_error_annotations(data: dict) -> list[dict]:
    annotations = []
    stack = [data["root"]]
    while stack:
        node = stack.pop()
        if node.get("error"):
            annotations.append({"node_id": node["node_id"], "recorded_error": node["error"]})
        stack.extend(node.get("children", []))
    return annotations


def validate_replayed_tree(expected: dict, tree) -> dict:
    """Check small per-node contracts without serializing messages or contexts."""
    nodes = []
    seen = set()
    for node in tree.root.get_all_nodes():
        if node.node_id in seen or type(node.node_id) is not int:
            raise ValueError("Replayed tree has invalid/duplicate node ids")
        seen.add(node.node_id)
        # Historical serialized errors may be stale annotations after a node was
        # rerun. Any error produced in this fresh execution remains a failure.
        if node.error:
            raise ValueError(f"Replayed node {node.node_id} contains an execution error")
        actions = []
        for step in node.action_steps:
            action = step.action.model_dump()
            action["action_args_class"] = f"{step.action.__class__.__module__}.{step.action.__class__.__name__}"
            actions.append({"action": canonical_action(action),
                            "observation_present": step.observation is not None})
        nodes.append({
            "node_id": node.node_id,
            "parent_id": node.parent.node_id if node.parent else None,
            "actions": actions, "terminal": bool(node.terminal),
            "is_duplicate": bool(node.is_duplicate),
        })
    actual = {"root_id": tree.root.node_id, "node_count": len(nodes),
              "nodes": sorted(nodes, key=lambda node: node["node_id"])}
    if actual != expected:
        raise ValueError("Replayed tree differs from the complete recorded structure/actions")
    return actual



def write_replay_contract(path: Path | None, contract: dict | None) -> None:
    if path is not None and contract is not None:
        path.write_text(json.dumps(contract, indent=2, ensure_ascii=False) + "\n")


# ---------------- driver ----------------

def run_replay(manifest_line: str, traces_root: Path, mock_port: int,
               repo_path: Path, index_store_dir: Path,
               runtime_backend: str = "configured",
               recorded_boundary: bool = False,
               replay_contract_json: Path | None = None) -> int:
    from baseline_audit import stats_ok
    instance_id, variant = parse_manifest_line(manifest_line)
    traj_path = resolve_trajectory(traces_root, instance_id, variant)
    if not traj_path.exists():
        log.error("trajectory not found: %s", traj_path)
        return 2
    if not repo_path.exists():
        log.error("repo not found at %s — please clone first", repo_path)
        return 2

    mock_url_base = f"http://127.0.0.1:{mock_port}"

    # 1. Tell mock to load this trace
    log.info("mock load %s/%s", instance_id, variant)
    resp = _http_json(f"{mock_url_base}/admin/load", "POST",
                      {"instance_id": instance_id, "variant": variant})
    log.info("mock loaded: n_completions=%d purposes=%s",
             resp["n_completions"], resp["purpose_mix"])

    # 2. Load trajectory + strip + rewrite
    with open(traj_path) as f:
        data = json.load(f)
    contract = None
    plan = recorded_replay_plan(data) if recorded_boundary else None
    if recorded_boundary:
        contract = {
            "schema_version": 1, "status": "planned",
            "scope": "all recorded expansions; not continuation of online search",
            "configured_max_iterations": data.get("max_iterations"),
            "recorded_node_limit": plan["node_count"], "expected": plan,
            "recorded_error_annotations": recorded_error_annotations(data),
            "validation_scope": "node/parent/action arguments, observation presence, terminal/duplicate state; fresh node errors rejected",
            "validation_timing": "after last step metrics; included in worker lifetime/RSS sampling; no full-tree serialization",
        }
        write_replay_contract(replay_contract_json, contract)
    data = strip_recorded_tree(data)
    n = rewrite_model_base_url(data, f"{mock_url_base}/v1")
    log.info("rewrote %d action completion_model.model_base_url → %s/v1", n, mock_url_base)

    # 3. Build moatless SearchTree
    # Late import: moatless brings in heavy deps (litellm, llama_index, faiss).
    from moatless.search_tree import SearchTree
    from moatless.repository.file import FileRepository
    from moatless.index import CodeIndex
    repo = FileRepository(repo_path=str(repo_path))
    log.info("FileRepository @ %s", repo_path)
    # CodeIndex auto-downloads pre-built moatless index from azure blob if not
    # already present at index_store_dir/<instance_id>/. The 287-trace set uses
    # only FindClass/FindFunction/FindCodeSnippet/ViewCode + edit actions —
    # NO SemanticSearch — so the embedding model never gets invoked. Tree-sitter
    # class/function name dictionaries (built into the .zip) are what's read.
    index_store_dir.mkdir(parents=True, exist_ok=True)
    code_index = CodeIndex.from_index_name(
        instance_id, file_repo=repo, index_store_dir=str(index_store_dir),
    )
    log.info("CodeIndex loaded: %d classes, %d functions",
             len(code_index._blocks_by_class_name),
             len(code_index._blocks_by_function_name))
    from baseline_runtime import build_runtime, check_runtime
    runtime = build_runtime(repo, data, code_index=code_index) if runtime_backend == "configured" else None
    if runtime_backend == "e2b":
        from moatless.benchmark.utils import get_moatless_instance
        from moatless.runtime.e2b_runtime import E2BRuntime
        runtime = E2BRuntime(
            instance=get_moatless_instance(instance_id),
            repository=repo,
            run_id=f"replay-{variant}",
        )
    tree = SearchTree.from_dict(
        data, repository=repo, code_index=code_index, runtime=runtime
    )
    log.info("SearchTree built: max_iterations=%d", tree.max_iterations)
    if runtime_backend == "configured" and runtime is not None:
        tree.add_event_handler(lambda event: check_runtime(runtime) if event.get("event_type") == "tree_iteration" else None)

    # 4. Run MCTS
    t0 = time.perf_counter()
    try:
        if recorded_boundary:
            final_node = tree.run_search(recorded_node_limit=plan["node_count"])
            contract["actual"] = validate_replayed_tree(plan, tree)
            contract["structure_and_actions_verified"] = True
        else:
            final_node = tree.run_search()
        if runtime_backend == "configured":
            check_runtime(runtime)
        wall = time.perf_counter() - t0
        log.info("run_search() finished in %.1fs final_node=%s", wall,
                 getattr(final_node, "node_id", None))
    except Exception as e:
        wall = time.perf_counter() - t0
        log.error("run_search() crashed after %.1fs: %s: %s", wall,
                  type(e).__name__, e)
        import traceback; traceback.print_exc()
        if contract is not None:
            contract.update(status="failed", error=f"{type(e).__name__}: {e}")
            try:
                write_replay_contract(replay_contract_json, contract)
            except OSError as save_error:
                log.error("Could not save failed replay contract: %s", save_error)
        # query mock final stats anyway
        try:
            stats = _http_json(f"{mock_url_base}/admin/stats", "GET")
            log.error("mock final stats: %s", stats)
        except Exception:
            pass
        return 1

    # 5. Final mock stats
    stats = _http_json(f"{mock_url_base}/admin/stats", "GET")
    log.info("mock final: cursor=%d/%d n_served=%d n_mismatch=%d",
             stats["cursor"], stats["total"], stats["n_served"], stats["n_mismatch"])
    if not stats_ok(stats) or stats['cursor'] != stats['total']:
        log.error("mock cursor/protocol/strict-message validation failed: %s", stats)
        if contract is not None:
            contract.update(status="failed", error="mock cursor/protocol/strict-message validation failed", mock_stats=stats)
            write_replay_contract(replay_contract_json, contract)
        return 1
    if contract is not None:
        contract.update(status="complete", mock_stats=stats)
        write_replay_contract(replay_contract_json, contract)
    return 0


def main() -> int:
    from baseline_audit import flush_audit
    def interrupted(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt('benchmark interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    logging.basicConfig(level=getattr(logging, os.environ.get('BASELINE_LOG_LEVEL', 'WARNING').upper()),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest-line", required=True,
                    help="e.g. sympy__sympy-20212__ms")
    ap.add_argument("--traces-root", default=str(REPO_ROOT / "traces" / "swe-search"))
    ap.add_argument("--mock-port", type=int, default=9999)
    ap.add_argument("--repo-base", default="/tmp/repos",
                    help="Where SWE-bench testbed repos live (cloned outside)")
    ap.add_argument("--index-store-dir", default="/tmp/index_store",
                    help="moatless CodeIndex cache (auto-download per instance)")
    ap.add_argument("--skip-mock-spawn", action="store_true",
                    help="If set, assume mock is already running on --mock-port")
    ap.add_argument("--runtime", choices=("configured", "none", "e2b"), default="configured",
                    help="Runtime backend for RunTests actions")
    ap.add_argument('--audit-json', type=Path, default=Path('replay.mock_audit.json'),
                    help='Post-measurement mock audit evidence (standalone CLI only)')
    ap.add_argument('--defer-audit', action='store_true',
                    help='External owner exports audit after its measurement (requires --skip-mock-spawn)')
    ap.add_argument("--recorded-boundary", action="store_true",
                    help="Replay every recorded expansion without extending beyond the recording")
    ap.add_argument("--replay-contract-json", type=Path,
                    help="Save the recorded structure/action verification (requires --recorded-boundary)")
    args = ap.parse_args()
    if bool(args.recorded_boundary) != bool(args.replay_contract_json):
        ap.error("--recorded-boundary and --replay-contract-json must be supplied together")
    if args.defer_audit and not args.skip_mock_spawn:
        ap.error('--defer-audit requires an external mock owner (--skip-mock-spawn)')

    instance_id, variant = parse_manifest_line(args.manifest_line)
    repo_path = Path(args.repo_base) / f"swe-bench_{instance_id}"
    index_store_dir = Path(args.index_store_dir)

    mock_proc: subprocess.Popen | None = None
    try:
        if not args.skip_mock_spawn:
            cmd = [sys.executable, str(Path(__file__).with_name('mock_llm_server.py')),
                   '--tcp-port', str(args.mock_port), '--traces-root', args.traces_root]
            log.info('spawning mock: %s', ' '.join(cmd))
            mock_proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT),
                                         stdout=sys.stdout, stderr=sys.stderr,
                                         start_new_session=True)
            wait_for_mock(args.mock_port)
        rc = run_replay(args.manifest_line, Path(args.traces_root),
                        args.mock_port, repo_path, index_store_dir,
                        runtime_backend=args.runtime,
                        recorded_boundary=args.recorded_boundary,
                        replay_contract_json=args.replay_contract_json)
    finally:
        try:
            if not args.defer_audit and (mock_proc is not None or args.skip_mock_spawn):
                flush_audit(f'http://127.0.0.1:{args.mock_port}', args.audit_json,
                            primary_error=sys.exc_info()[1])
        finally:
            if mock_proc:
                mock_proc.terminate()
                try:
                    mock_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    mock_proc.kill()
                    mock_proc.wait(timeout=3)
    return rc


if __name__ == "__main__":
    sys.exit(main())
