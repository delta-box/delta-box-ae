"""Run on the owned XFS; bytes independently specify branch/zero/tail behavior."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

P = Path(__file__).resolve().parent
sys.path.insert(0, str(P/'vendor/finalbench/fc_diff_dm'))
from reflink_merger import merge, sha


def main():
    root = P/'snapshots/synthetic-validation'
    root.mkdir()
    size = 64*2**20 + 4096
    base = root/'base.mem'
    original = bytearray((bytes(range(256)) * ((size+255)//256))[:size])
    base.write_bytes(original)
    definitions = [
        [(0, b'Z'*4096), (8*2**20, bytes(8192)), (size-4096, b'T'*4096)],
        [(0, bytes(4096)), (16*2**20, b'B'*4096)],
        [(8*2**20, b'C'*4096), (24*2**20, b'D'*4096)],
    ]
    diffs = []
    for i, pages in enumerate(definitions):
        path = root/f'diff{i}.mem'
        with path.open('xb') as f:
            f.truncate(size)
            for offset, data in pages:
                f.seek(offset); f.write(data)
            f.flush(); os.fsync(f.fileno())
        diffs.append(path)
    proofs = []
    for j, chain in enumerate([[0], [0, 1], [0, 2], [2, 0, 1]]):
        expected = bytearray(original)
        for i in chain:
            for offset, data in definitions[i]:
                expected[offset:offset+len(data)] = data
        out = root/f'out{j}.mem'
        row = merge(base, [diffs[i] for i in chain], out)
        row.update(chain=chain, expected_sha256=hashlib.sha256(expected).hexdigest(), actual_sha256=sha(out))
        assert row['expected_sha256'] == row['actual_sha256']
        proofs.append(row)
    (P/'synthetic-validation.json').write_text(json.dumps(dict(ok=True, epoch=time.time(), cases=proofs), indent=2))
    shutil.rmtree(root)


if __name__ == '__main__':
    main()
