"""root_mcts.py — decoupled MCTS over the whole-root (runc-model) sandbox.

Same MCTS strategy as decoupled_mcts, but the sandbox is a RootOverlayDriver:
the agent_worker pivot_root's into a full-root overlay, so the agent's actions
(edits, bash, apt installs anywhere under /) are captured on the overlay and
roll back with each node's checkpoint, coupled with CRIU process C/R.

Host side (this loop + NPD + WorkerClient) is stdlib-only and runs on the
system python; the worker is stdlib-only too. No moatless venv is needed for
this path. Must run inside a private mount namespace (see run_root_mcts.py).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from upper.host.decoupled_mcts import (
    Node, _select, _backprop, _evaluate, _git_diff, _p50, NPD_PATH,
)
from upper.host.root_overlay_driver import RootOverlayDriver
from upper.host.worker_client import WorkerClient

LOG = logging.getLogger("host.root_mcts")


def _launch_npd(config: dict, channel: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["API_BASE"] = config["api_base"]
    env["API_KEY"] = config.get("api_key") or os.environ.get("DEEPSEEK_API_KEY", "")
    env["MODEL_NAME"] = config["model"]
    env["NPD_REQ_FIFO"] = f"{channel}/npd_req.fifo"
    env["NPD_NOTIFY_FIFO"] = f"{channel}/npd_notify.fifo"
    env["NPD_REQ_DIR"] = f"{channel}/npd_requests"
    env["NPD_RESP_DIR"] = f"{channel}/npd_responses"
    env["NPD_EPOCH_FILE"] = f"{channel}/npd_epoch"
    return subprocess.Popen([sys.executable, "-u", str(NPD_PATH)], env=env,
                            stdout=sys.stdout, stderr=sys.stderr)


async def run_root_mcts(config: dict) -> dict:
    logging.basicConfig(level=getattr(logging, config.get("log_level", "INFO")),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    repo_root = Path(__file__).resolve().parents[2]
    driver = RootOverlayDriver(
        base_image=config["base_image"],
        layers_root=config["layers_root"],
        repo_src=str(Path(config["base_lower"]).resolve()),
        worker_dir=str(repo_root / "worker"),
        dns=config.get("dns", "8.8.8.8"),
    )
    n_restores = 0
    ckpt_ms: list[float] = []
    restore_ms: list[float] = []
    branch_resume_ms: list[float] = []
    worker_reopen_ms: list[float] = []
    candidates: list[Node] = []
    nodes: list[Node] = []
    result: dict = {"ok": False}
    npd = None
    client = None
    try:
        await driver.start()
        merged = Path(driver.merged_dir)
        npd = _launch_npd(config, driver.channel)
        time.sleep(0.5)
        client = WorkerClient(pipe_in=f"{driver.channel}/agent.in",
                              pipe_out=f"{driver.channel}/agent.out")
        client.connect()
        ri = client.init(driver.worker_repo_path, config["task"])
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
                restore_ms.append(rec.wall_ms)
                reopen_started = time.perf_counter()
                client.reopen()
                worker_reopen_ms.append((time.perf_counter() - reopen_started) * 1000)
                branch_resume_ms.append((time.perf_counter() - resume_started) * 1000)
                current = target
                LOG.info("iter %d: restored to node %d (%.1fms)", it, target.id, rec.wall_ms)

            try:
                r = client.step(temperature=temp)
            except Exception as e:
                LOG.warning("iter %d: worker step failed: %s", it, e)
                continue
            if not r.get("ok"):
                LOG.warning("iter %d: step not ok: %s", it, r.get("error"))
                continue

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
                restore_ms.append(rec.wall_ms)
                current = best
            final_patch = _git_diff(merged)

        result = {
            "ok": True,
            "best_node_id": best.id if best else None,
            "final_patch_len": len(final_patch),
            "n_iterations": len([n for n in nodes if n.id != 0]),
            "n_nodes": len(nodes),
            "n_restores": n_restores,
            "n_finished": len(candidates),
            "ckpt_ms_p50": _p50(ckpt_ms),
            "restore_ms_p50": _p50(restore_ms),
            "branch_resume_ms_p50": _p50(branch_resume_ms),
            "worker_reopen_ms_p50": _p50(worker_reopen_ms),
            "model": config["model"],
            "stop_reason": "completed",
        }
        out = config.get("out")
        if out and final_patch:
            Path(out).write_text(final_patch, encoding="utf-8")
            result["patch_file"] = out
    except Exception as e:
        LOG.exception("root MCTS failed")
        result = {"ok": False, "error": f"{type(e).__name__}: {e}", "n_restores": n_restores}
    finally:
        try:
            if client:
                client.close()
        except Exception:
            pass
        try:
            await driver.shutdown()
        except Exception as e:
            LOG.warning("driver shutdown ignore: %s", e)
        try:
            if npd:
                npd.terminate()
        except Exception:
            pass
    return result
