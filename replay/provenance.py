"""Content-address current sources and images; never trust a path-only hash cache."""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

_CACHE_VERSION = 2
# Local Linux filesystems can give two writes identical nanosecond stat
# fields within one coarse clock tick. One second is deliberately conservative
# for the ext4/XFS image stores used by the runner. Do not promote a digest
# computed in that ambiguous interval into a reusable cache entry later.
_RACY_STAT_NS = 1_000_000_000


def signature(path: Path) -> dict:
    st = path.stat()
    return {"path": str(path.resolve()), "device": st.st_dev, "inode": st.st_ino,
            "size": st.st_size, "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns}


def file_digest(path: Path) -> dict:
    before = signature(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 << 20), b""):
            digest.update(chunk)
    if signature(path) != before:
        raise RuntimeError(f"file changed while hashing: {path}")
    return {**before, "sha256": digest.hexdigest()}


def cached_digest(path: Path, cache: Path | None = None) -> dict:
    """Hash all bytes; reuse only a stable digest with matching stat identity.

    Cache files are local performance hints, not independently signed evidence.
    A change of size, timestamps, inode or device requires a fresh full hash.
    A reusable digest must also have been computed after the file's timestamp
    ambiguity window. Recent files are rehashed, even if their metadata matches.
    """
    if cache is None:
        return file_digest(path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.with_suffix(cache.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = json.loads(cache.read_text()) if cache.exists() else {}
        identity = signature(path)
        key = identity["path"]
        entry = records.get(key, {})
        record = entry.get("digest", {}) if isinstance(entry, dict) else {}
        hashed_at = entry.get("hash_started_ns") if isinstance(entry, dict) else None
        if (isinstance(entry, dict) and isinstance(record, dict) and entry.get("version") == _CACHE_VERSION
                and type(hashed_at) is int
                and max(identity["mtime_ns"], identity["ctime_ns"]) + _RACY_STAT_NS < hashed_at
                and hashed_at <= time.time_ns()
                and all(record.get(k) == value for k, value in identity.items())
                and re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", "")))):
            return record
        hash_started_ns = time.time_ns()
        record = file_digest(path)
        records[key] = {"version": _CACHE_VERSION, "hash_started_ns": hash_started_ns,
                        "digest": record}
        with tempfile.NamedTemporaryFile(mode="w", dir=cache.parent, delete=False) as target:
            json.dump(records, target, indent=2)
            temporary = Path(target.name)
        os.replace(temporary, cache)
        return record


def guest_sources(repo: Path, guest_dir: Path) -> dict[str, Path]:
    """Runtime modules must come only from this checkout, never guest snapshots."""
    sources = {p.name: p for p in sorted(guest_dir.glob("*.py"))}
    runtime = {p.name: p for p in sorted((repo / "backends/deltabox/gsd").glob("*.py"))}
    if not {"sandbox_controller.py", "template_fork.py", "namespace_launcher.py"} <= runtime.keys():
        raise RuntimeError("current checkout is missing required DeltaBox runtime sources")
    collisions = sources.keys() & runtime.keys()
    if collisions:
        raise RuntimeError(f"guest helpers shadow current runtime: {sorted(collisions)}")
    sources.update(runtime)
    pycriu = sorted((repo / "pycriu").rglob("*.py"))
    if not pycriu:
        raise RuntimeError("current checkout is missing pycriu")
    sources.update({str(p.relative_to(repo)): p for p in pycriu})
    return sources


def build_guest_archive(repo: Path, guest_dir: Path, archive: Path) -> dict:
    files = {}
    with tarfile.open(archive, "w") as bundle:
        for destination, source in sorted(guest_sources(repo, guest_dir).items()):
            data = source.read_bytes()
            files[destination] = {"source": str(source.relative_to(repo)),
                                  "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            info = tarfile.TarInfo(destination)
            info.size = len(data)
            info.mode = 0o644
            bundle.addfile(info, io.BytesIO(data))
        manifest = json.dumps(files, indent=2).encode()
        info = tarfile.TarInfo("guest_manifest.json")
        info.size = len(manifest)
        info.mode = 0o644
        bundle.addfile(info, io.BytesIO(manifest))
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    return {"git_commit": git("rev-parse", "HEAD"),
            "tracked_worktree_dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
            "files": files, "archive": file_digest(archive)}
