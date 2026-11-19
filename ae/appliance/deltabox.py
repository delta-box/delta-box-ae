#!/usr/bin/env python3
"""Boot the DeltaBox Linux kernel directly on the experiment appliance."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys

KERNEL_SHA256 = "0ba5e9ee744bcb2bac3391e967cf36cfeb10fd3add067055321cb136cd5004c0"
BASE = "DeltaBox-ATC2026-v3-l1.qcow2"
LAYER = "deltabox-direct.qcow2"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["check", "fanout", "shell"])
    parser.add_argument("--images", type=Path, required=True, help="Directory holding both verified qcow2 files")
    parser.add_argument("--kernel", type=Path, default=Path("linux/vmlinux"))
    parser.add_argument("--log", type=Path, required=True, help="New serial log file")
    args = parser.parse_args()
    layer = (args.images / LAYER).resolve(strict=True)
    base = (args.images / BASE).resolve(strict=True)
    kernel = args.kernel.resolve(strict=True)
    if hashlib.sha256(kernel.read_bytes()).hexdigest() != KERNEL_SHA256:
        parser.error("Use the DeltaBox kernel supplied in linux/vmlinux")
    chain = json.loads(subprocess.check_output(["qemu-img", "info", "--backing-chain", "--output=json", layer]))
    if len(chain) != 2 or Path(chain[0]["full-backing-filename"]).resolve() != base:
        parser.error("The direct layer must reference the verified v3 image in the same directory")
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        parser.error("Read/write access to /dev/kvm is required")
    if args.mode == "shell" and not sys.stdin.isatty():
        parser.error("The shell requires an interactive terminal")
    cmd = ["qemu-system-x86_64", "-machine", "microvm,accel=kvm,acpi=off", "-cpu", "host",
           "-m", "8192M", "-smp", "4", "-kernel", str(kernel), "-append",
           "console=ttyS0 loglevel=3 root=/dev/vda1 ro init=/usr/local/lib/deltabox-direct/init panic=-1 direct.mode=" + args.mode,
           "-blockdev", json.dumps({"driver":"qcow2", "node-name":"appliance", "read-only":True,
               "file":{"driver":"file", "filename":str(layer)}}),
           "-device", "virtio-blk-device,drive=appliance", "-nodefaults", "-no-reboot", "-display", "none",
           "-monitor", "none", "-serial", "stdio", "-nic", "none"]
    def deadline(signum, frame):
        raise TimeoutError("Guest did not report completion within 5 minutes")
    with args.log.open("xb") as log:
        process = subprocess.Popen(cmd, stdin=None if args.mode == "shell" else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            if args.mode != "shell":
                signal.signal(signal.SIGALRM, deadline)
                signal.alarm(300)
            for line in process.stdout:
                log.write(line)
                log.flush()
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
                marker = re.fullmatch(rb"DELTABOX_DIRECT_EXIT=(\d+)", line.strip())
                if marker:
                    return int(marker.group(1))
            raise RuntimeError("Guest exited before reporting completion")
        finally:
            signal.alarm(0)
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    sys.exit(main())
