#!/usr/bin/env python3
"""CRIU target worker for the CRIU+copytree baseline.

This process intentionally has no sockets. The host-side controller owns the
SearchTree, mock LLM state, rollback decisions, CRIU calls, and filesystem
snapshots. This worker owns only the real Moatless runtime state attached to a
live repository copy: FileRepository, CodeIndex, native-library state, and any
Moatless-side caches.

The controller drives it through a single-file mailbox:

  cmd.json  -> worker reads + unlinks
  resp.json <- worker writes atomically

At checkpoint time the worker is idle, polling for cmd.json. That means CRIU
dumps only the real worker state, not a control server or controller state.
"""
from __future__ import annotations

import atexit
import builtins
import concurrent.futures
import concurrent.futures.thread
import gc
import json
import logging
import os
import sys
import threading
import time
import traceback
import weakref
from pathlib import Path


PAYLOAD = Path(os.environ["SPR_PAYLOAD"])
MAILBOX = Path(os.environ["CRIU_WORKER_MAILBOX"])
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


concurrent.futures.ThreadPoolExecutor = TrackingThreadPoolExecutor
concurrent.futures.thread.ThreadPoolExecutor = TrackingThreadPoolExecutor

if os.environ.get("CRIU_BLOCK_PYARROW") == "1":
    _orig_import = builtins.__import__

    def _block_pyarrow_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ImportError("pyarrow disabled inside CRIU worker")
        return _orig_import(name, globals, locals, fromlist, level)

    builtins.__import__ = _block_pyarrow_import

from replay_driver import _http_json, rewrite_model_base_url, strip_recorded_tree  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s criu_worker: %(message)s",
)
log = logging.getLogger("criu_worker")


CTX: dict = {
    "instance": None,
    "repo": None,
    "code_index": None,
    "mock_url_base": None,
    "checkpoint_seq": -1,
    "node_id": None,
    "phase": "booting",
    "error": None,
}


def _log_exit() -> None:
    try:
        log.info("worker exiting pid=%d", os.getpid())
    except Exception:
        pass


atexit.register(_log_exit)


def state() -> dict:
    out = {
        "ok": True,
        "pid": os.getpid(),
        "phase": CTX.get("phase"),
        "instance": CTX.get("instance"),
        "checkpoint_seq": CTX.get("checkpoint_seq"),
        "node_id": CTX.get("node_id"),
        "error": CTX.get("error"),
        "thread_count": len(threading.enumerate()),
        "ts": time.time(),
    }
    if CTX.get("mock_url_base"):
        try:
            out["mock_stats"] = _http_json(
                f"{CTX['mock_url_base']}/admin/stats", "GET", timeout=2.0
            )
        except Exception as e:
            out["mock_stats_error"] = repr(e)
    return out


def _disable_litellm_backgrounding() -> None:
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
    _disable_litellm_backgrounding()

    shutdown = 0
    for ex in list(_TRACKED_EXECUTORS):
        try:
            ex.shutdown(wait=True, cancel_futures=True)
            shutdown += 1
        except Exception:
            log.debug("executor shutdown failed", exc_info=True)
    _TRACKED_EXECUTORS.clear()

    try:
        import litellm.utils as lu

        max_threads = int(getattr(lu, "MAX_THREADS", 100) or 100)
        lu.executor = TrackingThreadPoolExecutor(max_workers=max_threads)
    except Exception:
        pass

    # Do not call asyncio.get_event_loop() here: on Python 3.11 it can create a
    # new selector loop with epoll + socketpair fds, polluting the CRIU image
    # even when this worker never used asyncio. Only touch an already-created
    # running loop, if one exists.
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


def init_instance(instance: str, mock_url_base: str) -> dict:
    CTX["mock_url_base"] = mock_url_base.rstrip("/")
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
    CTX["checkpoint_seq"] = 0
    CTX["node_id"] = 0
    CTX["phase"] = "root_ready"
    CTX["error"] = None
    tree = build_initial_tree_dict(instance)
    q = quiesce_for_criu()
    return {"ok": True, "tree": tree, "state": state(), "quiesce": q}


def select_node(tree_dict: dict) -> dict:
    tree = tree_from_dict(tree_dict)
    if tree.is_finished():
        return {"ok": True, "finished": True, "selected_node_id": None, "state": state()}
    node = tree._select(tree.root)
    return {
        "ok": True,
        "finished": node is None,
        "selected_node_id": node.node_id if node else None,
        "state": state(),
    }


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
            "state": state(),
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
            "state": state(),
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
    CTX["phase"] = "iteration_done"
    CTX["checkpoint_seq"] = seq
    CTX["node_id"] = node_id
    CTX["error"] = None
    q = quiesce_for_criu()
    return {
        "ok": True,
        "finished": tree.is_finished(),
        "tree": tree.model_dump(),
        "node_id": node_id,
        "event": event,
        "mock_stats": stats,
        "state": state(),
        "quiesce": q,
    }


def handle_command(cmd: dict) -> dict:
    op = cmd.get("op")
    if op == "init":
        return init_instance(cmd["instance"], cmd["mock_url_base"])
    if op == "state":
        return {"ok": True, "state": state()}
    if op == "quiesce":
        return {"ok": True, "state": state(), "quiesce": quiesce_for_criu()}
    if op == "select":
        return select_node(cmd["tree"])
    if op == "step":
        return run_one_iteration(
            cmd["tree"],
            int(cmd["seq"]),
            cmd.get("selected_node_id"),
        )
    if op == "stop":
        return {"ok": True, "stopping": True, "state": state()}
    raise RuntimeError(f"unknown op {op!r}")


def atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    MAILBOX.mkdir(parents=True, exist_ok=True)
    cmd_path = MAILBOX / "cmd.json"
    resp_path = MAILBOX / "resp.json"
    ready_path = MAILBOX / "worker_ready.json"
    atomic_write_json(ready_path, {"ok": True, "pid": os.getpid(), "state": state()})
    log.info("worker pid=%d ready mailbox=%s", os.getpid(), MAILBOX)

    while True:
        while not cmd_path.exists():
            time.sleep(0.02)
        try:
            cmd = json.loads(cmd_path.read_text(encoding="utf-8"))
            cmd_path.unlink(missing_ok=True)
            log.info("worker command op=%s", cmd.get("op"))
            result = handle_command(cmd)
            atomic_write_json(resp_path, result)
            if cmd.get("op") == "stop":
                return 0
        except BaseException as e:
            CTX["phase"] = "error"
            CTX["error"] = f"{type(e).__name__}: {e}"
            log.exception("worker command failed")
            atomic_write_json(
                resp_path,
                {
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                    "traceback": traceback.format_exc(),
                    "state": state(),
                },
            )


if __name__ == "__main__":
    sys.exit(main())
