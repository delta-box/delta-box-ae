"""Bind newly appended Cubelet phase intervals to this run's API calls."""
from __future__ import annotations
import importlib.util
import json
from pathlib import Path
from repro.common import AE_ROOT, number

spec = importlib.util.spec_from_file_location('cube_phase_parser', AE_ROOT / 'vendor/finalbench/cube_phase_parser.py')
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)


def begin(path: Path) -> dict:
    state = path.stat()
    if not path.is_file():
        raise ValueError('Cubelet phase log must be a regular file')
    return {'path': str(path), 'device': state.st_dev, 'inode': state.st_ino, 'offset': state.st_size}


def collect(start: dict, pilot: Path, destination: Path) -> dict:
    source = Path(start['path'])
    end = source.stat()
    if (end.st_dev, end.st_ino) != (start['device'], start['inode']) or end.st_size < start['offset']:
        raise ValueError('Cubelet log rotated/truncated during measurement; retry with a stable log')
    with source.open('rb') as stream, destination.open('wb') as target:
        stream.seek(start['offset']); remaining = end.st_size - start['offset']
        while remaining:
            block = stream.read(min(1 << 20, remaining))
            if not block:
                raise ValueError('Cubelet log became incomplete during capture')
            target.write(block); remaining -= len(block)
    phases = []
    seen = set()
    for line in destination.read_text(errors='replace').splitlines():
        row = parser.parse_phase_line(line)
        if row:
            key = tuple(row.items())
            if key not in seen:
                seen.add(key); phases.append(row)
    records = []
    for event in json.loads(pilot.read_text())['iterations']:
        kind = event['kind']
        api = event['snapshot'] if kind == 'ckpt' else event['cube_steps'][0]
        if api['api_retries']:
            raise ValueError('Phase experiment requires a successful single API attempt; retries are separate evidence')
        matched = [p for p in phases if p['snapshot_id'] == event['snapshot_id']
                   and p['sandbox_id'] == api['sandbox_id']
                   and p['flow'] == ('commit_sandbox' if kind == 'ckpt' else 'rollback_sandbox')
                   and api['api_start_unix_ns'] <= p['start_unix_ns'] <= p['end_unix_ns'] <= api['api_end_unix_ns']]
        names = {p['phase'] for p in matched}
        required = {'rootfs_dump', 'memory_prepare', 'memory_dump'} if kind == 'ckpt' else parser.RS_FS | parser.RS_META_CONTROL | {'rollback_total', 'shim_update_restore'}
        if not required.issubset(names):
            raise ValueError(f'Missing Cubelet phase evidence for event {event["ev_i"]}: {required - names}')
        wall = number(event['checkpoint_wall_ms' if kind == 'ckpt' else 'restore_wall_ms'], 'API duration')
        fs = parser.sum_phase(matched, parser.CK_FS if kind == 'ckpt' else parser.RS_FS)
        process = parser.sum_phase(matched, parser.CK_PROC if kind == 'ckpt' else parser.RS_PROC)
        # Preserve the recovered paper parser's control component definition.
        phase_union = parser.union_ms(matched)
        other = (wall - phase_union if kind == 'ckpt' else
                 parser.sum_phase(matched, parser.RS_META_CONTROL) + wall - parser.sum_phase(matched, {'rollback_total'}))
        number(other, 'control component')
        records.append({'event_index': event['ev_i'], 'kind': kind, 'snapshot_id': event['snapshot_id'],
                        'sandbox_id': api['sandbox_id'], 'wall_ms': wall,
                        'filesystem_ms': number(fs, 'filesystem phases'), 'process_ms': number(process, 'process phases'),
                        'other_ms': other, 'phase_union_ms': phase_union,
                        'unclassified_ms': wall - fs - process - other, 'phase_names': sorted(names)})
    if not records:
        raise ValueError('No measured Cube phases')
    return {'protocol': 'Fresh Cubelet log intervals matched by sandbox, snapshot and API request timestamps; single attempt only',
            'log_start': start, 'log_end_bytes': end.st_size, 'events': records}
