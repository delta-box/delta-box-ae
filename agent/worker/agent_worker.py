#!/usr/bin/env python3
"""agent_worker.py — single-threaded, fork-safe, checkpointable agent worker.

In-sandbox process of the decoupled DeltaBox runtime (paper form):

    host (MCTS strategy)  ──FIFO/vsock──▶  agent_worker (THIS, in sandbox)
                                                 │ LLM via files+FIFO
                                                 ▼
                                            NPD (owns the HTTP socket)

INVARIANTS (why this file is shaped this way):

  1. SINGLE-THREADED AT EVERY QUIESCENT POINT. The warm-template fast path
     forks this process; fork() of a multi-threaded process is unsafe (the
     child keeps only the calling thread; locks held by vanished threads
     deadlock it). So the worker NEVER keeps a long-lived thread. The event
     loop is one thread; the only blocking wait is one select(). All
     thread-spawning machinery is kept OUT: LLM → NPD (separate process),
     and we clamp OMP/BLAS to 1 so transitively-imported numeric libs
     (faiss/numpy, pulled by moatless imports) never spin up a thread team.
     template_fork._assert_single_threaded refuses to fork if /proc/self/task
     shows >1 thread — a hard tripwire, not a hope.

  2. NO OUTWARD SOCKET IN THE ADDRESS SPACE. The worker imports no LLM SDK
     and opens no TCP/UDS. LLM round-trips go to the Network Proxy Daemon as
     a request file + one FIFO line; the reply is a file + one notify line.
     CRIU can dump the worker mid-LLM-wait safely and a fork carries no socket.

  3. RESEARCH-VALUABLE STATE LIVES IN THIS PROCESS. The worker holds the
     agent's evolving reasoning context (the running conversation: every
     viewed snippet, edit result and test output it accumulated) plus a warm,
     fully-imported interpreter. That is exactly what a warm-fork restore
     reconstitutes in milliseconds and a cold replay would pay seconds to
     rebuild. This is a stateful reasoning agent, not a stateless command shell.

ACTION SPACE: the moatless action wrappers (ViewCode/Grep/ListFiles/Glob/
StringReplace/RegexReplace/ReplaceLines/ApplyPatch/CreateFile/AppendString/
ViewDiff) — parser-free, index-free, pure file I/O + git/grep subprocess, so
they are single-threaded and fork-safe. Plus a worker-local Bash. The worker
runs the full ReAct step: build prompt from in-memory conversation → NPD → parse
JSON action → execute moatless action → append observation → return to host.

Control protocol (host → worker on /tmp/agent.in, replies on /tmp/agent.out),
one JSON object per line, each reply echoes the request's "ctrl_id":

    {"ctrl":"init", "repo_path":"/testbed", "task":"<problem statement>"}
        → set up workspace/actions, seed conversation
    {"ctrl":"step", "temperature":0.0, "observation":"<optional extra obs>"}
        → one ReAct step; returns {ok, step, action, observation, finished}
    {"ctrl":"state"}            → {ok, state}
    {"ctrl":"clear_soft_dirty"} → rebaseline soft-dirty after a restore
"""
from __future__ import annotations

import os

# Clamp numeric-lib thread teams BEFORE any heavy import. faiss/numpy are pulled
# transitively by moatless; with these = 1 they never spawn an OMP/BLAS thread,
# keeping the worker single-threaded (fork-safe). litellm is also imported
# transitively but we never call it (NPD does the HTTP), so its lazy executor
# never materialises.
for _k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "OMP_THREAD_LIMIT", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_k, "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# litellm (pulled transitively by moatless) starts a background cost-map fetch
# thread when LITELLM_LOCAL_MODEL_COST_MAP is set, and that thread can block on
# the network — which would (a) make the worker multi-threaded (fork-unsafe) and
# (b) hang a wait=True executor shutdown. The worker never calls litellm (NPD
# does the HTTP), so strip the trigger before anything imports litellm.
os.environ.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
os.environ.setdefault("LITELLM_TELEMETRY", "False")
os.environ.setdefault("DISABLE_LITELLM_LOGGING", "True")

import concurrent.futures
import concurrent.futures.thread
import gc
import json
import select
import subprocess
import sys
import threading
import time
import uuid
import weakref
from pathlib import Path

# Track every ThreadPoolExecutor created anywhere (litellm/moatless make these
# lazily). We shut them ALL down at each quiescent point so the worker is
# single-threaded before any warm-template fork. Installed BEFORE the heavy
# imports so nothing escapes the net.
_TRACKED_EXECUTORS: "weakref.WeakSet" = weakref.WeakSet()
_OrigTPE = concurrent.futures.ThreadPoolExecutor


