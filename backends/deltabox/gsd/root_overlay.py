"""Whole-root rollback coverage for DeltaBox.

The sandbox's primary writable surface (the task repo) is covered by a single
hot-switchable overlay at ``/testbed``. Some workloads, however, modify the
*system* root: ``apt``/``yum`` installs land in ``/usr``, ``/etc`` and
``/var``; an agent may build or configure tools there and expect those changes
to roll back with the rest of the checkpoint. This module extends the proven
single-overlay mechanism to an arbitrary set of system directories.

Design (each covered directory ``D``):

  1. ``mount --bind D STASH``  - bind D's *original* content to a distinct path.
     The overlay's ``lowerdir`` points at STASH, never at D itself. This is
     mandatory: an overlay whose ``lowerdir`` is the very path it is mounted
     over recurses into itself and deadlocks the kernel in ``ovl_lookup``.
  2. ``mount -t overlay ... MERGED`` with ``lowerdir=STASH`` - the patched
     d-overlayfs, at a neutral mountpoint.
  3. ``mount --bind MERGED D`` - every process (whose root stays the real ``/``)
     now reads/writes D through the overlay.

Checkpoint/restore reuse the patched ``OVL_IOCTL_CHECKPOINT`` hot-switch: each
covered directory keeps its own lower-layer chain, and a checkpoint freezes the
active upper into that chain exactly like the ``/testbed`` overlay. CRIU is told
about each overlay mount via ``--ext-mount-map`` so process C/R composes with
filesystem rollback.

Note: executing binaries served from these overlays requires the patched-kernel
fix to ``ovl_mmap`` (private/COW mmaps must not be copied up); without it every
binary run from an overlaid ``/usr`` segfaults.

CRIU coupling - two topologies:

  (A) "overlay submount over a live system dir" (this module's mount_all): the
      process root stays the real /, and /usr //etc //var are overlay submounts.
      This composes with FILESYSTEM hot-switch rollback but NOT with CRIU dump
      of a process in the same mount namespace. Observed failures: files-reg.c
      device mismatch for libs (ld-linux/libc) mmap'd via the overlay, and
      mount-tree lookup failures from the extra mounts. Use this mode for
      filesystem-only rollback (apt install -> hot-switch restore).

  (B) runc model: pivot_root the worker into a FULL-root overlay so its root
      *is* the overlay (one device, clean mount tree). This DOES couple with
      CRIU. Verified end-to-end on the patched kernel: a process pivot_root'd
      into a full-root overlay (lower = bind of /, tmpfs upper, fresh /proc //dev)
      was CRIU dump'd and restored (criu --root) with in-memory state preserved
      ("Dumping/Restore finished successfully"). Prerequisites that made it work:
        - the ovl_mmap exec fix (binaries run from the overlay);
        - reopen fds 0/1/2 into the post-pivot mount tree (else CRIU can't
          resolve fd mounts);
        - keep the mount tree minimal (mask /dev as tmpfs, fresh /proc) so CRIU
          does not hit "unsupported id" on copied mounts.
      Wiring mode (B) into the runtime (namespace_launcher mount-ns + pivot_root;
      controller hot-switch + criu --root) is the path to couple whole-root
      coverage with DeltaBox's process C/R.

Volatile directories (``/proc`` ``/sys`` ``/dev`` ``/run`` ``/tmp``
``/var/log``) must NOT be covered - they are separate mounts, carry no rollback
value, and would only add copy-up noise. The caller chooses the target list;
``DEFAULT_TARGETS`` / ``DEFAULT_EXCLUDES`` document the sane defaults.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import subprocess
from typing import Dict, List, Optional


class OvlCheckpointArgs(ctypes.Structure):
    _fields_ = [("options", ctypes.c_uint64), ("options_len", ctypes.c_uint32)]


def _iow(type_char: str, nr: int, size: int) -> int:
    return (1 << 30) | (size << 16) | (ord(type_char) << 8) | nr


OVL_IOCTL_CHECKPOINT_CMD = _iow('O', 1, ctypes.sizeof(OvlCheckpointArgs))

# Sane defaults. apt/dpkg write surface is /usr (binaries+libs; /bin /sbin /lib
# are symlinks into /usr on merged-usr systems), /etc (config) and /var
# (/var/lib/dpkg DB, /var/cache/apt). /opt covers third-party installers.
DEFAULT_TARGETS = ["/usr", "/etc", "/var", "/opt"]
# Documented for callers; these stay as their own mounts and are never overlaid.
DEFAULT_EXCLUDES = ["/proc", "/sys", "/dev", "/run", "/tmp", "/var/log", "/var/run"]

_OVL_OPTS = "index=off,metacopy=off,redirect_dir=off"


class _Mounted(Exception):
    pass


def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


class RootOverlay:
    """A single covered directory backed by a stash-bind patched overlay."""

    def __init__(self, target: str, layers_root: str):
        self.target = os.path.normpath(target)
        self.tag = self.target.strip("/").replace("/", "_") or "root"
        self.root = os.path.join(layers_root, self.tag)
        self.stash = os.path.join(self.root, "stash")
        self.merged = os.path.join(self.root, "merged")
        self.fd: Optional[int] = None
        self._n = 0
        # lower_chain is the read-only chain below the active upper.
        self.lower_chain: List[str] = []
        self.upper: str = ""
        self.work: str = ""
        # ckpt_id -> the lower chain that restores this directory to that point.
        self.snapshots: Dict[str, List[str]] = {}

    def _fresh(self):
        self._n += 1
        u = os.path.join(self.root, f"u{self._n}")
        w = os.path.join(self.root, f"w{self._n}")
        os.makedirs(u, exist_ok=True)
        os.makedirs(w, exist_ok=True)
        return u, w

    def _switch(self, lowers: List[str], upper: str, work: str, kind: str):
        opts = (f"lowerdir={':'.join(lowers)},upperdir={upper},"
                f"workdir={work},ovl_kind={kind}").encode("utf-8")
        buf = ctypes.create_string_buffer(opts)
        args = OvlCheckpointArgs()
        args.options = ctypes.cast(buf, ctypes.c_void_p).value
        args.options_len = len(opts)
        fcntl.ioctl(self.fd, OVL_IOCTL_CHECKPOINT_CMD, args)

    def mount(self) -> None:
        os.makedirs(self.stash, exist_ok=True)
        os.makedirs(self.merged, exist_ok=True)
        u0, w0 = self._fresh()
        r = _run(["mount", "--bind", self.target, self.stash])
        if r.returncode != 0:
            raise RuntimeError(f"stash bind {self.target}: {r.stderr.strip()}")
        r = _run(["mount", "-t", "overlay", "overlay", "-o",
                  f"lowerdir={self.stash},upperdir={u0},workdir={w0},{_OVL_OPTS}",
                  self.merged])
        if r.returncode != 0:
            _run(["umount", "-l", self.stash])
            raise RuntimeError(f"overlay mount {self.target}: {r.stderr.strip()}")
        r = _run(["mount", "--bind", self.merged, self.target])
        if r.returncode != 0:
            _run(["umount", "-l", self.merged])
            _run(["umount", "-l", self.stash])
            raise RuntimeError(f"bind merged over {self.target}: {r.stderr.strip()}")
        self.fd = os.open(self.merged, os.O_RDONLY | os.O_DIRECTORY)
        self.lower_chain = [self.stash]
        self.upper, self.work = u0, w0

    def checkpoint(self, ckpt_id: str) -> None:
        """Freeze the active upper into the lower chain; record restore target."""
        nu, nw = self._fresh()
        self._switch(self.lower_chain, nu, nw, "ckpt")
        self.lower_chain = [self.upper] + self.lower_chain
        self.upper, self.work = nu, nw
        self.snapshots[ckpt_id] = list(self.lower_chain)

    def restore(self, ckpt_id: str) -> None:
        if ckpt_id not in self.snapshots:
            raise KeyError(f"{self.target}: no checkpoint {ckpt_id!r}")
        target = self.snapshots[ckpt_id]
        nu, nw = self._fresh()
        self._switch(target, nu, nw, "restore")
        self.lower_chain = list(target)
        self.upper, self.work = nu, nw

    def ext_mount_map_args(self) -> List[str]:
        # The merged overlay is visible at two paths (its own mountpoint and the
        # bind over the target); CRIU must be able to resolve both as external.
        out: List[str] = []
        for p in (self.merged, self.target):
            out += ["--ext-mount-map", f"{p}:{p}"]
        return out

    def teardown(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        for p in (self.target, self.merged, self.stash):
            _run(["umount", "-l", p])


class RootOverlaySet:
    """Manages whole-root rollback coverage across a set of directories."""

    def __init__(self, targets: Optional[List[str]] = None,
                 layers_root: str = "/dev/shm/deltabox_rootovl"):
        self.layers_root = layers_root
        chosen = targets if targets is not None else DEFAULT_TARGETS
        # Only cover directories that exist and are not excluded.
        self.overlays: List[RootOverlay] = []
        for t in chosen:
            t = os.path.normpath(t)
            if t in DEFAULT_EXCLUDES:
                continue
            if not os.path.isdir(t):
                continue
            self.overlays.append(RootOverlay(t, layers_root))

    @property
    def targets(self) -> List[str]:
        return [o.target for o in self.overlays]

    def mount_all(self) -> None:
        os.makedirs(self.layers_root, exist_ok=True)
        done: List[RootOverlay] = []
        try:
            for o in self.overlays:
                o.mount()
                done.append(o)
        except Exception:
            for o in reversed(done):
                o.teardown()
            raise

    def checkpoint(self, ckpt_id: str) -> None:
        for o in self.overlays:
            o.checkpoint(ckpt_id)

    def has_checkpoint(self, ckpt_id: str) -> bool:
        return bool(self.overlays) and all(ckpt_id in o.snapshots for o in self.overlays)

    def restore(self, ckpt_id: str) -> None:
        for o in self.overlays:
            o.restore(ckpt_id)

    def criu_ext_mount_map_args(self) -> List[str]:
        out: List[str] = []
        for o in self.overlays:
            out += o.ext_mount_map_args()
        return out

    def teardown(self) -> None:
        for o in reversed(self.overlays):
            o.teardown()
