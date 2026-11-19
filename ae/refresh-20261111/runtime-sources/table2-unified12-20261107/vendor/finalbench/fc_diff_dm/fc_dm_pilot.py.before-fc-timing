#!/usr/bin/env python3
"""Firecracker Diff + dm-thin pilot for one Moatless SWE-search trace.

Scope of this first pilot:

* Boot a Firecracker VM with a dm-backed writable rootfs.
* Run real Moatless replay inside the VM with mock LLM also inside the VM.
* Pause at guest-reported safe points after each MCTS iteration.
* At each pause, take:
    - a Firecracker Diff memory/process snapshot;
    - a dm-thin filesystem snapshot of the VM root device.
* Restore to an earlier checkpoint and verify the guest process resumes from
  that checkpoint's paused state.

This establishes the core FC+dm mechanism before scaling to the full trajectory
set. It does not pretend to solve the controller/sandbox split problem for
continuous MCTS branch retention; that is recorded in the output JSON.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


BASE = Path(os.environ.get("AE_BASE", str(Path(__file__).resolve().parent)))
WORK_BASE = Path(os.environ.get("FCDM_WORK_BASE", str(BASE)))
D_OVERLAY = Path(os.environ["AE_D_OVERLAY"])
PAYLOAD = Path(os.environ["SPR_PAYLOAD"])
KERNEL = Path(os.environ["AE_KERNEL"])
BASE_XFS = Path(os.environ["AE_BASE_XFS"])
FC_BIN = Path(os.environ.get("FIRECRACKER", shutil.which("firecracker") or "firecracker"))

HOST_IP = os.environ.get("FCDM_HOST_IP", "172.16.0.1")
GUEST_IP = os.environ.get("FCDM_GUEST_IP", "172.16.0.2")
TAP = os.environ.get("FCDM_TAP", "fcmtap0")
TAP_MAC = os.environ.get("FCDM_TAP_MAC", "8a:7f:d9:dd:a5:d0")
GUEST_MAC = os.environ.get("FCDM_GUEST_MAC", "aa:fc:10:00:00:02")
GUEST_PORT = int(os.environ.get("FCDM_GUEST_PORT", "18080"))


def run(cmd: list[str], *, timeout: float = 60.0, check: bool = True,
        input_text: str | None = None) -> subprocess.CompletedProcess:
    cp = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if check and cp.returncode != 0:
        raise RuntimeError(
            f"command failed rc={cp.returncode}: {' '.join(cmd)}\n"
            f"stdout={cp.stdout[-2000:]}\nstderr={cp.stderr[-2000:]}"
        )
    return cp


class FCAPIError(RuntimeError):
    pass


class FirecrackerVM:
    def __init__(self, *, api_sock: Path, log_path: Path, root_dev: str,
                 mem_mib: int, vcpus: int):
        self.api_sock = api_sock
        self.requested_api_sock = api_sock
        self.socket_dir = None
        self.log_path = log_path
        self.root_dev = root_dev
        self.mem_mib = mem_mib
        self.vcpus = vcpus
        self.proc: subprocess.Popen | None = None
        self._log_f = None

    def spawn(self) -> None:
        if len(os.fsencode(self.requested_api_sock)) >= 100:
            self.socket_dir = Path(tempfile.mkdtemp(prefix='ae-fc-'))
            self.api_sock = self.socket_dir / 'api.sock'
        if self.api_sock.exists():
            self.api_sock.unlink()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_f = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [str(FC_BIN), "--api-sock", str(self.api_sock)],
            stdout=self._log_f,
            stderr=self._log_f,
        )
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if self.api_sock.exists():
                return
            time.sleep(0.05)
        raise FCAPIError("firecracker API socket did not appear")

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self.proc = None
        if self._log_f:
            self._log_f.close()
            self._log_f = None
        if self.api_sock.exists():
            try:
                self.api_sock.unlink()
            except OSError:
                pass
        if self.socket_dir is not None:
            self.socket_dir.rmdir()
            self.socket_dir = None

    def api(self, method: str, endpoint: str, data: dict | None = None,
            timeout: float = 60.0) -> bytes:
        body = json.dumps(data).encode("utf-8") if data is not None else b""
        req = (
            f"{method} /{endpoint} HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Accept: application/json\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode("utf-8") + body
        last = None
        for _ in range(30):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(timeout)
                    s.connect(str(self.api_sock))
                    s.sendall(req)
                    buf = b""
                    while b"\r\n\r\n" not in buf:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                    headers_raw, _, body_raw = buf.partition(b"\r\n\r\n")
                    headers = headers_raw.decode("utf-8", "replace")
                    status = headers.split("\r\n", 1)[0]
                    parts = status.split()
                    if len(parts) < 2:
                        raise FCAPIError(f"bad status {status!r}")
                    code = int(parts[1])
                    m = re.search(r"(?im)^content-length:\s*(\d+)\s*$", headers)
                    need = int(m.group(1)) if m else 0
                    while len(body_raw) < need:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                        body_raw += chunk
                    if 200 <= code < 300:
                        return body_raw
                    raise FCAPIError(f"{method} /{endpoint} -> {code}: {body_raw[:500]!r}")
            except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
                last = e
                time.sleep(0.1)
        raise FCAPIError(f"API connect failed: {last}")

    def configure_and_start(self) -> None:
        self.api("PUT", "machine-config", {
            "vcpu_count": self.vcpus,
            "mem_size_mib": self.mem_mib,
            "smt": False,
            "track_dirty_pages": True,
        })
        boot_args = (
            "console=ttyS0,115200n8 reboot=k panic=1 pci=off acpi=off "
            "systemd.journald.forward_to_console=1 "
            f"ip={GUEST_IP}::{HOST_IP}:255.255.255.0::eth0:off rw "
            "init=/lib/systemd/systemd systemd.unit=multi-user.target"
        )
        self.api("PUT", "boot-source", {
            "kernel_image_path": str(KERNEL),
            "boot_args": boot_args,
        })
        self.api("PUT", "drives/rootfs", {
            "drive_id": "rootfs",
            "path_on_host": self.root_dev,
            "is_root_device": True,
            "is_read_only": False,
        })
        # The existing base.xfs waits for /dev/vdb during boot. Supplying a
        # read-only data drive keeps the image's systemd dependency graph happy.
        self.api("PUT", "drives/data", {
            "drive_id": "data",
            "path_on_host": str(Path(os.environ["AE_FC_DATA_XFS"])),
            "is_root_device": False,
            "is_read_only": True,
        })
        self.api("PUT", "network-interfaces/eth0", {
            "iface_id": "eth0",
            "guest_mac": GUEST_MAC,
            "host_dev_name": TAP,
        })
        self.api("PUT", "actions", {"action_type": "InstanceStart"})

    def pause(self) -> None:
        self.api("PATCH", "vm", {"state": "Paused"}, timeout=30.0)

    def resume(self) -> None:
        self.api("PATCH", "vm", {"state": "Resumed"}, timeout=30.0)

    def take_snapshot(self, *, snapshot_path: Path, mem_path: Path,
                      snapshot_type: str) -> dict:
        t0 = time.perf_counter()
        self.pause()
        pause_ms = (time.perf_counter() - t0) * 1000.0
        t1 = time.perf_counter()
        self.api("PUT", "snapshot/create", {
            "snapshot_path": str(snapshot_path),
            "mem_file_path": str(mem_path),
            "snapshot_type": snapshot_type,
        }, timeout=180.0)
        create_ms = (time.perf_counter() - t1) * 1000.0
        self.resume()
        total_ms = (time.perf_counter() - t0) * 1000.0
        size_disk = mem_path.stat().st_blocks * 512 if mem_path.exists() else 0
        return {
            "fc_total_ms": total_ms,
            "fc_pause_ms": pause_ms,
            "fc_create_ms": create_ms,
            "fc_mem_disk_mb": size_disk / 1024 / 1024,
        }

    def load_snapshot(self, *, snapshot_path: Path, mem_path: Path,
                      enable_diff: bool) -> dict:
        self.kill()
        self.spawn()
        t0 = time.perf_counter()
        self.api("PUT", "snapshot/load", {
            "snapshot_path": str(snapshot_path),
            "mem_backend": {
                "backend_type": "File",
                "backend_path": str(mem_path),
            },
            "enable_diff_snapshots": enable_diff,
            "resume_vm": True,
        }, timeout=180.0)
        return {"fc_load_ms": (time.perf_counter() - t0) * 1000.0}


class DMThin:
    """Small dm-thin manager for a rootfs image.

    Each checkpoint creates a thin snapshot volume. Restore keeps the VM root
    block-device path stable and reloads that dm device to point at the target
    thin id; this matters because Firecracker snapshots remember the drive path.
    """

    def __init__(
        self,
        work_dir: Path,
        root_img: Path,
        *,
        data_size: str = "24G",
        meta_size: str = "256M",
    ):
        self.work_dir = work_dir
        self.root_img = root_img
        self.data_size = data_size
        self.meta_size = meta_size
        self.uid = f"fcdm{os.getpid()}"
        self.data_img = work_dir / f"{self.uid}_thin_data.img"
        self.meta_img = work_dir / f"{self.uid}_thin_meta.img"
        self.data_loop = ""
        self.meta_loop = ""
        self.pool = f"{self.uid}_pool"
        self.pool_dev = ""
        self.active_id = 1
        self.root_name = f"{self.uid}_root"
        self.root_dev = ""
        self.root_sectors = 0
        self.vols: dict[str, int] = {"root": 1}
        self.devices: dict[str, str] = {}
        self.created_names: list[str] = []

    def setup(self) -> str:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        run(["truncate", "-s", self.data_size, str(self.data_img)])
        run(["truncate", "-s", self.meta_size, str(self.meta_img)])
        self.data_loop = run(["losetup", "--find", "--show", str(self.data_img)]).stdout.strip()
        self.meta_loop = run(["losetup", "--find", "--show", str(self.meta_img)]).stdout.strip()
        sectors = int(run(["blockdev", "--getsz", self.data_loop]).stdout.strip())
        # low_water_mark = 128 sectors
        table = f"0 {sectors} thin-pool {self.meta_loop} {self.data_loop} 128 0\n"
        run(["dmsetup", "create", "--noudevsync", self.pool, "--table", table])
        self.pool_dev = self._kernel_dm_dev(self.pool)
        run(["dmsetup", "message", self.pool, "0", "create_thin", "1"])
        self.root_sectors = int(run(["blockdev", "--getsz", str(self.root_img)]).stdout.strip())
        self.created_names.append(self.root_name)
        run(["dmsetup", "create", "--noudevsync", self.root_name, "--table",
             f"0 {self.root_sectors} thin {self.pool_dev} 1\n"])
        root_dev = self._kernel_dm_dev(self.root_name)
        self.root_dev = root_dev
        run(["dd", f"if={self.root_img}", f"of={root_dev}", "bs=16M", "conv=fsync"],
            timeout=240)
        self.devices["root"] = root_dev
        return root_dev

    def snapshot(self, label: str) -> dict:
        if label in self.vols:
            return {"dm_snapshot_ms": 0.0, "dm_dev": self.devices[label], "dm_duplicate": True}
        new_id = max(self.vols.values()) + 1
        t0 = time.perf_counter()
        run(["dmsetup", "message", self.pool, "0", "create_snap",
             str(new_id), str(self.active_id)])
        name = f"{self.uid}_{label}"
        self.created_names.append(name)
        run(["dmsetup", "create", "--noudevsync", name, "--table",
             f"0 {self.root_sectors} thin {self.pool_dev} {new_id}\n"])
        ms = (time.perf_counter() - t0) * 1000.0
        dev = self._kernel_dm_dev(name)
        self.vols[label] = new_id
        self.devices[label] = dev
        return {"dm_snapshot_ms": ms, "dm_dev": dev, "dm_thin_id": new_id}

    def restore_dev(self, label: str) -> dict:
        if label not in self.devices:
            raise KeyError(f"no dm snapshot label {label}")
        t0 = time.perf_counter()
        target_id = self.vols[label]
        table = f"0 {self.root_sectors} thin {self.pool_dev} {target_id}\n"
        run(["dmsetup", "suspend", "--noudevsync", self.root_name], timeout=30)
        try:
            run(["dmsetup", "load", "--noudevsync", self.root_name, "--table", table], timeout=30)
        except Exception:
            run(["dmsetup", "resume", "--noudevsync", self.root_name], check=False)
            raise
        run(["dmsetup", "resume", "--noudevsync", self.root_name], timeout=30)
        ms = (time.perf_counter() - t0) * 1000.0
        self.active_id = self.vols[label]
        return {
            "dm_restore_ms": ms,
            "dm_dev": self.root_dev,
            "dm_target_label": label,
            "dm_target_thin_id": target_id,
        }

    def teardown(self) -> None:
        for name in reversed(self.created_names):
            run(["dmsetup", "remove", "--noudevsync", "--force", name], check=False)
        run(["dmsetup", "remove", "--noudevsync", "--force", self.pool], check=False)
        if self.data_loop:
            run(["losetup", "-d", self.data_loop], check=False)
        if self.meta_loop:
            run(["losetup", "-d", self.meta_loop], check=False)
        for p in (self.data_img, self.meta_img):
            try:
                p.unlink()
            except OSError:
                pass

    @staticmethod
    def _kernel_dm_dev(name: str) -> str:
        out = run(["dmsetup", "info", "-c", "--noheadings", "-o", "major,minor", name]).stdout.strip()
        parts = [p.strip() for p in out.replace(":", " ").split() if p.strip()]
        if len(parts) < 2:
            raise RuntimeError(f"cannot resolve dm major/minor for {name}: {out!r}")
        major, minor = parts[0], parts[1]
        # Device-mapper major is normally 253; minor maps to /dev/dm-<minor>.
        dev = Path(f"/dev/dm-{minor}")
        if not dev.exists():
            # Give devtmpfs a brief moment; this does not depend on udev symlinks.
            for _ in range(20):
                if dev.exists():
                    break
                time.sleep(0.05)
        if not dev.exists():
            raise RuntimeError(f"kernel dm device {dev} for {name} not present (major={major})")
        return str(dev)


def http_json(url: str, method: str = "GET", timeout: float = 5.0) -> dict:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def setup_tap() -> None:
    if not Path(f"/sys/class/net/{TAP}").exists():
        run(["ip", "tuntap", "add", "dev", TAP, "mode", "tap"])
    run(["ip", "link", "set", "dev", TAP, "address", TAP_MAC])
    run(["ip", "link", "set", TAP, "up"])
    run(["ip", "addr", "flush", "dev", TAP], check=False)
    run(["ip", "addr", "add", f"{HOST_IP}/24", "dev", TAP])
    run(["ip", "route", "replace", f"{GUEST_IP}/32", "dev", TAP, "src", HOST_IP])
    run(["ip", "neigh", "replace", GUEST_IP, "lladdr", GUEST_MAC, "dev", TAP, "nud", "permanent"],
        check=False)
    # Some hosts have high-priority policy routing rules for user traffic. Add a
    # more specific high-priority rule for this pilot endpoint.
    run(["ip", "rule", "add", "to", f"{GUEST_IP}/32", "priority", "100", "lookup", "main"],
        check=False)


def wait_guest_state(*, paused: bool | None = None, phase: str | None = None,
                     timeout_s: float = 180.0) -> dict:
    deadline = time.time() + timeout_s
    last = None
    url = f"http://{GUEST_IP}:{GUEST_PORT}/state"
    while time.time() < deadline:
        try:
            s = http_json(url, timeout=2.0)
            if (paused is None or s.get("paused") == paused) and (
                phase is None or s.get("phase") == phase
            ):
                return s
            last = s
            if s.get("phase") == "error":
                return s
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = repr(e)
        time.sleep(0.25)
    raise TimeoutError(f"guest state timeout paused={paused} phase={phase}; last={last}")


def guest_continue() -> None:
    http_json(f"http://{GUEST_IP}:{GUEST_PORT}/continue", method="POST", timeout=5.0)


def prepare_rootfs(
    instance: str,
    rootfs: Path,
    *,
    guest_driver: str = "guest_moatless_pause_driver.py",
) -> None:
    if rootfs.exists():
        rootfs.unlink()
    shutil.copyfile(BASE_XFS, rootfs)
    run(["truncate", "-s", "8G", str(rootfs)])
    mp = WORK_BASE / "mnt" / "rootfs_inject"
    mp.mkdir(parents=True, exist_ok=True)
    run(["mount", "-o", "loop,nouuid", str(rootfs), str(mp)])
    try:
        run(["xfs_growfs", str(mp)], timeout=60)
        # Minimal Python 3.11 runtime for the existing venv.
        for src, dst in [
            (Path("/usr/bin/python3.11"), mp / "usr/bin/python3.11"),
            (Path("/usr/lib/python3.11"), mp / "usr/lib/python3.11"),
            (Path("/usr/lib/x86_64-linux-gnu/libpython3.11.so.1.0"),
             mp / "usr/lib/x86_64-linux-gnu/libpython3.11.so.1.0"),
        ]:
            if src.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst, symlinks=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        libdir = mp / "usr/lib/x86_64-linux-gnu"
        for link in ["libpython3.11.so", "libpython3.11.so.1"]:
            p = libdir / link
            if p.exists() or p.is_symlink():
                p.unlink()
        os.symlink("libpython3.11.so.1.0", libdir / "libpython3.11.so.1")
        os.symlink("libpython3.11.so.1", libdir / "libpython3.11.so")

        # Payload subset.
        targets = [
            (Path(os.environ["MOATLESS_VENV"]), mp / "mnt/disk2/dyp/moatless_det_venv"),
            (PAYLOAD / "moatless-det-src", mp / "mnt/disk2/dyp/spr_payload/moatless-det-src"),
            (PAYLOAD / "repos" / f"swe-bench_{instance}",
             mp / "mnt/disk2/dyp/spr_payload/repos" / f"swe-bench_{instance}"),
            (PAYLOAD / "index_store" / instance,
             mp / "mnt/disk2/dyp/spr_payload/index_store" / instance),
            (PAYLOAD / "det_traces" / "ms" / instance,
             mp / "mnt/disk2/dyp/spr_payload/det_traces/ms" / instance),
        ]
        for name in ["mock_llm_server.py", "protocol.py", "replay_driver.py", "trajectory_index.py", "baseline_runtime.py"]:
            targets.append((PAYLOAD / name, mp / "mnt/disk2/dyp/spr_payload" / name))
        nltk_data = os.environ.get("NLTK_DATA")
        if nltk_data:
            targets.append((Path(nltk_data), mp / "mnt/disk2/dyp/nltk_data"))
        targets.append((BASE / guest_driver, mp / f"root/{guest_driver}"))

        for src, dst in targets:
            if dst.exists():
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)

        mock_trace_dir = mp / "var/lib/finalbench/det_mock_traces/qwen3-coder-30b-ms"
        mock_trace_dir.mkdir(parents=True, exist_ok=True)
        link = mock_trace_dir / instance
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink(f"/mnt/disk2/dyp/spr_payload/det_traces/ms/{instance}", link)

        runtime_config = json.loads(os.environ.get("DELTABOX_BASELINE_TEST_RUNTIME", '{"backend":"none"}'))
        if runtime_config.get("backend") == "local-pytest":
            # The rootfs stages this venv at a fixed guest path. Arbitrary host
            # environments are not silently assumed to exist in the guest.
            expected = str(Path(os.environ["MOATLESS_VENV"]) / "bin/python")
            if runtime_config["python"] == expected:
                runtime_config["python"] = "/mnt/disk2/dyp/moatless_det_venv/bin/python"
            else:
                # A standalone interpreter can be copied without a host venv's
                # outside symlinks/base interpreter. Keep old workload Python
                # independent of the Python running Moatless and CRIU.
                identity = json.loads(subprocess.check_output([runtime_config["python"], '-c',
                    'import sys,json; print(json.dumps([sys.prefix,sys.base_prefix,sys.executable]))'],
                    text=True, timeout=10))
                prefix = Path(identity[0]).resolve()
                executable = Path(identity[2]).resolve()
                if (identity[0] != identity[1] or prefix in (Path('/usr'), Path('/usr/local'))
                        or not executable.is_relative_to(prefix)
                        or not list((prefix / 'lib').glob('python3.*'))):
                    raise ValueError('FC external test Python must be a self-contained standalone installation')
                guest_prefix = mp / 'root/deltabox-test-python'
                if guest_prefix.exists():
                    shutil.rmtree(guest_prefix)
                shutil.copytree(prefix, guest_prefix, symlinks=True)
                runtime_config["python"] = str(Path('/root/deltabox-test-python') / executable.relative_to(prefix))
            if not any((mp / name).is_file() for name in ("usr/bin/git", "bin/git")):
                raise RuntimeError("FC local-pytest requires git in the guest rootfs; rebuild the image first")
        runtime_json = json.dumps(runtime_config, separators=(",", ":"))
        # JSON file avoids systemd Environment quoting/percent expansion.
        (mp / "root/baseline-test-runtime.json").write_text(runtime_json)
        unit = f"""[Unit]
