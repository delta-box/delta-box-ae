#!/usr/bin/env python3
"""Start a single-disk Firecracker debug VM for the DeltaFS examples.

Default usage from example/:

    sudo python3 scripts/start_debug_vm.py

The VM boots from a throwaway copy of assets/base.xfs, waits for SSH, then
prints connection and cleanup commands. It does not stage example files or run
any demo by itself.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import signal
import shutil
import subprocess
import sys
import time
import tempfile
from pathlib import Path


EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = EXAMPLE_ROOT.parent
DEFAULT_BASE = EXAMPLE_ROOT / "assets" / "base.xfs"
DEFAULT_ASSET_KERNEL = EXAMPLE_ROOT / "assets" / "vmlinux"
DEFAULT_REPO_KERNEL = REPO_ROOT / "linux-6.8" / "vmlinux"
DEFAULT_RUN_ROOTFS = EXAMPLE_ROOT / "assets" / "base-debug-run.xfs"
DEFAULT_SOCKET = Path("/tmp/firecracker-deltafs-example.socket")
DEFAULT_LOG = Path("/tmp/firecracker-deltafs-example.log")
HOST_IP = "172.16.0.1"
GUEST_IP = "172.16.0.2"
NETMASK = "24"
TAP = "tap-deltafs0"


def invoking_user_home() -> Path:
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass
    return Path.home()


def default_pubkey() -> Path | None:
    home = invoking_user_home()
    for name in ("id_ed25519.pub", "id_rsa.pub"):
        key = home / ".ssh" / name
        if key.exists():
            return key
    return None


def ssh_opts() -> list[str]:
    opts = [
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "BatchMode=yes",
        "-o",
        "PasswordAuthentication=no",
    ]
    home = invoking_user_home()
    for name in ("id_ed25519", "id_rsa"):
        key = home / ".ssh" / name
        if key.exists():
            opts += ["-i", str(key)]
            break
    return opts


def quote_cmd(parts: list[str]) -> str:
    return " ".join(shlex_quote(part) for part in parts)


def shlex_quote(text: str) -> str:
    if not text:
        return "''"
    safe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_+-=.,/:@%")
    if all(ch in safe for ch in text):
        return text
    return "'" + text.replace("'", "'\"'\"'") + "'"


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    timeout: int | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    print("+", quote_cmd(cmd), flush=True)
    return subprocess.run(cmd, check=check, text=True, capture_output=capture, timeout=timeout, cwd=cwd)


def require_root() -> None:
    if os.geteuid() != 0:
        sys.exit("ERROR: run with sudo/root; Firecracker and tap setup need CAP_NET_ADMIN.")


def resolve_default_kernel() -> Path:
    if DEFAULT_ASSET_KERNEL.exists():
        return DEFAULT_ASSET_KERNEL
    return DEFAULT_REPO_KERNEL


def check_prereqs(args: argparse.Namespace) -> None:
    required = ["firecracker", "curl", "ssh", "scp", "ip", "mount", "umount", "unshare"]
    if not args.no_nat:
        required += ["iptables", "sysctl"]
    missing = [tool for tool in required if shutil.which(tool) is None]
    if missing:
        sys.exit(f"ERROR: missing host tools: {', '.join(missing)}")
    if not args.kernel.exists():
        sys.exit(f"ERROR: kernel not found: {args.kernel}")
    if not args.base_xfs.exists():
        sys.exit(f"ERROR: base rootfs not found: {args.base_xfs}")
    if args.ssh_pubkey and not args.ssh_pubkey.exists():
        sys.exit(f"ERROR: ssh public key not found: {args.ssh_pubkey}")
    if args.base_xfs.resolve() == args.run_rootfs.resolve():
        sys.exit("ERROR: runtime rootfs must differ from the base image")
    if getattr(args, "data_xfs", None) and not args.data_xfs.is_file():
        sys.exit(f"ERROR: data image not found: {args.data_xfs}")


def prepare_rootfs(args: argparse.Namespace) -> None:
    if args.socket.exists():
        args.socket.unlink()
    if args.run_rootfs.exists() and not args.reuse_rootfs:
        args.run_rootfs.unlink()
    if not args.run_rootfs.exists():
        print(f"[host] copying rootfs: {args.base_xfs} -> {args.run_rootfs}")
        if getattr(args, "consume_staged_base", False):
            # A verified, private RAM image may become the writable rootfs.
            # Keep the fresh-path guard; never rename a shared source image.
            if args.base_xfs.parent.resolve() != args.run_rootfs.parent.resolve():
                raise ValueError("Disposable staged base must share the private runtime directory")
            args.base_xfs.rename(args.run_rootfs)
        else:
            shutil.copyfile(args.base_xfs, args.run_rootfs)


def inject_ssh_key(args: argparse.Namespace) -> None:
    pubkey = args.ssh_pubkey or default_pubkey()
    if not pubkey or not pubkey.exists():
        print("[host] WARN: no SSH public key found; SSH may fail")
        return

    mount_point = Path(tempfile.mkdtemp(prefix="deltafs-rootfs-"))
    mounted = False
    run(["mkdir", "-p", str(mount_point)])
    try:
        run(["mount", "-o", "loop", str(args.run_rootfs), str(mount_point)])
        mounted = True
        ssh_dir = mount_point / "root" / ".ssh"
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        auth = ssh_dir / "authorized_keys"
        key_text = pubkey.read_text().strip()
        existing = auth.read_text() if auth.exists() else ""
        if key_text not in existing:
            with auth.open("a") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write(key_text + "\n")
        auth.chmod(0o600)
        ssh_dir.chmod(0o700)
        print(f"[host] injected SSH public key: {pubkey}")
    finally:
        if mounted:
            run(["umount", str(mount_point)])
        mount_point.rmdir()


def get_default_interface() -> str | None:
    try:
        result = subprocess.check_output(["ip", "route", "get", "1.1.1.1"], text=True)
    except Exception:
        return None
    parts = result.split()
    if "dev" in parts:
        idx = parts.index("dev")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def setup_tap(tap: str) -> None:
    run(["ip", "tuntap", "add", "dev", tap, "mode", "tap"])
    try:
        run(["ip", "addr", "add", f"{HOST_IP}/{NETMASK}", "dev", tap])
        run(["ip", "link", "set", tap, "up"])
    except BaseException:
        # The add above succeeded, so this tap belongs to this attempt.
        run(["ip", "link", "del", tap], check=False)
        raise


def route_guest_to_tap(guest_ip: str, tap: str) -> None:
    run(["ip", "route", "replace", f"{guest_ip}/32", "dev", tap, "src", HOST_IP])


def configure_nat(tap: str) -> None:
    iface = get_default_interface()
    if not iface:
        print("[host] WARN: could not detect outbound interface; skipping NAT")
        return
    run(["sysctl", "-w", "net.ipv4.ip_forward=1"])
    check_rule = ["iptables", "-t", "nat", "-C", "POSTROUTING", "-o", iface, "-j", "MASQUERADE"]
    if run(check_rule, check=False).returncode != 0:
        run(["iptables", "-t", "nat", "-A", "POSTROUTING", "-o", iface, "-j", "MASQUERADE"])
    forward_rule = ["iptables", "-C", "FORWARD", "-i", tap, "-o", iface, "-j", "ACCEPT"]
    if run(forward_rule, check=False).returncode != 0:
        run(["iptables", "-A", "FORWARD", "-i", tap, "-o", iface, "-j", "ACCEPT"])
    established_rule = ["iptables", "-C", "FORWARD", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"]
    if run(established_rule, check=False).returncode != 0:
        run(["iptables", "-A", "FORWARD", "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"])
    print(f"[host] NAT configured: {tap} -> {iface}")


def fc_put(socket: Path, endpoint: str, data: dict) -> None:
    payload = json.dumps(data)
    cmd = [
        "curl",
        "--unix-socket",
        socket.name,
        "-sS",
        "--fail-with-body",
        "-X",
        "PUT",
        f"http://localhost/{endpoint}",
        "-H",
        "Content-Type: application/json",
        "-d",
        payload,
    ]
    for attempt in range(1, 8):
        res = run(cmd, check=False, capture=True, cwd=socket.resolve().parent)
        if res.returncode == 0:
            return
        print((res.stdout or "") + (res.stderr or ""), end="")
        time.sleep(0.5 * attempt)
    raise RuntimeError(f"failed to configure Firecracker endpoint: {endpoint}")


def host_net_diagnostics(ip: str, tap: str, fc: subprocess.Popen[bytes]) -> str:
    lines = [f"firecracker pid={fc.pid} poll={fc.poll()}"]
    checks = [
        ["ip", "addr", "show", tap],
        ["ip", "route", "get", ip],
        ["ip", "neigh", "show", "dev", tap],
        ["ping", "-c", "1", "-W", "1", ip],
    ]
    for cmd in checks:
        if shutil.which(cmd[0]) is None:
            lines.append(f"$ {quote_cmd(cmd)}\nmissing tool: {cmd[0]}")
            continue
        res = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = (res.stdout or "").strip()
        lines.append(f"$ {quote_cmd(cmd)}  # rc={res.returncode}\n{out or '(no output)'}")
    return "\n".join(lines)


def wait_for_ssh(ip: str, timeout_s: int, log_path: Path, tap: str, fc: subprocess.Popen[bytes]) -> None:
    deadline = time.time() + timeout_s
    opts = ssh_opts()
    last_output = ""
    attempt = 0
    while time.time() < deadline:
        if fc.poll() is not None:
            raise RuntimeError(f"Firecracker exited before SSH became ready; rc={fc.returncode}; log={log_path}")
        attempt += 1
        res = subprocess.run(
            ["ssh", *opts, "-o", "ConnectTimeout=2", f"root@{ip}", "true"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if res.returncode == 0:
            return
        last_output = (res.stdout or "").strip()
        if attempt == 1 or attempt % 5 == 0:
            detail = f": {last_output}" if last_output else ""
            print(f"[host] SSH not ready yet (attempt {attempt}, rc={res.returncode}){detail}", flush=True)
        time.sleep(2)
    raise TimeoutError(
        "\n".join(
            [
                f"SSH did not become ready on {ip} within {timeout_s}s.",
                f"last ssh output: {last_output or '(none)'}",
                "host network diagnostics:",
                host_net_diagnostics(ip, tap, fc),
                f"firecracker log: {log_path}",
                f"manual ssh: {quote_cmd(['ssh', *opts, f'root@{ip}'])}",
            ]
        )
    )


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Boot a single-disk DeltaFS example VM and leave it ready for SSH")
    ap.add_argument("--kernel", type=Path, default=resolve_default_kernel())
    ap.add_argument("--base-xfs", type=Path, default=DEFAULT_BASE)
    ap.add_argument("--data-xfs", type=Path, help="Optional read-only second disk")
    ap.add_argument("--run-rootfs", type=Path, default=DEFAULT_RUN_ROOTFS)
    ap.add_argument("--ssh-pubkey", type=Path, default=None)
    ap.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--tap", default=TAP)
    ap.add_argument("--guest-ip", default=GUEST_IP)
    ap.add_argument("--vcpus", type=int, default=2)
    ap.add_argument("--mem-mib", type=int, default=2048)
    ap.add_argument("--ssh-timeout", type=int, default=120)
    ap.add_argument("--reuse-rootfs", action="store_true", help="Do not recopy base.xfs if --run-rootfs exists")
    ap.add_argument("--keep-vm", action="store_true", help="Alias for the default behavior; VM is left running")
    ap.add_argument("--no-nat", action="store_true", help="Skip host NAT setup for guest outbound network")
    return ap.parse_args()


def start_vm(args: argparse.Namespace) -> subprocess.Popen:
    require_root()
    check_prereqs(args)
    if args.socket.exists() or args.run_rootfs.exists():
        raise FileExistsError("VM socket/rootfs must be fresh task-owned paths")
    prepare_rootfs(args)
    inject_ssh_key(args)
    setup_tap(args.tap)
    args._tap_created = True
    route_guest_to_tap(args.guest_ip, args.tap)
    if not args.no_nat:
        configure_nat(args.tap)

    print(f"[host] starting Firecracker; log={args.log}")
    logf = open(args.log, "wb")
    fc = subprocess.Popen(
        ["firecracker", "--api-sock", args.socket.name],
        cwd=args.socket.resolve().parent,
        stdout=logf,
        stderr=logf,
        start_new_session=not getattr(args, "inherit_process_group", False),
    )
    args._process = fc
    logf.close()
    time.sleep(0.7)

    boot_args = (
        "console=ttyS0,115200n8 reboot=k panic=1 pci=off acpi=off "
        f"ip={args.guest_ip}::{HOST_IP}:255.255.255.0::eth0:off "
        "rw init=/lib/systemd/systemd systemd.unit=multi-user.target"
    )
    fc_put(args.socket, "machine-config", {"vcpu_count": args.vcpus, "mem_size_mib": args.mem_mib, "smt": False})
    fc_put(args.socket, "boot-source", {"kernel_image_path": str(args.kernel.resolve()), "boot_args": boot_args})
    fc_put(
        args.socket,
        "drives/rootfs",
        {
            "drive_id": "rootfs",
            "path_on_host": str(args.run_rootfs.resolve()),
            "is_root_device": True,
            "is_read_only": False,
        },
    )
    fc_put(
        args.socket,
        "network-interfaces/eth0",
        {"iface_id": "eth0", "guest_mac": "AA:FC:00:00:00:02", "host_dev_name": args.tap},
    )
    if getattr(args, "data_xfs", None):
        fc_put(args.socket, "drives/data", {
            "drive_id": "data", "path_on_host": str(args.data_xfs.resolve()),
            "is_root_device": False, "is_read_only": True,
        })
    fc_put(args.socket, "actions", {"action_type": "InstanceStart"})

    print(f"[host] waiting for SSH on root@{args.guest_ip}")
    try:
        wait_for_ssh(args.guest_ip, args.ssh_timeout, args.log, args.tap, fc)
    except Exception:
        print(f"[host] SSH wait failed; Firecracker is still running, log={args.log}", file=sys.stderr)
        raise

    return fc


def stop_vm(args: argparse.Namespace, process: subprocess.Popen | None) -> None:
    process = process or getattr(args, "_process", None)
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    if getattr(args, "_tap_created", False):
        run(["ip", "link", "del", args.tap], check=False)
    args.socket.unlink(missing_ok=True)
    args.run_rootfs.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    fc = start_vm(args)
    ssh_command = ["ssh", *ssh_opts(), f"root@{args.guest_ip}"]
    print("[host] VM is ready")
    print(f"       firecracker pid: {fc.pid}")
    print(f"       ssh: {quote_cmd(ssh_command)}")
    print(f"       log: {args.log}")
    print(f"       socket: {args.socket}")
    print(f"       rootfs: {args.run_rootfs}")
    print(f"       tap: {args.tap}")
    print("[host] cleanup when done:")
    print(
        f"       kill {fc.pid}; ip route del {args.guest_ip}/32; "
        f"ip link del {args.tap}; rm -f {args.socket} {args.run_rootfs}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
