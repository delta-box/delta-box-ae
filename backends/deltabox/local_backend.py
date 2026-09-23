"""LocalDeltaBoxBackend — SandboxBackend over a host-local DeltaBox SandboxDriver.

This is the host-overlay realization of the DeltaBox C/R substrate (CRIU process
dump/restore + OverlayFS layer sink, driven by an in-process SandboxController).
It exposes the same `common.backend.SandboxBackend` seam the MCTS uses for any
backend (E2B, VM-transport DeltaBox), so the upper search/agent layer is identical
regardless of substrate; only the per-op latency differs (DeltaBox: ~ms; E2B: ~s).

The driver is async, so these coroutines just await it. The incremental chain is
threaded here (the upper layer never passes a parent): each checkpoint chains off
the last, and a restore re-roots the chain at the restored node.
"""
from __future__ import annotations

import asyncio
import json
import select
import time

from common.backend import SandboxBackend, Checkpoint


class LocalDeltaBoxBackend(SandboxBackend):
    def __init__(self, driver):
        self._driver = driver
        self._cur_id = None              # threads the incremental ck chain
        self._next_raw_command = ""
        self.last_restore_rec = None
        # Worker (shell_server) IO: one JSON line per request over FIFOs.
        self._f_in = None
        self._f_out = None
        self._io_lock = asyncio.Lock()

    # The upper layer may annotate the next checkpoint with the action that
    # produced it (recorded in the trajectory; also used by adaptive strategy
    # parsing when enabled). Optional; clears after one checkpoint.
    def set_next_checkpoint_command(self, raw_command: str) -> None:
        self._next_raw_command = raw_command or ""

    async def setup(self) -> None:
        await self._driver.start()

    async def teardown(self) -> None:
        self._close_io()
        await self._driver.shutdown()

    async def checkpoint(self, node_id: str) -> Checkpoint:
        rec = await self._driver.checkpoint(
            tag=node_id,
            parent_ckpt_id=self._cur_id,
            raw_command=self._next_raw_command,
        )
        self._next_raw_command = ""
        self._cur_id = rec.ckpt_id
        # handle carries the DeltaBox CkptRecord (id, wall_ms, fork_or_criu, ...);
        # the upper layer treats it as opaque except for logging/trajectory.
        return Checkpoint(node_id=node_id, handle=rec)

    async def restore(self, ckpt: Checkpoint) -> None:
        target_id = ckpt.handle.ckpt_id
        self.last_restore_rec = await self._driver.restore(target_id)
        self._cur_id = target_id

    async def exec_action(self, action: dict) -> dict:
        """Run ONE operation in the in-sandbox worker (shell_server) and return its
        result dict. `action` is the worker wire protocol:
            {"type": "bash"|"python"|"read_file"|"write_file", ...}
        This is the single point through which the host agent reaches the sandbox;
        the worker process + overlay FS are exactly what checkpoint() captures."""
        async with self._io_lock:
            return await asyncio.to_thread(self._roundtrip_sync, action)

    def _ensure_io_open(self) -> None:
        if self._f_in is not None:
            return
        fin, fout = self._driver.fifo_in_path, self._driver.fifo_out_path
        for _ in range(50):
            if fin.exists() and fout.exists():
                break
            time.sleep(0.1)
        if not (fin.exists() and fout.exists()):
            raise FileNotFoundError(f"sandbox worker FIFOs not found: {fin}")
        self._f_in = open(fin, "w", buffering=1)
        self._f_out = open(fout, "r")

    def _roundtrip_sync(self, req: dict) -> dict:
        self._ensure_io_open()
        self._f_in.write(json.dumps(req, ensure_ascii=False) + "\n")
        self._f_in.flush()
        timeout_s = float(req.get("timeout_s", 60.0)) + 30.0
        ready, _, _ = select.select([self._f_out], [], [], timeout_s)
        if not ready:
            self._close_io()
            raise TimeoutError(
                f"worker response timeout after {timeout_s:.1f}s for {req.get('type')}")
        line = self._f_out.readline()
        if not line:
            self._close_io()
            raise RuntimeError("worker closed FIFO mid-conversation")
        return json.loads(line)

    def _close_io(self) -> None:
        for f in (self._f_in, self._f_out):
            try:
                if f is not None:
                    f.close()
            except Exception:
                pass
        self._f_in = self._f_out = None

    # DeltaBox-specific accessors for the overlay layer-diff fallback in the
    # upper layer. Other backends simply lack a controller -> the fallback skips.
    @property
    def controller(self):
        return getattr(self._driver, "_ctrl", None)

    @property
    def current_ckpt_id(self):
        return self._cur_id
