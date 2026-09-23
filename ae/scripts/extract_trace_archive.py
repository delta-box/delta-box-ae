#!/usr/bin/env python3
"""Extract the original zstd bundle without executing its contents.

Verify the archive checksum first. Reject links and path traversal, and record
per-file checksums while streaming. Extracted files are local research inputs.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--index", type=Path, required=True)
    args = parser.parse_args()
    expected = Path(str(args.archive) + ".sha256").read_text().split()[0]
    h = hashlib.sha256()
    with args.archive.open("rb") as f:
        for block in iter(lambda: f.read(4 << 20), b""):
            h.update(block)
    if h.hexdigest() != expected:
        raise SystemExit("Archive SHA256 mismatch")
    dest = args.destination.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    args.index.parent.mkdir(parents=True, exist_ok=True)
    count = total = 0
    proc = subprocess.Popen(["zstd", "-dc", str(args.archive)], stdout=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as archive, args.index.open("w") as index:
            for member in archive:
                parts = PurePosixPath(member.name).parts
                if not parts or member.name.startswith("/") or ".." in parts:
                    raise ValueError("Unsafe archive path")
                # Strip only the original bundle's top-level directory.
                relative = Path(*parts[1:])
                target = dest / relative
                if not target.resolve().is_relative_to(dest):
                    raise ValueError("Archive path escapes destination")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ValueError("Unsupported archive member type: " + member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".import-partial")
                digest = hashlib.sha256()
                written = 0
                with archive.extractfile(member) as src, temporary.open("wb") as out:
                    for block in iter(lambda: src.read(1 << 20), b""):
                        out.write(block)
                        digest.update(block)
                        written += len(block)
                if written != member.size:
                    raise ValueError("Incomplete member: " + member.name)
                if target.exists():
                    existing = hashlib.sha256()
                    with target.open("rb") as old:
                        for block in iter(lambda: old.read(1 << 20), b""):
                            existing.update(block)
                    if existing.hexdigest() != digest.hexdigest():
                        temporary.unlink()
                        raise ValueError("Refusing to overwrite different data: " + str(target))
                    temporary.unlink()
                else:
                    os.replace(temporary, target)
                index.write(json.dumps({"archive_member": member.name,
                                        "relative_path": relative.as_posix(),
                                        "bytes": written, "sha256": digest.hexdigest()},
                                       ensure_ascii=False) + "\n")
                count += 1
                total += written
                if count % 5000 == 0:
                    print(json.dumps({"files": count, "bytes": total}), flush=True)
        if proc.wait() != 0:
            raise RuntimeError("zstd decompression failed")
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait()
    print(json.dumps({"files": count, "bytes": total,
                      "archive_sha256": expected, "destination": str(dest)}), flush=True)


if __name__ == "__main__":
    main()
