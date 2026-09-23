"""Whole-root sandbox with coupled filesystem + process checkpoint/restore.

Runc-model alternative to root_overlay.RootOverlaySet. Instead of overlaying
individual system directories as submounts (filesystem rollback only, does NOT
compose with CRIU process dump), the worker is ``pivot_root``'d so its entire
root *is* a single hot-switchable overlay. That clean, single-device mount tree
is what lets CRIU dump and restore the process, so whole-root coverage (apt/yum
installs anywhere under /) couples with DeltaBox's process C/R.

Verified end-to-end on the patched 6.8 kernel (9/9, real apt): a worker
pivot_root'd into a full-root overlay runs ``apt-get install jq``, uses it, is
CRIU-checkpointed together with a hot-switch of the root overlay, runs
``apt-get install tree``, then is restored - jq survives and still executes
(cross-checkpoint exec), tree (the post-checkpoint package, files + dpkg entry)
is rolled back, and the worker's in-memory state resumes.

Hard-won requirements baked in:
  * the ovl_mmap exec fix (binaries must run from the overlay);
  * lower = a READ-ONLY base image, never a rw bind of the live / (writes can
    then only reach the tmpfs upper; the base can never be corrupted);
  * the worker pivot_root's in its own mount(+pid) namespace; the overlay is
    mounted in the controller namespace too (shared superblock) so the
    controller can hot-switch it by ioctl;
  * reopen fds 0/1/2 into the post-pivot mount tree, and a minimal mount tree
    (fresh /proc, tmpfs /dev, tmpfs /run, tmpfs /tmp mode=1777) so CRIU does not
    hit "unsupported id" and so apt's drop-priv user can write /tmp;
  * CRIU dump with ``--ext-mount-map /:/`` and restore with ``criu --root``.

Kernel dependency: the checkpoint hot-switch must PRESERVE submounts (the
/dbchan channel + /proc //dev //run //tmp). Stock/older DeltaFS invalidated
overlay dentries on layer switch, which detached those submounts and made the
worker lose its channel after the first checkpoint. Requires the d-overlayfs
commit "overlayfs: preserve submounts across checkpoint layer switch"
(ovl_dentry_revalidate_common returns valid for mount-point dentries).

Full real-agent-worker integration verified 6/6 on the fixed kernel: the real
worker/agent_worker runs in the pivot-root overlay, does a real apt-install
ReAct step, is CHECKPOINTed (CRIU dump + hot-switch), does a SECOND real
apt-install after the checkpoint, then is CRIU-restored with FS rollback — jq
survives and still runs, tree (post-checkpoint) is rolled back.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import subprocess
import time
from typing import Dict, List, Optional


class OvlCheckpointArgs(ctypes.Structure):
    _fields_ = [("options", ctypes.c_uint64), ("options_len", ctypes.c_uint32)]


def _iow(t: str, nr: int, size: int) -> int:
    return (1 << 30) | (size << 16) | (ord(t) << 8) | nr


OVL_IOCTL_CHECKPOINT_CMD = _iow('O', 1, ctypes.sizeof(OvlCheckpointArgs))
_OVL_OPTS = "index=off,metacopy=off,redirect_dir=off"


def _run(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


# In-overlay command server: a stdlib-only worker that mirrors an in-memory
# heartbeat counter to /state.txt and runs commands dropped at /cmd.txt. This is
# the long-lived process that gets CRIU-checkpointed.
_WORKER_SERVER = r'''
import os, time, subprocess
open("/pid.txt","w").write(str(os.getpid()))
n = 0
while True:
    n += 1
    open("/state.txt","w").write(str(n))
    if os.path.exists("/cmd.txt"):
        try:
            c = open("/cmd.txt").read(); os.remove("/cmd.txt")
            e = dict(os.environ, PATH="/usr/sbin:/usr/bin:/sbin:/bin", DEBIAN_FRONTEND="noninteractive")
            r = subprocess.run(c, shell=True, capture_output=True, text=True, env=e, timeout=600)
            open("/cmdout.txt","w").write(f"rc={r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-1000:]}")
            os.replace("/cmdout.txt","/cmddone.txt")
        except Exception as ex:
            open("/cmddone.txt","w").write(f"rc=255\nERR {ex}")
    time.sleep(0.2)
'''

# Pivot the launcher into the root overlay, then exec the worker. {merged} is the
# overlay (mounted in the parent ns, inherited via the mount-ns copy).
_PIVOT_SETUP = r"""
set -e
mount --make-rprivate /
cd {merged}
mkdir -p oldroot proc sys dev run tmp
pivot_root . oldroot
mount -t proc proc /proc
mount -t sysfs sys /sys 2>/dev/null || true
mount -t tmpfs tdev /dev
mknod /dev/null c 1 3; mknod /dev/zero c 1 5; mknod /dev/urandom c 1 9; mknod /dev/random c 1 8
mkdir -p /dev/pts /dev/shm
mount -t devpts devpts /dev/pts 2>/dev/null || true
mount -t tmpfs trun /run; mkdir -p /run/lock
mount -t tmpfs -o mode=1777 ttmp /tmp
rm -f /etc/resolv.conf; echo "nameserver {dns}" > /etc/resolv.conf
umount -l /oldroot 2>/dev/null || true
echo $$ > /{pid_file}
exec {worker} </dev/null >/dev/null 2>&1
"""


class RootOverlaySandbox:
    """Runc-model whole-root sandbox: worker root IS a hot-switchable overlay."""

    def __init__(self, base_image: str, layers_root: str,
                 dns: str = "8.8.8.8", criu_bin: str = "criu",
                 python_bin: str = "/usr/bin/python3"):
        self.base_image = base_image
        self.layers_root = layers_root
        self.dns = dns
        self.criu_bin = criu_bin
        self.python_bin = python_bin
        self.merged = os.path.join(layers_root, "merged")
        self.img_root = os.path.join(layers_root, "img")
        # Host<->worker<->NPD channel lives on a SEPARATE shared tmpfs bound into
        # the worker root at /dbchan (NOT the overlay). The hot-switch changes
        # overlay inode identity, which would make CRIU restore of FIFOs on the
        # overlay fail with ESTALE; keeping the channel on a stable external
        # mount fixes that and keeps comms alive across checkpoints.
        self.channel = os.path.join(layers_root, "chan")   # host path (tmpfs)
        self.channel_mount = "/dbchan"                       # worker path
        self._n = 0
        self.fd: Optional[int] = None
        self.lower_chain: List[str] = []
        self.upper = ""
        self.work = ""
        self.snapshots: Dict[str, List[str]] = {}
        self.worker_pid: Optional[int] = None

    # ---- overlay layer plumbing -------------------------------------------
    def _fresh(self):
        self._n += 1
        u = os.path.join(self.layers_root, f"u{self._n}")
        w = os.path.join(self.layers_root, f"w{self._n}")
        os.makedirs(u, exist_ok=True)
        os.makedirs(w, exist_ok=True)
        return u, w

    def _switch(self, lowers: List[str], upper: str, work: str, kind: str):
        opts = (f"lowerdir={':'.join(lowers)},upperdir={upper},"
                f"workdir={work},ovl_kind={kind}").encode()
        buf = ctypes.create_string_buffer(opts)
        a = OvlCheckpointArgs()
        a.options = ctypes.cast(buf, ctypes.c_void_p).value
        a.options_len = len(opts)
        fcntl.ioctl(self.fd, OVL_IOCTL_CHECKPOINT_CMD, a)

    def mount(self) -> None:
        os.makedirs(self.merged, exist_ok=True)
        os.makedirs(self.img_root, exist_ok=True)
        u0, w0 = self._fresh()
        r = _run(f"mount -t overlay overlay -o "
                 f"lowerdir={self.base_image},upperdir={u0},workdir={w0},{_OVL_OPTS} "
                 f"{self.merged}")
        if r.returncode != 0:
            raise RuntimeError(f"root overlay mount: {r.stderr.strip()}")
        self.fd = os.open(self.merged, os.O_RDONLY | os.O_DIRECTORY)
        self.lower_chain = [self.base_image]
        self.upper, self.work = u0, w0
        # Bind the channel tmpfs into the overlay at /dbchan (external to the
        # rollback surface, stable across hot-switch). The worker's nested mount
        # ns inherits this bind and pivot_root keeps it as /dbchan.
        os.makedirs(self.channel, exist_ok=True)
        os.makedirs(f"{self.merged}{self.channel_mount}", exist_ok=True)
        _run(f"mount --bind {self.channel} {self.merged}{self.channel_mount}")

    # ---- worker lifecycle + command channel -------------------------------
    def launch(self, timeout: float = 15.0, worker_cmd: Optional[str] = None,
               pid_file: str = "pid.txt") -> int:
        """Launch the in-overlay worker (pivot_root'd) and return its host pid.

        worker_cmd: shell command to exec as the worker (already resolvable
        inside the overlay). Defaults to the built-in command-server. pid_file
        is the path (relative to the overlay root) the worker writes its pid to
        for the launch handshake."""
        if worker_cmd is None:
            open(f"{self.merged}/worker.py", "w").write(_WORKER_SERVER)
            worker_cmd = f"{self.python_bin} -u /worker.py"
        for f in ("pid.txt", "cmd.txt", "cmddone.txt"):
            p = f"{self.merged}/{f}"
            if os.path.exists(p):
                os.remove(p)
        pf = f"{self.merged}/{pid_file}"
        if os.path.exists(pf):
            os.remove(pf)
        setup = _PIVOT_SETUP.format(merged=self.merged, dns=self.dns,
                                    worker=worker_cmd, pid_file=pid_file)
        subprocess.Popen(["setsid", "unshare", "-m", "--propagation", "private",
                          "bash", "-c", setup],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if os.path.exists(pf):
                self.worker_pid = int(open(pf).read().strip())
                return self.worker_pid
            time.sleep(0.2)
        raise RuntimeError("worker did not start (no pid handshake)")

    def exec(self, command: str, timeout: float = 600.0) -> str:
        """Run a shell command inside the worker (in the overlay root)."""
        done = f"{self.merged}/cmddone.txt"
        if os.path.exists(done):
            os.remove(done)
        open(f"{self.merged}/cmd.txt", "w").write(command)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if os.path.exists(done):
                return open(done).read()
            time.sleep(0.2)
        return "rc=255\nTIMEOUT"

    def heartbeat(self) -> Optional[int]:
        try:
            return int(open(f"{self.merged}/state.txt").read().strip())
        except Exception:
            return None

    # ---- checkpoint / restore ---------------------------------------------
    def checkpoint(self, ckpt_id: str) -> None:
        if self.worker_pid is None:
            raise RuntimeError("no worker to checkpoint")
        img = os.path.join(self.img_root, ckpt_id)
        os.makedirs(img, exist_ok=True)
        _run(f"{self.criu_bin} dump -t {self.worker_pid} -D {img} --log-file dump.log -v4 "
             f"--tcp-close --ext-unix-sk --manage-cgroups=ignore "
             f"--ext-mount-map /:/ --ext-mount-map {self.channel_mount}:{self.channel_mount} "
             f"--leave-running")
        log = os.path.join(img, "dump.log")
        if not (os.path.exists(log) and "Dumping finished successfully" in open(log).read()):
            raise RuntimeError(f"CRIU dump failed for {ckpt_id}; see {log}")
        nu, nw = self._fresh()
        self._switch(self.lower_chain, nu, nw, "ckpt")
        self.lower_chain = [self.upper] + self.lower_chain
        self.upper, self.work = nu, nw
        self.snapshots[ckpt_id] = list(self.lower_chain)

    def restore(self, ckpt_id: str) -> None:
        """FS rollback (hot-switch) + CRIU restore the worker (criu --root).

        Must be run from an ISOLATED mount namespace (the caller should wrap the
        process in unshare -m / make-rprivate) so criu's mount operations cannot
        touch the host namespace.
        """
        if ckpt_id not in self.snapshots:
            raise KeyError(f"no checkpoint {ckpt_id!r}")
        # The previously-active worker was left running (checkpoint uses
        # --leave-running); it must be gone before we restore a (possibly
        # same-pid) tree from this checkpoint.
        if self.worker_pid:
            _run(f"kill -9 {self.worker_pid}")
            # CRIU (no pid ns) recreates the tree at its ORIGINAL pids; wait for
            # the killed worker to be reaped or CRIU fails "Can't fork ...: File
            # exists" because the pid is still occupied.
            for _ in range(500):
                if not os.path.exists(f"/proc/{self.worker_pid}"):
                    break
                time.sleep(0.01)
            self.worker_pid = None
        ur, wr = self._fresh()
        self._switch(self.snapshots[ckpt_id], ur, wr, "restore")
        self.lower_chain = list(self.snapshots[ckpt_id])
        self.upper, self.work = ur, wr
        img = os.path.join(self.img_root, ckpt_id)
        pidfile = os.path.join(img, "restore.pid")
        # A checkpoint may be restored many times (MCTS revisits nodes); CRIU
        # refuses to overwrite an existing --pidfile, so clear the prior one.
        if os.path.exists(pidfile):
            os.remove(pidfile)
        _run(f"{self.criu_bin} restore -D {img} --root {self.merged} "
             f"--log-file restore.log -v4 --restore-detached --tcp-close "
             f"--ext-unix-sk --manage-cgroups=ignore --ext-mount-map /:/ "
             f"--ext-mount-map {self.channel_mount}:{self.channel} --pidfile {pidfile}")
        log = os.path.join(img, "restore.log")
        if not (os.path.exists(log) and "Restore finished successfully" in open(log).read()):
            raise RuntimeError(f"CRIU restore failed for {ckpt_id}; see {log}")
        # The restored process (detached) gets a fresh host pid; capture it so
        # subsequent checkpoints target the live worker.
        try:
            self.worker_pid = int(open(pidfile).read().strip())
        except Exception:
            pass

    def teardown(self) -> None:
        if self.worker_pid:
            _run(f"kill -9 {self.worker_pid} 2>/dev/null")
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        _run(f"umount -l {self.merged}{self.channel_mount}")
        _run(f"umount -l {self.merged}")
