"""decoupled_mcts.py — host-side MCTS that drives the in-sandbox agent_worker.

Paper-form topology:

    HOST: MCTS strategy (this module) + SandboxController (C/R) + NPD
      │  select node ─▶ restore worker to node (warm-fork) ─▶ step worker
      │                                                          │
    SANDBOX: agent_worker (ReAct: LLM via NPD + action) ◀────────┘
             ▲ checkpoint(worker process + overlay) after each step

The host owns NO agent reasoning state: the worker holds the conversation /
accumulated context in its own memory, and a warm-fork restore reconstitutes
exactly that node's worker memory in milliseconds. The host only decides WHICH
node to expand (UCT), drives C/R, and records the (action, observation) tree.

`run_decoupled(config)` is the entry point used by run.py.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("host.mcts")

_REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_PATH = _REPO_ROOT / "agent" / "worker" / "agent_worker.py"
NPD_PATH = _REPO_ROOT / "agent" / "npd" / "npd.py"

UCT_C = 1.4


@dataclass
class Node:
    id: int
    parent: Optional["Node"]
    ckpt_id: str                       # checkpoint of THIS node's (fs+worker) state
    action: Optional[dict] = None
    observation: str = ""
    finished: bool = False
    depth: int = 0
    children: list = field(default_factory=list)
    visits: int = 0
    value_sum: float = 0.0
    reward: float = 0.0

    @property
    def q(self) -> float:
        return self.value_sum / self.visits if self.visits else 0.0


def _uct(child: Node, parent_visits: int) -> float:
    if child.visits == 0:
        return float("inf")
    return child.q + UCT_C * math.sqrt(math.log(parent_visits + 1) / child.visits)


def _select(root: Node, max_expansions: int) -> Optional[Node]:
    """Descend from root by UCT until a node that can be expanded (not finished,
    fewer than max_expansions children). Returns None if the whole tree is
    exhausted (every reachable leaf is finished)."""
    node = root
    while True:
        if not node.finished and len(node.children) < max_expansions:
            return node
        if not node.children:
            return None
        node = max(node.children, key=lambda c: _uct(c, node.visits))


def _backprop(node: Node, value: float) -> None:
    cur: Optional[Node] = node
    while cur is not None:
        cur.visits += 1
        cur.value_sum += value
        cur = cur.parent


def _git_diff(root: Path) -> str:
    env = dict(os.environ)
    env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": str(root)})
    try:
        p = subprocess.run(["git", "--no-pager", "diff", "--no-color", "HEAD"],
                           cwd=str(root), env=env, capture_output=True,
                           text=True, timeout=20)
        return p.stdout or ""
    except Exception:
        return ""


def _launch_npd(config: dict) -> subprocess.Popen:
    env = dict(os.environ)
    env["API_BASE"] = config["api_base"]
    env["API_KEY"] = config.get("api_key") or os.environ.get("DEEPSEEK_API_KEY", "")
    env["MODEL_NAME"] = config["model"]
    # NPD endpoints default to /tmp/npd_* (matches agent_worker defaults).
    return subprocess.Popen([sys.executable, "-u", str(NPD_PATH)], env=env,
                            stdout=sys.stdout, stderr=sys.stderr)


def _evaluate(child: Node, merged: Path, verify_cmd: str | None) -> float:
    """Lightweight value. Terminal (finish) nodes are verified (test/diff);
    non-terminal nodes get a progress heuristic. A stronger LLM-judge can be
    plugged here without touching the C/R or worker code."""
    act = ((child.action or {}).get("action") or "").lower()
    if child.finished:
        if verify_cmd:
            # The verify runs against the live overlay right after the node's
            # checkpoint layer-switch; the new layer stack can take a moment to
            # become fully visible (base-lower dirs like tests/ briefly absent),
            # so settle + retry a transient "not found".
            out = ""
            rc = 1
            for attempt in range(3):
                try:
                    # Force the repo dir inside the command: this VM's bash
                    # startup cd's to /testbed on every invocation, so a relative
                    # test path would resolve there. Lead with an explicit cd.
                    full = f"cd {shlex.quote(str(merged))} && {verify_cmd}"
                    cp = subprocess.run(["bash", "-c", full], cwd=str(merged),
                                       capture_output=True, text=True, timeout=120)
                    rc = cp.returncode
                    out = (cp.stdout or "") + (cp.stderr or "")
                except Exception as e:
                    rc, out = 1, repr(e)
                if rc == 0 or ("not found" not in out and "no tests ran" not in out):
                    break
                time.sleep(0.4)
            child.reward = 1.0 if rc == 0 else 0.2
            if rc != 0:
                LOG.info("verify FAIL rc=%s on node %d:\n%s", rc, child.id, out[-1200:])
            return child.reward
        child.reward = 0.7 if _git_diff(merged).strip() else 0.3
        return child.reward
    edit_actions = ("stringreplace", "replacelines", "regexreplace",
                    "applypatch", "createfile", "appendstring")
    obs_low = (child.observation or "").lower()
    if act in edit_actions and "not found" not in obs_low and "error" not in obs_low:
        return 0.6
    if "not found" in obs_low or "raised" in obs_low or "invalid" in obs_low:
        return 0.15
    return 0.4


async def run_decoupled(config: dict) -> dict:
    """Run the decoupled MCTS. config keys: task, base_lower, workdir, model,
    api_base, api_key, max_iter, max_expansions, temperature, warm_template,
    clean, verify_cmd (optional), out (optional)."""
    logging.basicConfig(level=getattr(logging, config.get("log_level", "INFO")),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from agent.sandbox_driver import SandboxDriver
    from agent.host.worker_client import WorkerClient

    workdir = Path(config["workdir"]).resolve()
    base_lower = Path(config["base_lower"]).resolve()
    if config.get("clean", True) and workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)

    # The worker reads AGENT_WARM_TEMPLATE from env; the SandboxDriver launches
    # it with the current environment.
    os.environ["AGENT_WARM_TEMPLATE"] = "1" if config.get("warm_template", True) else "0"

    npd = _launch_npd(config)
    time.sleep(0.5)

    driver = SandboxDriver(
        workdir=workdir,
        base_lower=base_lower,
        shell_server_path=WORKER_PATH,
        enable_warm_template=config.get("warm_template", True),
        checkpoint_profile=config.get("checkpoint_profile", "runtime-default"),
        sudo_wrap=True,
    )
    client = WorkerClient()
    n_restores = 0
    warm_forks = 0
    step_failures = 0
    ckpt_ms: list[float] = []
    restore_ms: list[float] = []
    branch_resume_ms: list[float] = []
    worker_reopen_ms: list[float] = []
    candidates: list[Node] = []
    nodes: list[Node] = []
    result: dict = {"ok": False}

    try:
        await driver.start()
        merged = Path(driver.merged_dir)
        client.connect()
        ri = client.init(str(merged), config["task"])
        if not ri.get("ok"):
            raise RuntimeError(f"worker init failed: {ri}")

        root_ck = await driver.checkpoint("root")
        ckpt_ms.append(root_ck.wall_ms)
        root = Node(id=0, parent=None, ckpt_id=root_ck.ckpt_id, depth=0)
        nodes.append(root)
        current = root

        max_iter = int(config.get("max_iter", 8))
        max_exp = int(config.get("max_expansions", 1))
        temp = float(config.get("temperature", 0.7))
        verify_cmd = config.get("verify_cmd")

        for it in range(max_iter):
            target = _select(root, max_exp)
            if target is None:
                LOG.info("search exhausted at iter %d", it)
                break
            if target is not current:
                resume_started = time.perf_counter()
                rec = await driver.restore(target.ckpt_id)
                n_restores += 1
                warm_forks += int(rec.fork_or_criu == "fork")
                restore_ms.append(rec.wall_ms)
                reopen_started = time.perf_counter()
                client.reopen()
                worker_reopen_ms.append((time.perf_counter() - reopen_started) * 1000)
                branch_resume_ms.append((time.perf_counter() - resume_started) * 1000)
                current = target
                LOG.info("iter %d: restored to node %d via %s (%.1fms)",
                         it, target.id, rec.fork_or_criu, rec.wall_ms)

            try:
                r = client.step(temperature=temp)
            except Exception as e:
                step_failures += 1
                raise RuntimeError(f"worker step {it} failed; aborting to avoid a late action") from e
            if not r.get("ok"):
                step_failures += 1
                raise RuntimeError(f"worker step {it} failed: {r.get('error')}")

            action = r.get("action")
            obs = r.get("observation", "") or ""
            finished = bool(r.get("finished"))

            ck = await driver.checkpoint(f"n{it}")
            ckpt_ms.append(ck.wall_ms)
            child = Node(id=len(nodes), parent=target, ckpt_id=ck.ckpt_id,
                         action=action, observation=obs, finished=finished,
                         depth=target.depth + 1)
            target.children.append(child)
            nodes.append(child)
            current = child

            value = _evaluate(child, merged, verify_cmd)
            _backprop(child, value)
            if finished:
                candidates.append(child)
            LOG.info("iter %d: node %d action=%s finished=%s value=%.2f",
                     it, child.id, (action or {}).get("action"), finished, value)

        # Pick the best candidate (finished + highest reward), else the deepest
        # node that produced a non-empty diff, then materialise its patch.
        best = None
        if candidates:
            best = max(candidates, key=lambda n: (n.reward, n.depth))
        if best is None:
            diffed = [n for n in nodes if n.action and
                      ((n.action.get("action") or "").lower() in
                       ("stringreplace", "replacelines", "regexreplace",
                        "applypatch", "createfile", "appendstring"))]
            best = max(diffed, key=lambda n: n.depth) if diffed else None

        final_patch = ""
        if best is not None:
            if best is not current:
                rec = await driver.restore(best.ckpt_id)
                n_restores += 1
                warm_forks += int(rec.fork_or_criu == "fork")
                restore_ms.append(rec.wall_ms)
                current = best
            final_patch = _git_diff(merged)

        result = {
            "ok": len(nodes) > 1 and step_failures == 0,
            "solved": bool(best and best.finished and verify_cmd and best.reward == 1.0),
            "step_failures": step_failures,
            "checkpoint_profile": config.get("checkpoint_profile", "runtime-default"),
            "best_node_id": best.id if best else None,
            "final_patch_len": len(final_patch),
            "n_iterations": len([n for n in nodes if n.id != 0]),
            "n_nodes": len(nodes),
            "n_restores": n_restores,
            "n_finished": len(candidates),
            "warm_forks": warm_forks,
            "ckpt_ms_p50": _p50(ckpt_ms),
            "restore_ms_p50": _p50(restore_ms),
            "branch_resume_ms_p50": _p50(branch_resume_ms),
            "worker_reopen_ms_p50": _p50(worker_reopen_ms),
            "model": config["model"],
            "stop_reason": "step-failures" if step_failures else "completed",
        }
        out = config.get("out")
        if out and final_patch:
            Path(out).write_text(final_patch, encoding="utf-8")
            result["patch_file"] = out
    except Exception as e:
        LOG.exception("decoupled MCTS failed")
        result = {"ok": False, "error": f"{type(e).__name__}: {e}",
                  "n_restores": n_restores}
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            await driver.shutdown()
        except Exception as e:
            LOG.error("driver shutdown failed: %s", e)
            result["ok"] = False
            result["shutdown_error"] = f"{type(e).__name__}: {e}"
        try:
            npd.terminate()
            try:
                npd.wait(timeout=5)
            except subprocess.TimeoutExpired:
                npd.kill()
                npd.wait(timeout=5)
        except OSError as e:
            result["ok"] = False
            result["npd_shutdown_error"] = type(e).__name__
    return result


def _p50(xs: list[float]):
    if not xs:
        return None
    s = sorted(xs)
    return round(s[len(s) // 2], 2)
