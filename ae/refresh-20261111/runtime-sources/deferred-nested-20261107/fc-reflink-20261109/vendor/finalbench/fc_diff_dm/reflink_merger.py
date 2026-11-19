"""Private CoW memory reconstruction; no mutation of checkpoint ancestors."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

EDITOR = Path(__file__).with_name('snapshot-editor-v1.6.0-x86_64')
FICLONE = 0x40049409


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(8 * 2**20), b''):
            h.update(block)
    return h.hexdigest()


def merge(base, diffs, out):
    base, diffs, out = Path(base), list(map(Path, diffs)), Path(out)
    # Empty chains are handled by the caller without making an output.
    assert diffs, 'empty chain must use the immutable Full checkpoint directly'
    assert all(p.stat().st_dev == base.stat().st_dev for p in [out.parent, *diffs])
    validate = os.environ.get('FC_MERGE_VALIDATE') == '1'
    before = {str(p): sha(p) for p in [base, *diffs]} if validate else None
    start = time.perf_counter_ns()
    with base.open('rb') as src, out.open('xb') as dst:
        # Failure is intentional: never silently fall back to a physical copy.
        fcntl.ioctl(dst.fileno(), FICLONE, src.fileno())
    clone_end = time.perf_counter_ns()
    per_diff = []
    for path in diffs:
        begin = time.perf_counter_ns()
        subprocess.run([str(EDITOR), 'edit-memory', 'rebase',
                        '--memory-path', str(out), '--diff-path', str(path)],
                       check=True, capture_output=True)
        per_diff.append((time.perf_counter_ns() - begin) / 1e6)
    apply_end = time.perf_counter_ns()
    with out.open('rb+') as f:
        os.fsync(f.fileno())
    end = time.perf_counter_ns()
    result = dict(method='ficlone-official-v1.6.0-fsync', n_diffs=len(diffs),
                  merge_ms=(end-start)/1e6, base_copy_ms=(clone_end-start)/1e6,
                  base_clone_ms=(clone_end-start)/1e6,
                  diff_apply_ms=(end-clone_end)/1e6,
                  apply_without_fsync_ms=(apply_end-clone_end)/1e6,
                  fsync_ms=(end-apply_end)/1e6, per_diff_ms=per_diff,
                  out_size_bytes=out.stat().st_size)
    if validate:
        # Validation-only run: deliberately excluded in its entirety from Table 2.
        # Force the reference to materialize the base even on a reflink filesystem.
        from reference_merger import _copy_data_regions
        reference = out.with_suffix('.reference')
        subprocess.run(['cp', '--reflink=never', '--sparse=always', str(base), str(reference)], check=True)
        with reference.open('rb+') as f:
            for path in diffs:
                _copy_data_regions(str(path), f.fileno())
            os.fsync(f.fileno())
        actual, expected = sha(out), sha(reference)
        unchanged = {str(p): sha(p) for p in [base, *diffs]} == before
        proof = dict(output=str(out), depth=len(diffs), sha256=actual,
                     reference_sha256=expected, sources_unchanged=unchanged,
                     sources=before, ok=actual == expected and unchanged)
        with (out.parent/'merge-validation.jsonl').open('a') as f:
            f.write(json.dumps(proof)+'\n')
        # A second copy outside the snapshot directory survives normal teardown.
        with Path(os.environ['FC_VALIDATION_LOG']).open('a') as f:
            f.write(json.dumps(proof)+'\n')
        assert proof['ok'], 'CoW/reference or source immutability mismatch'
        reference.unlink()
        result['validation'] = proof
    return result
