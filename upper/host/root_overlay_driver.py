"""RootOverlayDriver — SandboxDriver-shaped async wrapper around
RootOverlaySandbox, for the whole-root (runc-model) decoupled MCTS.

Exposes the same async surface the MCTS driver loop expects
(`start()`, `merged_dir`, `checkpoint(id)`, `restore(id)`, `shutdown()`), but
the worker's entire root is a hot-switchable overlay (ro base image lower +
tmpfs upper) that it pivot_root's into, so root-level changes (apt installs
anywhere) roll back with the checkpoint and couple with CRIU process C/R.

The host<->worker<->NPD channel lives on a stable external tmpfs bound at
/dbchan (see RootOverlaySandbox); the driver exposes that host path as
`channel` so the caller points NPD + WorkerClient at it.

Must run inside a private mount namespace (unshare -m / make-rprivate) so the
mounts / pivot_root / criu operations cannot affect the host namespace.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import dataclass

from backends.deltabox.gsd.root_sandbox import RootOverlaySandbox


@dataclass
class CkptRec:
    ckpt_id: str
    wall_ms: float
    fork_or_criu: str = "criu"


# env the pivot-root worker gets; channel endpoints all under /dbchan (the
# stable external bind), cold-CRIU mode (no warm template).
_WORKER_ENV = (
    "AGENT_PIPE_IN=/dbchan/agent.in AGENT_PIPE_OUT=/dbchan/agent.out "
    "NPD_REQ_FIFO=/dbchan/npd_req.fifo NPD_NOTIFY_FIFO=/dbchan/npd_notify.fifo "
    "NPD_REQ_DIR=/dbchan/npd_requests NPD_RESP_DIR=/dbchan/npd_responses "
    "NPD_EPOCH_FILE=/dbchan/npd_epoch AGENT_WORKER_LOG=/dbchan/worker.log "
    "AGENT_WARM_TEMPLATE=0 AGENT_BASH_TIMEOUT=200"
)


class RootOverlayDriver:
    def __init__(self, base_image: str, layers_root: str, repo_src: str,
                 worker_dir: str, python_bin: str = "/usr/bin/python3",
                 dns: str = "8.8.8.8", repo_mount: str = "/work"):
        self.sb = RootOverlaySandbox(base_image=base_image, layers_root=layers_root,
                                     dns=dns, python_bin=python_bin)
        self.repo_src = repo_src        # host path to the task repo (a git checkout)
        self.worker_dir = worker_dir    # host dir holding agent_worker.py + worker_actions.py
        self.python_bin = python_bin
        self.repo_mount = repo_mount    # repo path INSIDE the overlay (worker's view)

    @property
    def channel(self) -> str:
        return self.sb.channel          # host path to the comms channel tmpfs

    @property
    def worker_repo_path(self) -> str:
        return self.repo_mount          # worker's view of the repo

    @property
    def merged_dir(self) -> str:
        # host path to the repo inside the overlay (for git diff / verify)
        return os.path.join(self.sb.merged, self.repo_mount.lstrip("/"))

    def _blocking_start(self) -> None:
        self.sb.mount()
        m = self.sb.merged
        # stage the task repo into the overlay upper at repo_mount (incl. .git)
        dst = os.path.join(m, self.repo_mount.lstrip("/"))
        shutil.copytree(self.repo_src, dst, symlinks=True, dirs_exist_ok=True)
        # stage the stdlib-only worker into the overlay at /dbagent
        agent = os.path.join(m, "dbagent")
        os.makedirs(agent, exist_ok=True)
        for fn in ("agent_worker.py", "worker_actions.py"):
            shutil.copy(os.path.join(self.worker_dir, fn), os.path.join(agent, fn))
        # channel scaffolding (host side of /dbchan)
        os.makedirs(os.path.join(self.channel, "npd_requests"), exist_ok=True)
        os.makedirs(os.path.join(self.channel, "npd_responses"), exist_ok=True)
        with open(os.path.join(self.channel, "npd_epoch"), "w") as f:
            f.write("0")
        wc = f"env {_WORKER_ENV} {self.python_bin} -u /dbagent/agent_worker.py"
        self.sb.launch(worker_cmd=wc, pid_file="dbchan/wpid", timeout=25)

    async def start(self) -> None:
        await asyncio.to_thread(self._blocking_start)

    async def checkpoint(self, ckpt_id: str) -> CkptRec:
        t0 = time.time()
        await asyncio.to_thread(self.sb.checkpoint, ckpt_id)
        return CkptRec(ckpt_id, (time.time() - t0) * 1000.0)

    async def restore(self, ckpt_id: str) -> CkptRec:
        t0 = time.time()
        await asyncio.to_thread(self.sb.restore, ckpt_id)
        return CkptRec(ckpt_id, (time.time() - t0) * 1000.0, "criu")

    async def shutdown(self) -> None:
        await asyncio.to_thread(self.sb.teardown)
