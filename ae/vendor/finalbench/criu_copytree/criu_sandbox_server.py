#!/usr/bin/env python3
"""Host-side Moatless sandbox process for CRIU+copytree.

The parent controller keeps the SearchTree JSON and mock LLM state. This
process owns only the live repository filesystem plus Moatless runtime objects
that are safe to roll back with CRIU. The API mirrors the FC+dm guest driver:

  POST /init
  POST /select
  POST /step
  GET  /state

The process is checkpointed after each completed step. On rollback the
controller restores this process image and separately restores the live repo
directory from that node's filesystem snapshot.
"""
from __future__ import annotations

import json
import logging
import os
import gc
import sys
import atexit
import threading
import time
import traceback
import weakref
import concurrent.futures
import concurrent.futures.thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


PAYLOAD = Path(os.environ["SPR_PAYLOAD"])
CONTROL_HOST = os.environ.get("CRIU_CONTROL_HOST", "127.0.0.1")
CONTROL_PORT = int(os.environ.get("CRIU_CONTROL_PORT", "18080"))
REPO_BASE = Path(os.environ.get("CRIU_REPO_BASE", str(PAYLOAD / "repos")))
INDEX_STORE = Path(os.environ.get("CRIU_INDEX_STORE", str(PAYLOAD / "index_store")))
TRACES_ROOT = Path(os.environ.get("CRIU_TRACES_ROOT", "/tmp/det_mock_traces"))

if str(PAYLOAD) not in sys.path:
    sys.path.insert(0, str(PAYLOAD))

_TRACKED_EXECUTORS: "weakref.WeakSet[concurrent.futures.ThreadPoolExecutor]" = weakref.WeakSet()
_ORIG_THREAD_POOL_EXECUTOR = concurrent.futures.ThreadPoolExecutor


class TrackingThreadPoolExecutor(_ORIG_THREAD_POOL_EXECUTOR):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _TRACKED_EXECUTORS.add(self)


# Patch before importing moatless/litellm. LiteLLM creates helper executors for
# callbacks/async wrappers; idle helper threads make CRIU 3.16 fail on this host.
concurrent.futures.ThreadPoolExecutor = TrackingThreadPoolExecutor
concurrent.futures.thread.ThreadPoolExecutor = TrackingThreadPoolExecutor

if os.environ.get("CRIU_BLOCK_PYARROW") == "1":
    import builtins

    _orig_import = builtins.__import__

    def _block_pyarrow_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ImportError("pyarrow disabled inside CRIU sandbox")
        return _orig_import(name, globals, locals, fromlist, level)

    builtins.__import__ = _block_pyarrow_import

from replay_driver import _http_json, rewrite_model_base_url, strip_recorded_tree  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s criu_sandbox: %(message)s",
)
log = logging.getLogger("criu_sandbox")



def _log_process_exit() -> None:
    try:
        log.info("sandbox process exiting pid=%d", os.getpid())
    except Exception:
        pass


atexit.register(_log_process_exit)


