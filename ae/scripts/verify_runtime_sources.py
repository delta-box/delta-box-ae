#!/usr/bin/env python3
"""Verify the shipped, adapted vendor source bytes (not the original source hashes)."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[2]
lock = root / 'ae/vendor/adapted-sha256.json'
rows = json.loads(lock.read_text())
for record in rows:
    relative = Path(record['path'])
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Unsafe source lock path')
    path = root / relative
    raw = path.read_bytes()
    if len(raw) != record['bytes'] or hashlib.sha256(raw).hexdigest() != record['sha256']:
        raise ValueError(f'Adapted source hash mismatch: {relative}')
print(f'Verified {len(rows)} adapted vendor source files')
