#!/usr/bin/env python3
"""Runnable entry point for the whole-root sandbox (runc-model, coupled FS +
process checkpoint/restore).

Boots a worker inside a pivot_root'd full-root overlay (read-only base image
lower + tmpfs upper), then drives a real workload through DeltaBox's coupled
checkpoint/restore:

    apt install jq  ->  use jq  ->  CHECKPOINT  ->  apt install tree
                    ->  RESTORE  ->  jq survives & runs (cross-checkpoint exec),
                                     tree (post-checkpoint) rolled back,
                                     worker in-memory state resumed.

This is the capability behind "the agent can apt-install/modify anywhere under /
and the change rolls back with the MCTS checkpoint, while the process state is
restored too". The same RootOverlaySandbox primitives (launch / exec /
checkpoint / restore) are what a search driver calls per node.

Run as root. It re-execs itself inside a private mount namespace so all mounts /
pivot_root / criu operations are contained and cannot affect the host.

Usage:
    python3 run_root_sandbox.py --base-dev /dev/vdc            # ro base image block dev
    python3 run_root_sandbox.py --base-dir /path/to/ro/rootfs  # or an existing ro dir
"""
import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backends.deltabox.gsd.root_sandbox import RootOverlaySandbox


def reexec_in_mount_ns():
    if os.environ.get("_RS_IN_NS") == "1":
        return
    os.environ["_RS_IN_NS"] = "1"
    argv = " ".join(subprocess.list2cmdline([a]) for a in sys.argv)
    os.execvp("unshare", [
        "unshare", "-m", "--propagation", "private", "bash", "-c",
        f"mount --make-rprivate /; exec {sys.executable} {argv}",
    ])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-dev", help="read-only base-image block device (e.g. /dev/vdc)")
    ap.add_argument("--base-dir", help="read-only base rootfs directory to use as overlay lower")
    ap.add_argument("--layers-root", default="/dev/shm/dbroot")
    ap.add_argument("--pkg1", default="jq")
    ap.add_argument("--pkg1-use", default="jq -n '1+2+3'")
    ap.add_argument("--pkg1-expect", default="6")
    ap.add_argument("--pkg2", default="tree")
    args = ap.parse_args()

    reexec_in_mount_ns()
    P = {"pass": 0, "fail": 0}

    def chk(cond, name):
        P["pass" if cond else "fail"] += 1
        print(f"  {'PASS' if cond else 'FAIL'} {name}", flush=True)

    shutil.rmtree(args.layers_root, ignore_errors=True)
    os.makedirs(args.layers_root, exist_ok=True)

    base_dir = args.base_dir
    if args.base_dev:
        base_dir = os.path.join(args.layers_root, "baseimg")
        os.makedirs(base_dir, exist_ok=True)
        r = subprocess.run(f"mount -t xfs -o ro,nouuid {args.base_dev} {base_dir}",
                           shell=True, capture_output=True, text=True)
        if r.returncode != 0:
            r = subprocess.run(f"mount -o ro {args.base_dev} {base_dir}",
                               shell=True, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"ERROR mounting base dev: {r.stderr}"); return 1
    if not base_dir:
        print("ERROR: provide --base-dev or --base-dir"); return 1

    sb = RootOverlaySandbox(base_image=base_dir, layers_root=args.layers_root)
    sb.mount()
    print(f"[run] root overlay mounted (lower={base_dir})", flush=True)
    wp = sb.launch()
    print(f"[run] worker pid={wp}", flush=True)

    APT_UPD = "apt-get -o Acquire::AllowInsecureRepositories=true update"
    APT_INS = "apt-get install -y --allow-unauthenticated --no-install-recommends"

    def installed(p):
        return "install ok installed" in sb.exec(f"dpkg -s {p} 2>/dev/null | grep '^Status:'", timeout=30)

    sb.exec(APT_UPD, timeout=200)
    sb.exec(f"{APT_INS} {args.pkg1}", timeout=200)
    chk(installed(args.pkg1), f"apt install {args.pkg1}")
    out = sb.exec(args.pkg1_use, timeout=30)
    chk(args.pkg1_expect in out, f"{args.pkg1} runs ({args.pkg1_use} -> {args.pkg1_expect})")

    v_dump = sb.heartbeat()
    sb.checkpoint("node0")
    print(f"[run] CHECKPOINT node0 (worker counter~{v_dump})", flush=True)

    sb.exec(f"{APT_INS} {args.pkg2}", timeout=200)
    chk(installed(args.pkg2), f"apt install {args.pkg2} after checkpoint")

    if sb.worker_pid:
        subprocess.run(f"kill -9 {sb.worker_pid}", shell=True)
    import time
    time.sleep(1)
    sb.restore("node0")
    time.sleep(1)
    v_r1 = sb.heartbeat()
    time.sleep(1.2)
    v_r2 = sb.heartbeat()
    print(f"[run] RESTORE node0 (counter {v_dump} -> {v_r1} -> {v_r2})", flush=True)

    chk(installed(args.pkg1), f"{args.pkg1} SURVIVED rollback")
    out = sb.exec(args.pkg1_use, timeout=30)
    chk(args.pkg1_expect in out, f"{args.pkg1} STILL RUNS after restore (cross-checkpoint exec)")
    chk(not installed(args.pkg2), f"{args.pkg2} ROLLED BACK (pkg + dpkg gone)")
    try:
        mem_ok = v_r1 is not None and v_r2 is not None and v_r2 > v_r1 and v_r1 >= (v_dump or 0)
    except Exception:
        mem_ok = False
    chk(mem_ok, f"worker memory resumed ({v_dump} -> {v_r1} -> {v_r2})")

    sb.teardown()
    print(f"[run] RESULT {'OK' if P['fail'] == 0 else 'FAILED'} pass={P['pass']} fail={P['fail']}", flush=True)
    return 0 if P["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