class _TrackingTPE(_OrigTPE):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        _TRACKED_EXECUTORS.add(self)


concurrent.futures.ThreadPoolExecutor = _TrackingTPE
concurrent.futures.thread.ThreadPoolExecutor = _TrackingTPE

# Resolve template_fork (warm-template endpoint) regardless of whether the
# launcher env preserved PYTHONPATH (sudo -E may strip it). gsd lives at
# <repo>/backends/deltabox/gsd; this file at <repo>/worker/agent_worker.py.
# Resolve sibling modules (worker_actions) + template_fork even if sudo -E
# stripped PYTHONPATH. The worker deliberately imports NO moatless (importing it
# pulls faiss, whose native OpenMP thread makes the process unforkable); the
# action space lives in worker_actions, which imports only the stdlib.
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT / "agent" / "worker"),
           str(_REPO_ROOT / "backends" / "deltabox" / "gsd"),
           str(_REPO_ROOT)):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

import worker_actions  # noqa: E402  (stdlib-only action space)

# NPD channel — all four endpoints are files/FIFOs, never a socket.
NPD_REQ_FIFO    = os.environ.get("NPD_REQ_FIFO",    "/tmp/npd_req.fifo")
NPD_NOTIFY_FIFO = os.environ.get("NPD_NOTIFY_FIFO", "/tmp/npd_notify.fifo")
NPD_REQ_DIR     = os.environ.get("NPD_REQ_DIR",     "/tmp/npd_requests")
NPD_RESP_DIR    = os.environ.get("NPD_RESP_DIR",    "/tmp/npd_responses")
NPD_EPOCH_FILE  = os.environ.get("NPD_EPOCH_FILE",  "/tmp/npd_current_epoch")

PIPE_IN  = os.environ.get("AGENT_PIPE_IN",  "/tmp/agent.in")   # host → worker
PIPE_OUT = os.environ.get("AGENT_PIPE_OUT", "/tmp/agent.out")  # worker → host

WARM_TEMPLATE = os.environ.get("AGENT_WARM_TEMPLATE", "0") == "1"
LOG_FILE = os.environ.get("AGENT_WORKER_LOG", "/tmp/agent_worker.log")
BASH_TIMEOUT = float(os.environ.get("AGENT_BASH_TIMEOUT", "60"))
MAX_OBS_CHARS = int(os.environ.get("AGENT_MAX_OBS_CHARS", "8000"))
MAX_CONTEXT_TURNS = int(os.environ.get("AGENT_MAX_CONTEXT_TURNS", "40"))
# NOTE: AGENT_PID_FILE is written by namespace_launcher (the host PID of the
# fixed-active worker). The worker must NOT write it — as ns-PID 100 it would
# publish the wrong (ns-local) pid and race the launcher.


def _log(msg: str) -> None:
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] pid={os.getpid()} {msg}\n")
    except OSError:
        pass


def _disable_litellm_backgrounding() -> None:
    """litellm (pulled transitively by moatless) spawns telemetry / callback
    background threads. The worker never calls litellm (NPD does the HTTP), so
    disable all of it to keep the process single-threaded and forkable."""
    try:
        import litellm
        litellm.telemetry = False
        for name in ("callbacks", "success_callback", "failure_callback",
                     "_async_success_callback", "_async_failure_callback",
                     "input_callback", "service_callback"):
            try:
                setattr(litellm, name, [])
            except Exception:
                pass
    except Exception:
        pass


def _quiesce(reason: str) -> int:
    """Bring the worker back to single-threaded before it becomes forkable:
    shut down every tracked ThreadPoolExecutor, disable litellm backgrounding,
    gc. Returns the live thread count and logs the remaining thread names so a
    >1 result is diagnosable rather than a silent fork refusal."""
    _disable_litellm_backgrounding()
    shut = 0
    for ex in list(_TRACKED_EXECUTORS):
        try:
            # wait=True actually JOINS the worker threads (so the process is
            # truly single-threaded afterwards). Safe now that the only
            # network-blocking thread (litellm cost-map) is prevented at the
            # source via the env strip above, so no join can hang.
            ex.shutdown(wait=True, cancel_futures=True)
            shut += 1
        except Exception:
            pass
    try:
        gc.collect()
    except Exception:
        pass
    names = [t.name for t in threading.enumerate()]
    n = threading.active_count()
    if n != 1:
        _log(f"quiesce[{reason}] WARN still {n} threads: {names} "
             f"(executors_shutdown={shut})")
    else:
        _log(f"quiesce[{reason}] ok single-threaded (executors_shutdown={shut})")
    return n