Description=FinalBench FC+dm Moatless guest driver
After=network.target

[Service]
Type=simple
Environment=INSTANCE_ID={instance}
Environment=CONTROL_PORT={GUEST_PORT}
Environment=MOCK_PORT=19999
Environment=MOCK_TRACES_ROOT=/var/lib/finalbench/det_mock_traces
Environment=PYTHONHASHSEED=0
Environment=OPENAI_API_KEY=dummy
Environment=DELTABOX_BASELINE_TEST_RUNTIME_FILE=/root/baseline-test-runtime.json
Environment=LITELLM_LOCAL_MODEL_COST_MAP=True
{('Environment=NLTK_DATA=/mnt/disk2/dyp/nltk_data' if nltk_data else '')}
Environment=PYTHONPATH=/mnt/disk2/dyp/spr_payload:/mnt/disk2/dyp/spr_payload/moatless-det-src
ExecStart=/mnt/disk2/dyp/moatless_det_venv/bin/python /root/{guest_driver}
StandardOutput=journal+console
StandardError=journal+console
Restart=no

[Install]
WantedBy=multi-user.target
"""
        unit_path = mp / "etc/systemd/system/finalbench-fc-dm.service"
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(unit, encoding="utf-8")
        wants = mp / "etc/systemd/system/multi-user.target.wants"
        wants.mkdir(parents=True, exist_ok=True)
        svc_link = wants / "finalbench-fc-dm.service"
        if svc_link.exists() or svc_link.is_symlink():
            svc_link.unlink()
        os.symlink("/etc/systemd/system/finalbench-fc-dm.service", svc_link)

        stale_net = wants / "finalbench-net.service"
        if stale_net.exists() or stale_net.is_symlink():
            stale_net.unlink()

        # Make per-worker networking explicit inside the guest. The kernel
        # command line also carries an ip= stanza, but systemd-networkd in the
        # base image can later replace it. Full runs use one TAP/subnet per
        # NUMA worker, so the guest rootfs must be injected with that worker's
        # concrete address.
        netdir = mp / "etc/systemd/network"
        netdir.mkdir(parents=True, exist_ok=True)
        for old in netdir.glob("*.network"):
            old.unlink()
        (netdir / "20-finalbench-static.network").write_text(
            f"""[Match]
