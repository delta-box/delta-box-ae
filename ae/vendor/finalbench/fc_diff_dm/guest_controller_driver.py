#!/usr/bin/env python3
"""Guest-side single-step executor for controller-retained FC+dm experiments.

The controller/search tree lives on the host. This process only provides a real
Moatless execution environment inside the Firecracker guest:

  POST /init      -> load instance metadata and connect to host-side mock LLM
  POST /step      -> reconstruct SearchTree from host JSON, run one real
                     select/expand/simulate/backpropagate iteration, return
                     updated SearchTree JSON and checkpoint metadata
  GET  /state     -> current state plus filesystem markers

This avoids the incorrect whole-VM semantics where restoring a VM also rolls
back the MCTS tree. The mock LLM runs on the host/controller side, so its cursor
also survives VM rollback. The host-provided tree keeps branch memory across
restore and LLM calls continue from the global recorded sequence.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PAYLOAD = Path("/mnt/disk2/dyp/spr_payload")
VENV_PY = Path("/mnt/disk2/dyp/moatless_det_venv/bin/python")
CONTROL_HOST = os.environ.get("CONTROL_HOST", "0.0.0.0")
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "18080"))
MOCK_PORT = int(os.environ.get("MOCK_PORT", "19999"))
MOCK_TRACES_ROOT = Path(os.environ.get("MOCK_TRACES_ROOT", "/var/lib/finalbench/det_mock_traces"))
REPO_BASE = PAYLOAD / "repos"
INDEX_STORE = PAYLOAD / "index_store"

if str(PAYLOAD) not in sys.path:
    sys.path.insert(0, str(PAYLOAD))

from replay_driver import _http_json, rewrite_model_base_url, strip_recorded_tree  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s guest_ctrl: %(message)s",
)
log = logging.getLogger("guest_ctrl")


STATE_LOCK = threading.Lock()
STATE = {
    "ok": True,
    "phase": "booting",
    "instance": None,
    "checkpoint_seq": -1,
    "node_id": None,
    "mock_stats": None,
    "error": None,
    "ts": time.time(),
}

CTX: dict = {
    "instance": None,
    "repo": None,
    "code_index": None,
    "mock_url_base": None,
}


def set_state(**kwargs) -> None:
    with STATE_LOCK:
        STATE.update(kwargs)
        STATE["ts"] = time.time()


def get_state() -> dict:
    with STATE_LOCK:
        snap = json.loads(json.dumps(STATE))
    if CTX.get("mock_url_base"):
        try:
            snap["mock_stats"] = _http_json(
                f"{CTX['mock_url_base']}/admin/stats", "GET", timeout=2.0
            )
        except Exception as e:
            snap["mock_stats_error"] = repr(e)
    marker_dir = Path(os.environ.get("MARKER_DIR", "/tmp/fc_dm_markers"))
    markers = []
    if marker_dir.exists():
        for p in sorted(marker_dir.glob("fc_dm_marker_*.txt")):
            try:
                markers.append({"name": p.name, "content": p.read_text(encoding="utf-8").strip()})
            except OSError as e:
                markers.append({"name": p.name, "error": repr(e)})
    snap["fs_markers"] = markers
    return snap


def write_marker(seq: int, node_id: int, label: str) -> None:
    marker_dir = Path(os.environ.get("MARKER_DIR", "/tmp/fc_dm_markers"))
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f"fc_dm_marker_{seq}_{node_id}.txt"
    with open(marker, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "checkpoint_seq": seq,
            "node_id": node_id,
            "label": label,
            "ts": time.time(),
        }) + "\n")
        f.flush()
        os.fsync(f.fileno())
    fd = os.open(marker_dir, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def build_initial_tree_dict(instance: str) -> dict:
    traj_path = MOCK_TRACES_ROOT / "qwen3-coder-30b-ms" / instance / "trajectory.json"
    with open(traj_path) as f:
        data = json.load(f)
    from baseline_runtime import build_runtime
    CTX["test_runtime"] = build_runtime(CTX["repo"], data, code_index=CTX["code_index"])
    data = strip_recorded_tree(data)
    rewrite_model_base_url(data, f"{CTX['mock_url_base']}/v1")
    return data


def tree_from_dict(tree_dict: dict):
    from moatless.search_tree import SearchTree
    return SearchTree.from_dict(
        tree_dict,
        repository=CTX["repo"],
        code_index=CTX["code_index"],
        runtime=CTX["test_runtime"],
    )


def run_one_iteration(tree_dict: dict, seq: int, selected_node_id: int | None = None) -> dict:
    if CTX.get("test_runtime") is not None:
        CTX["test_runtime"].records.clear()
    tree = tree_from_dict(tree_dict)
    tree.assert_runnable()
    if tree.is_finished():
        stats = _http_json(f"{CTX['mock_url_base']}/admin/stats", "GET", timeout=5.0)
        return {
            "ok": True,
            "finished": True,
            "tree": tree.model_dump(),
            "node_id": None,
            "event": None,
            "mock_stats": stats,
        }

    t0 = time.perf_counter()
    if selected_node_id is None:
        node = tree._select(tree.root)
    else:
        node = tree.get_node_by_id(selected_node_id)
        if node is None:
            raise RuntimeError(f"selected_node_id {selected_node_id} not found in retained tree")
    if node is None:
        stats = _http_json(f"{CTX['mock_url_base']}/admin/stats", "GET", timeout=5.0)
        return {
            "ok": True,
            "finished": True,
            "tree": tree.model_dump(),
            "node_id": None,
            "event": {"event_type": "no_expandable_nodes"},
            "mock_stats": stats,
        }

    selected_node_id = node.node_id
    new_node = tree._expand(node)
    if new_node is not None:
        tree._simulate(new_node)
        tree._backpropagate(new_node)
        node_id = new_node.node_id
    else:
        node_id = selected_node_id

    stats = _http_json(f"{CTX['mock_url_base']}/admin/stats", "GET", timeout=5.0)
    from baseline_runtime import check_runtime, runtime_records
    check_runtime(CTX["test_runtime"])
    event = {
        "test_runtime_records": runtime_records(CTX["test_runtime"]),
        "event_type": "tree_iteration",
        "selected_node_id": selected_node_id,
        "new_node_id": node_id,
        "total_nodes": len(tree.root.get_all_nodes()),
        "finished_nodes": len(tree.get_finished_nodes()),
        "best_node_id": tree.get_best_trajectory().node_id if tree.get_best_trajectory() else None,
        "step_wall_ms": (time.perf_counter() - t0) * 1000.0,
    }
    write_marker(seq, node_id, f"node_{node_id}")
    set_state(
        phase="iteration_done",
        checkpoint_seq=seq,
        node_id=node_id,
        mock_stats=stats,
        error=None,
    )
    return {
        "ok": True,
        "finished": tree.is_finished(),
        "tree": tree.model_dump(),
        "node_id": node_id,
        "event": event,
        "mock_stats": stats,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def read_json(self) -> dict:
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8"))

    def send_json(self, obj: dict, code: int = 200) -> None:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/state":
            self.send_json(get_state())
        else:
            self.send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        try:
            if self.path == "/init":
                body = self.read_json()
                instance = body["instance"]
                CTX["mock_url_base"] = body["mock_url_base"].rstrip("/")
                health = _http_json(f"{CTX['mock_url_base']}/admin/healthz", "GET", timeout=5.0)
                if not health.get("ok"):
                    raise RuntimeError(f"host mock is not healthy: {health}")

                from moatless.index import CodeIndex
                from moatless.repository.file import FileRepository

                repo_path = REPO_BASE / f"swe-bench_{instance}"
                CTX["repo"] = FileRepository(repo_path=str(repo_path))
                CTX["code_index"] = CodeIndex.from_index_name(
                    instance,
                    file_repo=CTX["repo"],
                    index_store_dir=str(INDEX_STORE),
                )
                CTX["instance"] = instance
                tree = build_initial_tree_dict(instance)
                write_marker(0, 0, "root")
                stats = _http_json(f"{CTX['mock_url_base']}/admin/stats", "GET", timeout=5.0)
                set_state(
                    ok=True,
                    phase="root_ready",
                    instance=instance,
                    checkpoint_seq=0,
                    node_id=0,
                    mock_stats=stats,
                    error=None,
                )
                self.send_json({"ok": True, "tree": tree, "state": get_state()})
            elif self.path == "/step":
                body = self.read_json()
                result = run_one_iteration(
                    body["tree"],
                    int(body["seq"]),
                    body.get("selected_node_id"),
                )
                result["state"] = get_state()
                self.send_json(result)
            elif self.path == "/select":
                body = self.read_json()
                tree = tree_from_dict(body["tree"])
                if tree.is_finished():
                    self.send_json({"ok": True, "finished": True, "selected_node_id": None})
                    return
                node = tree._select(tree.root)
                self.send_json({
                    "ok": True,
                    "finished": node is None,
                    "selected_node_id": node.node_id if node else None,
                })
            else:
                self.send_json({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            log.exception("request failed")
            set_state(ok=False, phase="error", error=f"{type(e).__name__}: {e}")
            self.send_json({"ok": False, "error": f"{type(e).__name__}: {e}", "state": get_state()}, 500)


def main() -> int:
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    srv = ThreadingHTTPServer((CONTROL_HOST, CONTROL_PORT), Handler)
    log.info("controller guest server listening on %s:%d", CONTROL_HOST, CONTROL_PORT)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