# ───────────────────────── in-memory agent state ─────────────────────────
# Held in THIS process so a checkpoint captures it and a warm-fork restore
# brings it back. `conversation` is the agent's accumulated reasoning context.
STATE: dict = {
    "step": 0,
    "repo_path": None,
    "task": None,
    "conversation": [],     # list[{"role","content"}], [0] = system
    "last_action": None,
    "last_observation": None,
    "finished": False,
    "phase": "booting",
    "ready": False,         # set True after init
}


def state_summary() -> dict:
    return {
        "pid": os.getpid(),
        "step": STATE["step"],
        "repo_path": STATE["repo_path"],
        "n_messages": len(STATE["conversation"]),
        "last_action": STATE["last_action"],
        "finished": STATE["finished"],
        "phase": STATE["phase"],
        "ts": time.time(),
    }


# Action space lives in worker_actions (stdlib-only, no moatless/faiss import).
SYSTEM_TEMPLATE = """You are a software engineer fixing a bug in a repository at {repo_path}.

Work in this loop: locate the bug (ViewCode / GrepTool / ListFiles), edit it
(StringReplace / ReplaceLines / CreateFile / ApplyPatch), verify (Bash, e.g.
running the failing test), then emit finish.

Reply with EXACTLY ONE JSON object and nothing else, of the form:
  {{"action": "<ActionName>", "thought": "<brief reasoning>", <action args...>}}

Available actions:
{catalog}

Rules:
- Use relative paths inside the repository.
- old_str in StringReplace must match the file EXACTLY (copy from ViewCode).
- Do not repeat the same failing action; if an edit's old_str is not found,
  ViewCode the region first, then retry.
- When the fix is applied and verified, emit {{"action": "finish"}}.
"""


def _truncate(s: str, limit: int = MAX_OBS_CHARS) -> str:
    if s is None:
        return ""
    if len(s) <= limit:
        return s
    half = limit // 2
    return s[:half] + f"\n...[truncated {len(s) - limit} chars]...\n" + s[-half:]


def _execute_action(adict: dict) -> tuple[str, bool]:
    """Map a decided JSON action to a worker_actions handler and execute it
    against the in-memory repo_path. Returns (observation_text, finished)."""
    name = adict.get("action") or adict.get("tool") or ""
    if not isinstance(name, str):
        return ("Invalid action: 'action' must be a string.", False)
    if name.strip().lower() in ("finish", "done", "submit"):
        return ("Task marked finished by the agent.", True)
    resolved = worker_actions.resolve(name)
    if resolved is None:
        return (f"Unknown action {name!r}. Available: "
                f"{list(worker_actions.ACTIONS.keys())} + finish", False)
    fn, _args = resolved
    try:
        return fn(Path(STATE["repo_path"]), adict)
    except Exception as e:
        return (f"Action {name} raised {type(e).__name__}: {e}", False)


