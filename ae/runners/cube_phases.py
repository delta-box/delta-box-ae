"""Bind newly appended Cubelet phase intervals to this run's API calls."""
from __future__ import annotations
from collections import Counter
import importlib.util
import json
from pathlib import Path
from repro.common import AE_ROOT, file_record, number

spec = importlib.util.spec_from_file_location('cube_phase_parser', AE_ROOT / 'vendor/finalbench/cube_phase_parser.py')
parser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser)


def begin(path: Path) -> dict:
    state = path.stat()
    if not path.is_file():
        raise ValueError('Cubelet phase log must be a regular file')
    return {'path': str(path), 'device': state.st_dev, 'inode': state.st_ino, 'offset': state.st_size}


def _timestamp(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f'{label}: expected nonnegative integer timestamp')
    return value


def _strict_event(event, api, matched, required):
    if event.get('ok') is not True:
        raise ValueError('Failed Cube event cannot supply successful phase evidence')
    start = _timestamp(api['api_start_unix_ns'], 'API start')
    end = _timestamp(api['api_end_unix_ns'], 'API end')
    if start >= end:
        raise ValueError('Invalid Cube API timestamp window')
    field = 'checkpoint_wall_ms' if event['kind'] == 'ckpt' else 'restore_wall_ms'
    wall = number(event[field], 'API duration')
    if number(api[field], 'nested API duration') != wall:
        raise ValueError('Cube event duration differs from its API record')
    # The monotonic API timer is inside these wall-clock timestamps. A large
    # disagreement indicates a bad time window or clock jump, not a phase cost.
    window_ms = (end - start) / 1_000_000
    if abs(window_ms - wall) > max(1.0, window_ms * 0.01):
        raise ValueError('Cube API duration disagrees with timestamp window')
    counts = Counter(p['phase'] for p in matched)
    duplicates = {name: count for name, count in counts.items() if count != 1}
    if duplicates:
        raise ValueError(f'Duplicate Cubelet phase evidence: {duplicates}')
    if set(counts) != required:
        raise ValueError(f'Unexpected Cubelet phase evidence: {set(counts) - required}')
    for phase in matched:
        raw = phase['raw_record']
        if not isinstance(raw, dict) or raw.get('success') is not True:
            raise ValueError('Cubelet phase requires explicit JSON success=true')
        pstart = _timestamp(raw.get('startUnixNs'), 'phase start')
        pend = _timestamp(raw.get('endUnixNs'), 'phase end')
        duration = number(raw.get('durationMs'), 'phase duration')
        if not start <= pstart <= pend <= end:
            raise ValueError('Cubelet phase is outside API timestamp window')
        # Instrumentation emits milliseconds rounded to three decimals.
        if abs(duration - (pend - pstart) / 1_000_000) > 0.002:
            raise ValueError('Cubelet phase duration disagrees with timestamp window')
    if event['kind'] == 'ckpt':
        ordered = sorted(matched, key=lambda p: p['start_unix_ns'])
        if [p['phase'] for p in ordered] != ['rootfs_dump', 'memory_prepare', 'memory_dump']:
            raise ValueError('Cube checkpoint phase order differs from instrumentation')
        if any(a['end_unix_ns'] > b['start_unix_ns'] for a, b in zip(ordered, ordered[1:])):
            raise ValueError('Cube checkpoint phase intervals overlap')
    else:
        total = next(p for p in matched if p['phase'] == 'rollback_total')
        children = sorted((p for p in matched if p is not total), key=lambda p: p['start_unix_ns'])
        if any(not total['start_unix_ns'] <= p['start_unix_ns'] <= p['end_unix_ns'] <= total['end_unix_ns']
               for p in children):
            raise ValueError('Cube restore phase is outside rollback_total')
        if any(a['end_unix_ns'] > b['start_unix_ns'] for a, b in zip(children, children[1:])):
            raise ValueError('Cube restore child phase intervals overlap')


def collect(start: dict, pilot: Path, destination: Path, *, strict: bool = False) -> dict:
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
    for line_number, line in enumerate(destination.read_text(errors='replace').splitlines(), 1):
        row = parser.parse_phase_line(line)
        if row:
            key = tuple(row.items())
            # Legacy Figure 1 accepted duplicate log lines. Paper reconstruction
            # retains every line and rejects duplicates instead of hiding them.
            if not strict and key in seen:
                continue
            seen.add(key)
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                raw = None
            phases.append({**row, 'success': raw.get('success') if isinstance(raw, dict) else None,
                           'raw_record': raw, 'raw_line': line, 'captured_line': line_number})
    records, raw_phases = [], []
    for event in json.loads(pilot.read_text())['iterations']:
        kind = event['kind']
        if kind not in ('ckpt', 'restore'):
            raise ValueError(f'Unknown Cube phase event kind: {kind}')
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
        if strict:
            _strict_event(event, api, matched, required)
        wall = number(event['checkpoint_wall_ms' if kind == 'ckpt' else 'restore_wall_ms'], 'API duration')
        fs = parser.sum_phase(matched, parser.CK_FS if kind == 'ckpt' else parser.RS_FS)
        process = parser.sum_phase(matched, parser.CK_PROC if kind == 'ckpt' else parser.RS_PROC)
        # Preserve the recovered paper parser's control component definition.
        phase_union = parser.union_ms(matched)
        other = (wall - phase_union if kind == 'ckpt' else
                 parser.sum_phase(matched, parser.RS_META_CONTROL) + wall - parser.sum_phase(matched, {'rollback_total'}))
        number(other, 'control component')
        indices = list(range(len(raw_phases), len(raw_phases) + len(matched)))
        raw_phases.extend(matched)
        records.append({'event_index': event['ev_i'], 'kind': kind, 'snapshot_id': event['snapshot_id'],
                        'sandbox_id': api['sandbox_id'], 'wall_ms': wall,
                        'api_start_unix_ns': api['api_start_unix_ns'], 'api_end_unix_ns': api['api_end_unix_ns'],
                        'filesystem_ms': number(fs, 'filesystem phases'), 'process_ms': number(process, 'process phases'),
                        'other_ms': other, 'phase_union_ms': phase_union,
                        'unclassified_ms': wall - fs - process - other, 'phase_names': sorted(names),
                        'phase_record_indices': indices,
                        'success_evidence': 'explicit-json' if all(p['success'] is True for p in matched) else 'unverified-legacy'})
    if not records:
        raise ValueError('No measured Cube phases')
    return {'protocol': 'Fresh Cubelet log intervals matched by sandbox, snapshot and API request timestamps; single attempt only',
            'strict_success_and_phase_contract': strict,
            'log_start': start, 'log_end_bytes': end.st_size,
            'pilot': file_record(pilot), 'captured_log': file_record(destination),
            'phase_record_count': len(raw_phases), 'raw_phases': raw_phases, 'events': records}