STATE_LOCK = threading.Lock()
CHECKPOINT_LOCK = threading.Lock()
CHECKPOINT_REQUEST: dict | None = None
STATE = {
    "ok": True,
    "phase": "booting",
    "instance": None,
    "checkpoint_seq": -1,
    "node_id": None,
    "mock_stats": None,
    "error": None,
    "pid": os.getpid(),
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
        STATE["pid"] = os.getpid()
        STATE["ts"] = time.time()


def request_checkpoint_quiesce(body: dict) -> dict:
    global CHECKPOINT_REQUEST
    ready_path = Path(body["ready_path"])
    resume_path = Path(body["resume_path"])
    ready_path.parent.mkdir(parents=True, exist_ok=True)
    with CHECKPOINT_LOCK:
        CHECKPOINT_REQUEST = {
            "seq": int(body.get("seq", -1)),
            "ready_path": str(ready_path),
            "resume_path": str(resume_path),
        }
    set_state(phase="checkpoint_quiesce_requested", checkpoint_seq=int(body.get("seq", -1)))
    return {"ok": True, "ready_path": str(ready_path), "resume_path": str(resume_path)}


def pop_checkpoint_request() -> dict | None:
    global CHECKPOINT_REQUEST
    with CHECKPOINT_LOCK:
        req = CHECKPOINT_REQUEST
        CHECKPOINT_REQUEST = None
    return req


def get_state() -> dict:
    with STATE_LOCK:
        snap = json.loads(json.dumps(STATE))
    snap["thread_count"] = len(threading.enumerate())
    if CTX.get("mock_url_base"):
        try:
            snap["mock_stats"] = _http_json(
                f"{CTX['mock_url_base']}/admin/stats", "GET", timeout=2.0
            )
        except Exception as e:
            snap["mock_stats_error"] = repr(e)
    return snap


def _disable_litellm_backgrounding() -> None:
    """Keep LiteLLM synchronous and checkpoint-friendly.

    This does not alter the LLM response path: calls still go to the mock OpenAI
    endpoint. It only removes background callbacks/telemetry that otherwise
    leave idle ThreadPoolExecutor workers behind after a request.
    """
    try:
        import litellm

        litellm.telemetry = False
        for name in (
            "callbacks",
            "success_callback",
            "failure_callback",
            "_async_success_callback",
            "_async_failure_callback",
            "input_callback",
        ):
            try:
                setattr(litellm, name, [])
            except Exception:
                pass
    except Exception:
        pass


def quiesce_for_criu() -> dict:
    """Stop Python helper threads before the controller asks CRIU to dump us."""
    _disable_litellm_backgrounding()

    shutdown = 0
    for ex in list(_TRACKED_EXECUTORS):
        try:
            ex.shutdown(wait=True, cancel_futures=True)
            shutdown += 1
        except Exception:
            log.debug("executor shutdown failed", exc_info=True)
    _TRACKED_EXECUTORS.clear()

    # If LiteLLM keeps a module-global executor, replace the shutdown instance
    # with a fresh lazy executor for the next completion call. Creating it does
    # not start a thread until submit() is called.
    try:
        import litellm.utils as lu

        max_threads = int(getattr(lu, "MAX_THREADS", 100) or 100)
        lu.executor = TrackingThreadPoolExecutor(max_workers=max_threads)
    except Exception:
        pass

    try:
        import asyncio

        loop = asyncio.get_running_loop()
        default_executor = getattr(loop, "_default_executor", None)
        if default_executor is not None:
            default_executor.shutdown(wait=True, cancel_futures=True)
            loop._default_executor = None
    except RuntimeError:
        pass
    except Exception:
        pass

    gc.collect()
    time.sleep(0.05)
    return {
        "tracked_executors_shutdown": shutdown,
        "live_threads": [
            {
                "name": th.name,
                "ident": th.ident,
                "native_id": getattr(th, "native_id", None),
                "daemon": th.daemon,
            }
            for th in threading.enumerate()
        ],
    }


def build_initial_tree_dict(instance: str) -> dict:
    traj_path = TRACES_ROOT / "qwen3-coder-30b-ms" / instance / "trajectory.json"
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
            raise RuntimeError(f"selected_node_id {selected_node_id} not found")
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
        elif self.path == "/threads":
            frames = sys._current_frames()
            threads = []
            for th in threading.enumerate():
                frame = frames.get(th.ident)
                threads.append({
                    "name": th.name,
                    "ident": th.ident,
                    "native_id": getattr(th, "native_id", None),
                    "daemon": th.daemon,
                    "stack": "".join(traceback.format_stack(frame)) if frame else None,
                })
            self.send_json({"ok": True, "pid": os.getpid(), "threads": threads})
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
                    raise RuntimeError(f"mock not healthy: {health}")

                from moatless.index import CodeIndex
                from moatless.repository.file import FileRepository

                try:
                    import faiss
                    if hasattr(faiss, "omp_set_num_threads"):
                        faiss.omp_set_num_threads(1)
                except Exception:
                    pass

                repo_path = REPO_BASE / f"swe-bench_{instance}"
                CTX["repo"] = FileRepository(repo_path=str(repo_path))
                CTX["code_index"] = CodeIndex.from_index_name(
                    instance,
                    file_repo=CTX["repo"],
                    index_store_dir=str(INDEX_STORE),
                )
                _disable_litellm_backgrounding()
                CTX["instance"] = instance
                tree = build_initial_tree_dict(instance)
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
            elif self.path == "/step":
                body = self.read_json()
                result = run_one_iteration(
                    body["tree"],
                    int(body["seq"]),
                    body.get("selected_node_id"),
                )
                result["quiesce"] = quiesce_for_criu()
                result["state"] = get_state()
                self.send_json(result)
            elif self.path == "/prepare_checkpoint":
                body = self.read_json()
                self.send_json(request_checkpoint_quiesce(body))
            else:
                self.send_json({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            log.exception("request failed")
            set_state(ok=False, phase="error", error=f"{type(e).__name__}: {e}")
            self.send_json({"ok": False, "error": f"{type(e).__name__}: {e}", "state": get_state()}, 500)


class CheckpointHTTPServer(HTTPServer):
    allow_reuse_address = True


def serve_until_checkpoint() -> None:
    """Serve requests until /prepare_checkpoint asks us to close the control
    socket. The controller then dumps this quiesced process; after it writes the
    resume file, both the original and any future restored copy reopen the same
    control socket and continue serving.
    """
    srv = CheckpointHTTPServer((CONTROL_HOST, CONTROL_PORT), Handler)
    srv.timeout = 0.2
    set_state(phase="listening")
    log.info("sandbox server pid=%d listening on %s:%d", os.getpid(), CONTROL_HOST, CONTROL_PORT)
    try:
        while True:
            srv.handle_request()
            req = pop_checkpoint_request()
            if req is not None:
                set_state(phase="checkpoint_quiesced", checkpoint_seq=req["seq"])
                break
    finally:
        srv.server_close()

    ready_path = Path(req["ready_path"])
    resume_path = Path(req["resume_path"])
    ready_path.write_text(json.dumps({"ok": True, "pid": os.getpid(), "seq": req["seq"]}), encoding="utf-8")
    log.info("sandbox checkpoint seq=%s ready; waiting for resume %s", req["seq"], resume_path)
    while not resume_path.exists():
        time.sleep(0.05)
    log.info("sandbox checkpoint seq=%s resume observed", req["seq"])
    set_state(phase="checkpoint_resumed", checkpoint_seq=req["seq"])


def main() -> int:
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    # Single-threaded on purpose: CRIU 3.16 is fragile with Python helper
    # threads on this SPR host. The controller is strictly sequential anyway.
    try:
        while True:
            serve_until_checkpoint()
    except BaseException:
        log.exception("sandbox main exiting due to exception")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