Name=eth0

[Network]
Address={GUEST_IP}/24
Gateway={HOST_IP}
DNS=8.8.8.8
ConfigureWithoutCarrier=yes
""",
            encoding="utf-8",
        )
    finally:
        run(["umount", str(mp)], timeout=60, check=True)


def merge_mem(base_mem: Path, diff_mems: list[Path], out_mem: Path) -> dict:
    sys.path.insert(0, str(D_OVERLAY / "kunpeng/deltabox/benchmarks/benchmarks_src/trace_replay"))
    from diff_merger import merge
    return merge(str(base_mem), [str(p) for p in diff_mems], str(out_mem))


def run_pilot(instance: str, max_ckpts: int, restore_seq: int,
              mem_mib: int, vcpus: int) -> dict:
    results = BASE / "results" / f"pilot_{instance}"
    snaps = WORK_BASE / "snapshots" / f"pilot_{instance}"
    logs = WORK_BASE / "logs" / f"pilot_{instance}"
    dm_work = WORK_BASE / "dm_work" / f"pilot_{instance}"
    image = WORK_BASE / "images" / f"pilot_{instance}.xfs"
    image.parent.mkdir(parents=True, exist_ok=True)
    print(f"[pilot] start instance={instance} max_ckpts={max_ckpts} restore_seq={restore_seq}", flush=True)
    for p in [results, snaps, logs, dm_work]:
        if p.exists():
            shutil.rmtree(p)
        p.mkdir(parents=True, exist_ok=True)
    print("[pilot] preparing rootfs", flush=True)
    prepare_rootfs(instance, image)
    print("[pilot] setting up tap", flush=True)
    setup_tap()
    print("[pilot] setting up dm-thin", flush=True)
    dm = DMThin(dm_work, image)
    root_dev = dm.setup()
    print(f"[pilot] dm root device {root_dev}", flush=True)
    vm = FirecrackerVM(
        api_sock=WORK_BASE / "fc_pilot.socket",
        log_path=logs / "firecracker.log",
        root_dev=root_dev,
        mem_mib=mem_mib,
        vcpus=vcpus,
    )
    ckpts: list[dict] = []
    conditions = {
        "cpu_governor": Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text().strip(),
        "cpu_min_freq": Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_min_freq").read_text().strip(),
        "cpu_max_freq": Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq").read_text().strip(),
        "numa_command_required": "run this script under numactl --cpunodebind=0 --membind=0",
    }
    try:
        print("[pilot] spawning firecracker", flush=True)
        vm.spawn()
        print("[pilot] configuring VM", flush=True)
        vm.configure_and_start()
        print("[pilot] waiting for guest root pause", flush=True)
        s = wait_guest_state(paused=True, timeout_s=240.0)
        if not s.get("ok"):
            raise RuntimeError(f"guest error before root checkpoint: {s}")

        base_vmstate = snaps / "base.vmstate"
        base_mem = snaps / "base.mem"
        fc = vm.take_snapshot(snapshot_path=base_vmstate, mem_path=base_mem, snapshot_type="Full")
        dmres = dm.snapshot("seq0")
        ckpts.append({"seq": 0, "state": s, "vmstate": str(base_vmstate),
                      "mem": str(base_mem), "diff_chain": [], **fc, **dmres})
        print(f"[ckpt 0] node={s.get('node_id')} fc={fc['fc_total_ms']:.1f}ms dm={dmres['dm_snapshot_ms']:.1f}ms", flush=True)

        for _ in range(max_ckpts):
            guest_continue()
            s = wait_guest_state(paused=True, timeout_s=240.0)
            if not s.get("ok"):
                raise RuntimeError(f"guest error at checkpoint: {s}")
            seq = int(s["checkpoint_seq"])
            vmstate = snaps / f"diff_{seq}.vmstate"
            mem = snaps / f"diff_{seq}.mem"
            fc = vm.take_snapshot(snapshot_path=vmstate, mem_path=mem, snapshot_type="Diff")
            dmres = dm.snapshot(f"seq{seq}")
            prev_chain = ckpts[-1]["diff_chain"] if ckpts else []
            ckpts.append({"seq": seq, "state": s, "vmstate": str(vmstate), "mem": str(mem),
                          "diff_chain": prev_chain + [str(mem)], **fc, **dmres})
            print(f"[ckpt {seq}] node={s.get('node_id')} cursor={(s.get('mock_stats') or {}).get('cursor')} fc={fc['fc_total_ms']:.1f}ms dm={dmres['dm_snapshot_ms']:.1f}ms", flush=True)
            if len(ckpts) - 1 >= max_ckpts:
                break

        target = next((c for c in ckpts if c["seq"] == restore_seq), None)
        if target is None:
            raise RuntimeError(f"restore_seq {restore_seq} not captured")
        merged_mem = snaps / f"merged_seq{restore_seq}.mem"
        merge = merge_mem(base_mem, [Path(p) for p in target["diff_chain"]], merged_mem)
        dm_restore = dm.restore_dev(f"seq{restore_seq}")
        load = vm.load_snapshot(
            snapshot_path=Path(target["vmstate"]),
            mem_path=merged_mem if target["diff_chain"] else base_mem,
            enable_diff=False,
        )
        restored_state = wait_guest_state(paused=True, timeout_s=120.0)
        ok_restore = (
            restored_state.get("checkpoint_seq") == restore_seq
            and restored_state.get("paused") is True
        )
        print(f"[restore seq{restore_seq}] ok={ok_restore} state={restored_state}", flush=True)

        # Continue once after restore to prove guest process can make forward progress.
        guest_continue()
        next_state = wait_guest_state(paused=True, timeout_s=240.0)

        out = {
            "ok": bool(ok_restore and next_state.get("ok")),
            "instance": instance,
            "vm_config": {"mem_mib": mem_mib, "vcpus": vcpus},
            "conditions": conditions,
            "ckpts": ckpts,
            "restore": {
                "target_seq": restore_seq,
                "merge": merge,
                "dm_restore": dm_restore,
                "load": load,
                "restored_state": restored_state,
                "next_state_after_continue": next_state,
                "semantic_note": (
                    "This pilot snapshots the whole VM including Moatless and mock state. "
                    "Restoring resumes that older process state; it proves FC+dm restore "
                    "mechanics, but continuous MCTS with retained controller tree needs "
                    "a controller-outside-sandbox design."
                ),
            },
        }
        results.mkdir(parents=True, exist_ok=True)
        (results / "pilot_result.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
        return out
    finally:
        vm.kill()
        dm.teardown()
        run(["ip", "rule", "del", "to", f"{GUEST_IP}/32", "priority", "100", "lookup", "main"],
            check=False)
        run(["ip", "route", "del", f"{GUEST_IP}/32"], check=False)
        run(["ip", "link", "del", TAP], check=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", default="django__django-14672")
    ap.add_argument("--max-ckpts", type=int, default=4)
    ap.add_argument("--restore-seq", type=int, default=2)
    ap.add_argument("--mem-mib", type=int, default=8192)
    ap.add_argument("--vcpus", type=int, default=4)
    args = ap.parse_args()
    result = run_pilot(args.instance, args.max_ckpts, args.restore_seq, args.mem_mib, args.vcpus)
    print(json.dumps(result["restore"], indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
