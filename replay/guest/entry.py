#!/usr/bin/env python3
"""Prepare a supplied data disk and execute the recorded workload."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def verify_sources(root: Path) -> None:
    expected = json.loads((root / "guest_manifest.json").read_text())
    for name, record in expected.items():
        source = root / name
        if not source.resolve().is_relative_to(root.resolve()):
            raise RuntimeError(f"invalid guest source path: {name}")
        if hashlib.sha256(source.read_bytes()).hexdigest() != record["sha256"]:
            raise RuntimeError(f"injected source hash mismatch: {name}")
    print(f"[sources] verified {len(expected)} current runtime/helper files", flush=True)


def main() -> None:
    verify_sources(Path("/app"))
    config = json.loads(Path("/tmp/table4-config.json").read_text())
    for name in list(os.environ):
        if (name.startswith(("DELTABOX_", "AGENT_", "NPD_"))
                or name in ("API_KEY", "API_BASE", "MODEL_NAME", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY")):
            del os.environ[name]
    os.environ.update(config["guest_env"])
    binary = config.get("criu_dump_binary")
    if binary is not None:
        path = Path("/app/bin/criu-pinned")
        if hashlib.sha256(path.read_bytes()).hexdigest() != binary["sha256"]:
            raise RuntimeError("injected CRIU binary hash mismatch")
    from pycriu import images  # noqa: F401; fail before replay if protobuf is missing
    required = ("python3", "git", "mount", "cp", "criu", "unshare")
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        raise RuntimeError(f"benchmark rootfs is missing tools: {', '.join(missing)}")
    data = Path("/mnt/data")
    data.mkdir(parents=True, exist_ok=True)
    if not os.path.ismount(data):
        subprocess.run(["mount", "-t", "xfs", "-o", "ro,nouuid,noatime", "/dev/vdb", str(data)], check=True)
    if not (data / "testbeds").is_dir():
        raise RuntimeError("data image must contain /testbeds/<repo>__<version>")
    if (data / "opt").is_dir() and Path("/opt").resolve() != (data / "opt").resolve():
        Path("/opt").mkdir(exist_ok=True)
        subprocess.run(["mount", "--bind", str(data / "opt"), "/opt"], check=True)
    explicit_testbed = config.get("testbed")
    if explicit_testbed:
        testbed = (data / "testbeds" / explicit_testbed).resolve()
        if not testbed.is_relative_to(data / "testbeds") or not testbed.is_dir():
            raise RuntimeError(f"invalid data-image testbed: {explicit_testbed}")
        os.environ["DELTABOX_DATA_TESTBED"] = explicit_testbed
    Path("/tmp/replay_results.jsonl").unlink(missing_ok=True)
    os.execv(sys.executable, [
        sys.executable, "-u", "/app/trace_replay_main.py",
        "--schedule", "/tmp/replay_schedule.jsonl",
        "--results", "/tmp/replay_results.jsonl",
        *config["guest_flags"],
    ])


if __name__ == "__main__":
    main()