# ───────────────────────── NPD plumbing ─────────────────────────
def _atomic_write_json(path: str, payload: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.rename(tmp, path)


def _read_current_epoch() -> int:
    try:
        with open(NPD_EPOCH_FILE) as f:
            return int(f.read().strip() or "0")
    except (OSError, ValueError):
        return 0


def _send_llm_request(req_fd: int, messages: list, temperature: float,
                      max_tokens: int = 4000) -> str:
    epoch = _read_current_epoch()
    rid = f"ep{epoch}.{uuid.uuid4().hex}"
    _atomic_write_json(os.path.join(NPD_REQ_DIR, f"{rid}.json"), {
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    })
    os.write(req_fd, (rid + "\n").encode())
    return rid


def _read_response_file(rid: str) -> dict | None:
    resp_path = os.path.join(NPD_RESP_DIR, f"{rid}.json")
    try:
        with open(resp_path) as f:
            resp = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _log(f"npd resp read failed rid={rid}: {e}")
        return None
    try:
        os.unlink(resp_path)
    except OSError:
        pass
    return resp


def _extract_first_json(text: str) -> dict | None:
    s = (text or "").strip()
    for fence in ("```json", "```"):
        if s.startswith(fence):
            s = s[len(fence):].lstrip()
    if s.endswith("```"):
        s = s[:-3].rstrip()
    dec = json.JSONDecoder()
    i = 0
    while i < len(s):
        b = s.find("{", i)
        if b == -1:
            return None
        try:
            obj, _end = dec.raw_decode(s[b:])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
        i = b + 1
    return None


def _clear_self_soft_dirty() -> bool:
    try:
        with open("/proc/self/clear_refs", "w") as f:
            f.write("4")
        return True
    except OSError as e:
        _log(f"clear_refs failed: {e}")
        return False


def _context_messages(extra_observation: str | None) -> list:
    """Build the LLM message list from the in-memory conversation, keeping a
    sliding window but always pinning the system prompt and the task."""
    conv = STATE["conversation"]
    if not conv:
        return []
    system = conv[0:1]
    body = conv[1:]
    if len(body) > 2 * MAX_CONTEXT_TURNS:
        # keep the task (body[0]) + the most recent turns
        body = body[0:1] + body[-(2 * MAX_CONTEXT_TURNS - 1):]
    msgs = system + body
    if extra_observation:
        msgs = msgs + [{"role": "user", "content": _truncate(extra_observation)}]
    return msgs


# ───────────────────────── main loop ─────────────────────────
def main() -> int:
    os.makedirs(NPD_REQ_DIR, exist_ok=True)
    os.makedirs(NPD_RESP_DIR, exist_ok=True)

    template_fd = None
    template_write_path = None
    if WARM_TEMPLATE:
        try:
            import template_fork
            template_fd, template_write_path = template_fork.install_template_endpoint()
            _log("template endpoint installed")
        except Exception as e:
            _log(f"template endpoint install failed: {e}")

    for p in (NPD_REQ_FIFO, NPD_NOTIFY_FIFO):
        if not os.path.exists(p):
            os.mkfifo(p, 0o600)
    req_fd    = os.open(NPD_REQ_FIFO,    os.O_RDWR | os.O_NONBLOCK)
    notify_fd = os.open(NPD_NOTIFY_FIFO, os.O_RDWR | os.O_NONBLOCK)

    for p in (PIPE_IN, PIPE_OUT):
        if not os.path.exists(p):
            os.mkfifo(p, 0o600)
    in_fd  = os.open(PIPE_IN,  os.O_RDWR | os.O_NONBLOCK)
    out_fd = os.open(PIPE_OUT, os.O_RDWR | os.O_NONBLOCK)

    STATE["phase"] = "ready"
    _log(f"worker ready (warm_template={'on' if template_fd is not None else 'off'})")

    in_buf = b""
    notify_buf = b""
    pending: dict[str, dict] = {}   # rid → {ctrl_id, start_ts}

    def reply(obj: dict) -> None:
        os.write(out_fd, (json.dumps(obj) + "\n").encode())

    while True:
        try:
            watch = [in_fd, notify_fd]
            template_pending = False
            if template_fd is not None:
                watch.append(template_fd)
                try:
                    import template_fork
                    template_pending = template_fork.has_pending_ctrl_message(template_fd)
                except Exception:
                    template_pending = False

            if b"\n" in in_buf:
                ready = [in_fd]
            else:
                ready, _, _ = select.select(watch, [], [], 1.0)
                if not ready and not template_pending:
                    continue

            # 1. Warm-template fork (time-sensitive).
            if template_fd is not None and (template_pending or template_fd in ready):
                import template_fork
                role = template_fork.handle_template_message(template_fd, template_write_path)
                if role is not None:
                    continue

            # 2. Host control command. Defer reading a new command while an LLM
            #    call is outstanding (keep "pending = pure data" for clean fork).
            if in_fd in ready and len(pending) >= 1:
                ready = [fd for fd in ready if fd != in_fd]

            if in_fd in ready:
                if b"\n" not in in_buf:
                    try:
                        chunk = os.read(in_fd, 65536)
                    except BlockingIOError:
                        chunk = b""
                    if not chunk:
                        time.sleep(0.005)
                        continue
                    in_buf += chunk
                if b"\n" not in in_buf:
                    continue
                line_b, in_buf = in_buf.split(b"\n", 1)
                try:
                    req = json.loads(line_b.decode(errors="replace"))
                except json.JSONDecodeError as e:
                    _log(f"bad control request: {e}")
                    continue

                ctrl = req.get("ctrl")
                ctrl_id = req.get("ctrl_id")

                if ctrl == "state":
                    reply({"ok": True, "ctrl_id": ctrl_id, "state": state_summary()})
                    continue
                if ctrl == "clear_soft_dirty":
                    ok = _clear_self_soft_dirty()
                    reply({"ok": True, "ctrl_id": ctrl_id, "cleared": ok})
                    continue
                if ctrl == "init":
                    repo_path = req.get("repo_path") or "/testbed"
                    task = req.get("task") or ""
                    _log(f"init recv repo={repo_path}")
                    STATE["repo_path"] = repo_path
                    STATE["task"] = task
                    STATE["step"] = 0
                    STATE["finished"] = False
                    STATE["last_action"] = None
                    STATE["last_observation"] = None
                    system = SYSTEM_TEMPLATE.format(
                        repo_path=repo_path, catalog=worker_actions.catalog())
                    STATE["conversation"] = [
                        {"role": "system", "content": system},
                        {"role": "user", "content": f"Task:\n{task}"},
                    ]
                    STATE["phase"] = "ready"
                    STATE["ready"] = True
                    # Belt-and-suspenders: keep the worker single-threaded before
                    # the host's first checkpoint (worker_actions imports nothing
                    # that spawns threads, so this is normally a no-op).
                    _quiesce("init")
                    reply({"ok": True, "ctrl_id": ctrl_id, "state": state_summary(),
                           "n_actions": len(worker_actions.ACTIONS)})
                    continue
                if ctrl == "step":
                    if not STATE["ready"]:
                        reply({"ok": False, "ctrl_id": ctrl_id,
                               "error": "not initialized (send ctrl=init first)"})
                        continue
                    temp = float(req.get("temperature", 0.0))
                    messages = _context_messages(req.get("observation"))
                    if not messages:
                        reply({"ok": False, "ctrl_id": ctrl_id,
                               "error": "no conversation"})
                        continue
                    # If the host injected an extra observation, fold it into
                    # the conversation so it persists across the step.
                    if req.get("observation"):
                        STATE["conversation"].append(
                            {"role": "user", "content": _truncate(req["observation"])})
                    rid = _send_llm_request(req_fd, messages, temp)
                    pending[rid] = {"ctrl_id": ctrl_id, "start_ts": time.time()}
                    STATE["phase"] = "awaiting_llm"
                    _log(f"step dispatched rid={rid} ctrl_id={ctrl_id} "
                         f"msgs={len(messages)}")
                    continue
                reply({"ok": False, "ctrl_id": ctrl_id,
                       "error": f"unknown ctrl {ctrl!r}"})
                continue

            # 3. NPD response → parse action → EXECUTE → update context.
            if notify_fd in ready:
                try:
                    chunk = os.read(notify_fd, 65536)
                except BlockingIOError:
                    chunk = b""
                if chunk:
                    notify_buf += chunk
                while b"\n" in notify_buf:
                    nline, notify_buf = notify_buf.split(b"\n", 1)
                    rid = nline.decode(errors="replace").strip()
                    if not rid or rid not in pending:
                        continue   # stale (rolled-away branch) or not ours
                    ctx = pending.pop(rid)
                    resp = _read_response_file(rid)
                    latency_ms = int((time.time() - ctx["start_ts"]) * 1000)
                    STATE["phase"] = "ready"
                    if resp is None or not resp.get("ok"):
                        err = (resp or {}).get("error", "resp unavailable")
                        reply({"ok": False, "ctrl_id": ctx["ctrl_id"],
                               "error": f"llm: {err}", "latency_ms": latency_ms})
                        continue
                    content = resp.get("content", "") or ""
                    action = _extract_first_json(content)
                    STATE["conversation"].append(
                        {"role": "assistant", "content": content})
                    if action is None:
                        obs_text = ("Could not parse a JSON action from your reply. "
                                    "Reply with exactly one JSON object.")
                        finished = False
                    else:
                        obs_text, finished = _execute_action(action)
                    obs_text = _truncate(obs_text)
                    STATE["conversation"].append(
                        {"role": "user", "content": f"Observation:\n{obs_text}"})
                    STATE["step"] += 1
                    STATE["last_action"] = action
                    STATE["last_observation"] = obs_text
                    STATE["finished"] = finished
                    # Reap any threads an action/LLM path may have spawned so the
                    # worker is single-threaded before the host checkpoints it.
                    _quiesce(f"step{STATE['step']}")
                    _log(f"step done rid={rid} latency_ms={latency_ms} "
                         f"step={STATE['step']} action={(action or {}).get('action')} "
                         f"finished={finished}")
                    reply({
                        "ok": True,
                        "ctrl_id": ctx["ctrl_id"],
                        "step": STATE["step"],
                        "action": action,
                        "observation": obs_text,
                        "finished": finished,
                        "latency_ms": latency_ms,
                        "raw": None if action is not None else content[:2000],
                    })
        except Exception as e:
            _log(f"main loop error: {type(e).__name__}: {e}")
            time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
