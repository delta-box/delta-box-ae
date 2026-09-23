#!/usr/bin/env python3
"""Guest-side Moatless runner for the Firecracker+dm pilot.

This is intentionally a real Moatless replay path:

* starts `mock_llm_server` inside the VM, so mock cursor state is covered by
  Firecracker snapshots;
* builds a real `SearchTree` from the recorded trajectory with the recorded tree
  stripped, like `spr_payload/replay_driver.py`;
* executes real Moatless actions (Find*, ViewCode, edits, RunTests as recorded
  NoEnvironment/null behavior);
* pauses at safe points, where the host can pause the VM and take FC+dm
  checkpoints.

The host controls progress through an HTTP server exposed by this guest:

  GET  /state      -> current phase and checkpoint metadata
  POST /continue   -> release the paused search thread

Snapshots are taken only while the guest reports `paused=true`; at that point no
host HTTP request is in flight, so restoring a VM snapshot resumes the guest in a
clean wait state.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PAYLOAD = Path("/mnt/disk2/dyp/spr_payload")
VENV_PY = Path("/mnt/disk2/dyp/moatless_det_venv/bin/python")
INSTANCE_ID = os.environ.get("INSTANCE_ID", "pytest-dev__pytest-8365")
CONTROL_HOST = os.environ.get("CONTROL_HOST", "0.0.0.0")
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "18080"))
CONTROL_TRANSPORT = os.environ.get("CONTROL_TRANSPORT", "tcp")
MOCK_PORT = int(os.environ.get("MOCK_PORT", "19999"))
MOCK_TRACES_ROOT = Path(os.environ.get("MOCK_TRACES_ROOT", "/tmp/det_mock_traces"))
REPO_BASE = PAYLOAD / "repos"
INDEX_STORE = PAYLOAD / "index_store"

if str(PAYLOAD) not in sys.path:
    sys.path.insert(0, str(PAYLOAD))

from replay_driver import (  # noqa: E402
    _http_json,
    rewrite_model_base_url,
    strip_recorded_tree,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s guest_fc_dm: %(message)s",
)
log = logging.getLogger("guest_fc_dm")


class PauseController:
    def __init__(self):
        self._lock = threading.Lock()
        self._continue = threading.Event()
        self.state = {
            "ok": True,
            "phase": "starting",
            "paused": False,
            "instance": INSTANCE_ID,
            "checkpoint_seq": -1,
            "node_id": None,
            "expansions_completed": 0,
            "label": None,
            "event": None,
            "mock_stats": None,
            "error": None,
            "ts": time.time(),
        }

    def snapshot(self) -> dict:
        with self._lock:
            snap = json.loads(json.dumps(self.state))
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

    def set_state(self, **kwargs) -> None:
        with self._lock:
            self.state.update(kwargs)
            self.state["ts"] = time.time()

    def pause(
        self,
        *,
        phase: str,
        checkpoint_seq: int,
        node_id: int,
        expansions_completed: int,
        label: str,
        event: dict | None = None,
    ) -> None:
        marker_dir = Path(os.environ.get("MARKER_DIR", "/tmp/fc_dm_markers"))
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker = marker_dir / f"fc_dm_marker_{checkpoint_seq}_{node_id}.txt"
        with open(marker, "w", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "checkpoint_seq": checkpoint_seq,
                        "node_id": node_id,
                        "expansions_completed": expansions_completed,
                        "label": label,
                        "ts": time.time(),
                    }
                )
                + "\n"
            )
            f.flush()
            os.fsync(f.fileno())
        dir_fd = os.open(marker_dir, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        self._continue.clear()
        self.set_state(
            phase=phase,
            paused=True,
            checkpoint_seq=checkpoint_seq,
            node_id=node_id,
            expansions_completed=expansions_completed,
            label=label,
            event=event,
        )
        log.info("paused: seq=%s node=%s label=%s", checkpoint_seq, node_id, label)
        self._continue.wait()
        self.set_state(paused=False, phase=f"running_after_{label}")
        log.info("continued: seq=%s node=%s label=%s", checkpoint_seq, node_id, label)

    def resume(self) -> None:
        self._continue.set()


CTRL = PauseController()


class ControlHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send_json(self, obj: dict, code: int = 200) -> None:
        raw = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/state":
            self._send_json(CTRL.snapshot())
        else:
            self._send_json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/continue":
            CTRL.resume()
            self._send_json({"ok": True})
        else:
            self._send_json({"ok": False, "error": "not found"}, 404)


class VsockHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_VSOCK


def start_control_server() -> ThreadingHTTPServer:
    if CONTROL_TRANSPORT == "vsock":
        srv = VsockHTTPServer((socket.VMADDR_CID_ANY, CONTROL_PORT), ControlHandler)
        log.info("control server listening on vsock:%d", CONTROL_PORT)
    else:
        srv = ThreadingHTTPServer((CONTROL_HOST, CONTROL_PORT), ControlHandler)
        log.info("control server listening on %s:%d", CONTROL_HOST, CONTROL_PORT)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def start_mock() -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PAYLOAD)
    env["PYTHONHASHSEED"] = "0"
    proc = subprocess.Popen(
        [
            str(VENV_PY),
            str(PAYLOAD / "mock_llm_server.py"),
            "--tcp-host",
            "127.0.0.1",
            "--tcp-port",
            str(MOCK_PORT),
            "--traces-root",
            str(MOCK_TRACES_ROOT),
        ],
        env=env,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    deadline = time.time() + 30.0
    while time.time() < deadline:
        try:
            r = _http_json(
                f"http://127.0.0.1:{MOCK_PORT}/admin/healthz",
                "GET",
                timeout=1.0,
            )
            if r.get("ok"):
                log.info("mock healthy on port %d", MOCK_PORT)
                return proc
        except Exception:
            time.sleep(0.2)
    raise TimeoutError("mock_llm_server did not become healthy")


def build_tree():
    mock_url_base = f"http://127.0.0.1:{MOCK_PORT}"
    load_resp = _http_json(
        f"{mock_url_base}/admin/load",
        "POST",
        {"instance_id": INSTANCE_ID, "variant": "ms"},
        timeout=30.0,
    )
    log.info("mock loaded: %s", load_resp)

    traj_path = MOCK_TRACES_ROOT / "qwen3-coder-30b-ms" / INSTANCE_ID / "trajectory.json"
    with open(traj_path) as f:
        data = json.load(f)
    data = strip_recorded_tree(data)
    rewrite_model_base_url(data, f"{mock_url_base}/v1")

    from moatless.index import CodeIndex
    from moatless.repository.file import FileRepository
    from moatless.search_tree import SearchTree

    repo_path = REPO_BASE / f"swe-bench_{INSTANCE_ID}"
    repo = FileRepository(repo_path=str(repo_path))
    code_index = CodeIndex.from_index_name(
        INSTANCE_ID,
        file_repo=repo,
        index_store_dir=str(INDEX_STORE),
    )
    from baseline_runtime import build_runtime
    runtime = build_runtime(repo, data, code_index=code_index)
    if runtime is not None:
        raise RuntimeError("Use guest_controller_driver for bound test runtime")
    tree = SearchTree.from_dict(data, repository=repo, code_index=code_index, runtime=runtime)
    return tree


def run_search_thread() -> None:
    mock_proc = None
    try:
        mock_proc = start_mock()
        tree = build_tree()

        CTRL.pause(
            phase="root_ready",
            checkpoint_seq=0,
            node_id=0,
            expansions_completed=0,
            label="root",
            event=None,
        )

        completed = 0

        def on_event(event: dict) -> None:
            nonlocal completed
            if event.get("event_type") != "tree_iteration":
                return
            completed += 1
            nodes = tree.root.get_all_nodes()
            node_id = max(n.node_id for n in nodes)
            try:
                stats = _http_json(
                    f"http://127.0.0.1:{MOCK_PORT}/admin/stats",
                    "GET",
                    timeout=5.0,
                )
            except Exception as e:
                stats = {"ok": False, "error": repr(e)}
            CTRL.set_state(mock_stats=stats)
            CTRL.pause(
                phase="iteration_done",
                checkpoint_seq=completed,
                node_id=node_id,
                expansions_completed=completed,
                label=f"node_{node_id}",
                event=event,
            )

        tree.add_event_handler(on_event)

        t0 = time.perf_counter()
        final_node = tree.run_search()
        wall_s = time.perf_counter() - t0
        try:
            stats = _http_json(f"http://127.0.0.1:{MOCK_PORT}/admin/stats", "GET", timeout=5.0)
        except Exception as e:
            stats = {"ok": False, "error": repr(e)}
        CTRL.set_state(
            phase="finished",
            paused=False,
            final_node_id=getattr(final_node, "node_id", None),
            run_search_wall_s=wall_s,
            mock_stats=stats,
        )
        log.info(
            "run_search finished final_node=%s wall=%.3fs stats=%s",
            getattr(final_node, "node_id", None),
            wall_s,
            stats,
        )
    except Exception as e:
        log.exception("guest driver failed")
        CTRL.set_state(ok=False, phase="error", paused=False, error=f"{type(e).__name__}: {e}")
    finally:
        if mock_proc and mock_proc.poll() is None:
            mock_proc.terminate()


def main() -> int:
    os.environ.setdefault("OPENAI_API_KEY", "dummy")
    start_control_server()
    t = threading.Thread(target=run_search_thread, daemon=True)
    t.start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main())
