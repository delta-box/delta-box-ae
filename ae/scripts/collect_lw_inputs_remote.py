#!/usr/bin/env python3
"""Read-only supplement for the older adaptive-checkpoint input schedules."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

base = Path('/mnt/disk2/dyp')
root = base / 'd-overlayfs/benchresults/2026-05-11_table3_swesearch'
files = [root / 'README.md'] + sorted((root / 'schedules').glob('*.jsonl'))
converter = base / 'd-overlayfs/benchmarks/trace_replay/swesearch_to_schedule.py'
if converter.is_file():
    files.append(converter)
index = []
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz') as out:
    for p in files:
        data = p.read_bytes()
        rel = p.relative_to(base).as_posix()
        member = tarfile.TarInfo(rel)
        member.size = len(data)
        member.mode = 0o644
        out.addfile(member, io.BytesIO(data))
        index.append(dict(path=rel, source=str(p), source_type='working_tree',
                          bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))
    for name, obj in [('SOURCE_FILES.json', index), ('MISSING_SOURCES.json', [])]:
        data = json.dumps(obj, indent=2).encode()
        member = tarfile.TarInfo(name)
        member.size = len(data)
        member.mode = 0o644
        out.addfile(member, io.BytesIO(data))
print(json.dumps({'files': len(index)}), file=sys.stderr)
