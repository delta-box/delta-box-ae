"""Prepare the benchmark filesystem inside a disposable VM."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def load_instance_data(dataset_path: str, instance_id: str) -> dict | None:
    path = Path(dataset_path)
    if not path.is_file():
        return None
    for row in json.loads(path.read_text()):
        if row["instance_id"] == instance_id:
            return row
    return None


def stage_testbed_from_data(instance_id: str, version: str) -> None:
    short_repo = instance_id.split("__", 1)[1].rsplit("-", 1)[0]
    source = Path("/mnt/data/testbeds") / f"{short_repo}__{version}"
    if not source.is_dir():
        raise RuntimeError(f"missing testbed: {source}")
    destination = Path("/testbed_original_data")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir()
    subprocess.run(["cp", "-a", f"{source}/.", str(destination)], check=True)


def init_testbed_overlay(sandbox_root: str) -> dict:
    workspace = Path("/overlay_workspace")
    merged = Path("/testbed")
    if os.path.ismount(merged):
        subprocess.run(["umount", str(merged)], check=True)
    paths = {name: workspace / name for name in ("upper", "work", "layers")}
    for path in paths.values():
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
    merged.mkdir(exist_ok=True)
    options = (
        f"lowerdir=/testbed_original_data,upperdir={paths['upper']},"
        f"workdir={paths['work']},index=off,metacopy=off,redirect_dir=off"
    )
    subprocess.run(["mount", "-t", "overlay", "overlay", "-o", options, str(merged)], check=True)
    return {name: str(path) for name, path in paths.items()}


def select_testbed(candidates: list[str], base_commit: str) -> str:
    matching = []
    exact = []
    for candidate in sorted(candidates):
        command = ["git", "-c", f"safe.directory={candidate}", "-C", candidate]
        if subprocess.run(command + ["cat-file", "-e", f"{base_commit}^{{commit}}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            continue
        matching.append(candidate)
        head = subprocess.run(command + ["rev-parse", "HEAD"], capture_output=True, text=True)
        if head.returncode == 0 and head.stdout.strip() == base_commit:
            exact.append(candidate)
    matches = exact or matching
    if not matches:
        raise RuntimeError(f"no testbed contains commit {base_commit}; searched {candidates}")
    if len(matches) > 1:
        print(f"[stage] {len(matches)} catalog clones contain commit {base_commit}; "
              f"using {matches[0]} and checking out the recorded commit", flush=True)
    return matches[0]


def setup_git_environment(base_commit: str, root: str = "/testbed") -> None:
    command = ["git", "-c", f"safe.directory={root}"]
    subprocess.run(command + ["checkout", "-f", base_commit], cwd=root, check=True)
    subprocess.run(command + ["clean", "-fdx"], cwd=root, check=True)
