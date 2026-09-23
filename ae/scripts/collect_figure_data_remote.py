#!/usr/bin/env python3
"""Read-only collection of figure-specific measurements not in trace archives."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

BASE = Path('/mnt/disk2/dyp')
R = BASE / 'd-overlayfs'
selected, missing = set(), []
for rel in ['benchresults/2026-05-18_b1_tgpu',
            'experiments/table2_three_methods_breakdown_20260609']:
    p = R / rel
    if p.exists():
        selected.update(x for x in p.rglob('*') if x.is_file() and not x.is_symlink()
                        and x.suffix in {'.json', '.jsonl', '.csv', '.tsv', '.md'})
    else:
        missing.append(str(p))
for rel in ['benchmarks/bench_tgpu.py', 'benchmarks/bench_b1_exp_C_lora_backprop.py',
            'benchmarks/bench_exp_C_multigpu.py']:
    p = R / rel
    if p.is_file():
        selected.add(p)
    else:
        missing.append(str(p))
index = []
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz') as out:
    for p in sorted(selected):
        data = p.read_bytes()
        rel = p.relative_to(BASE).as_posix()
        item = tarfile.TarInfo(rel)
        item.size, item.mode = len(data), 0o644
        out.addfile(item, io.BytesIO(data))
        index.append(dict(path=rel, source=str(p), source_type='working_tree',
                          sha256=hashlib.sha256(data).hexdigest(), bytes=len(data)))
    for name, obj in [('SOURCE_FILES.json', index), ('MISSING_SOURCES.json', missing)]:
        data = json.dumps(obj, indent=2).encode()
        item = tarfile.TarInfo(name)
        item.size, item.mode = len(data), 0o644
        out.addfile(item, io.BytesIO(data))
print(json.dumps(dict(files=len(index), bytes=sum(x['bytes'] for x in index), missing=missing)), file=sys.stderr)
