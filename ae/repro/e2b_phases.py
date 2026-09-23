"""Disjoint, clipped elapsed time attribution of the opt-in E2B OTel probe.

Spans in different categories may overlap. Such time is reported once as
``overlapping_phases``; uninstrumented time (including upload) stays unclassified.
These are implementation spans, not the paper's historical fixed proportions.
"""
import math

CATEGORIES = ('filesystem', 'process', 'guest_readiness', 'control_plane',
              'overlapping_phases', 'unclassified_api')
NAMES = {
    'pause': {'process-rootfs': 'filesystem', 'process-memory': 'process',
              'pause-fc': 'process', 'create-snapshot-fc': 'process'},
    'resume': {'wait-rootfs-path': 'filesystem', 'serve-memory': 'process',
               'wait-uffd-socket': 'process', 'load-snapshot': 'process',
               'resume-vm': 'process', 'sandbox-wait-for-start': 'guest_readiness',
               'get network-slot': 'control_plane', 'sandbox-create-cgroup': 'control_plane'},
    'upload': {},
}


def _window(value):
    if (not isinstance(value, list) or len(value) != 2 or
            any(type(x) is not int for x in value) or value[0] >= value[1]):
        raise ValueError('Invalid E2B phase window')
    return value


def partition(window, spans, names):
    start, end = _window(window)
    intervals = []
    for span in spans:
        a, b = _window([span['start_unix_ns'], span['end_unix_ns']])
        category = names.get(span['name'])
        a, b = max(a, start), min(b, end)
        if category and a < b:
            intervals.append((a, b, category))
    boundaries = sorted({start, end, *(x for a, b, _ in intervals for x in (a, b))})
    totals = dict.fromkeys(CATEGORIES, 0.)
    for a, b in zip(boundaries, boundaries[1:]):
        active = {category for left, right, category in intervals if left <= a and b <= right}
        category = next(iter(active)) if len(active) == 1 else ('overlapping_phases' if active else 'unclassified_api')
        totals[category] += (b-a)/1e6
    return totals


def measured_phases(row):
    windows, spans = row['phase_windows'], row['phase_spans']
    if not spans:
        raise ValueError('E2B phase probe has no completed spans')
    parts = {}
    for name, timer in (('pause', 'pause_ms'), ('upload', 'snapshot_upload_ms'), ('resume', 'resume_ms')):
        a, b = _window(windows[name])
        value = row[timer]
        if (not isinstance(value, (int, float)) or isinstance(value, bool) or
                not math.isfinite(value) or value < 0 or
                not math.isclose((b-a)/1e6, value, abs_tol=.0011)):
            raise ValueError('E2B phase window differs from API timer')
        parts[name] = partition([a, b], spans, NAMES[name])
    if windows['pause'][1] > windows['upload'][0]:
        raise ValueError('E2B pause/upload API windows overlap')
    checkpoint = {key: parts['pause'][key]+parts['upload'][key] for key in CATEGORIES}
    if not math.isclose(sum(checkpoint.values()), row['checkpoint_persist_ms'], abs_tol=.003):
        raise ValueError('E2B checkpoint phase windows do not close')
    return [('checkpoint', row['checkpoint_persist_ms'], checkpoint),
            ('restore', row['resume_ms'], parts['resume'])]


def model_components(data):
    """Controller RTT + action elapsed time includes execution LLM wait exactly once.

    Only the warm worker component model is comparable with the archived Figure
    7 protocol. Setup, command transport and controller overhead are excluded.
    """
    floor = state = 0.
    count = 0
    for iteration in data['iterations']:
        event = iteration.get('event') or {}
        if iteration['node_id'] is None:
            if iteration.get('e2b_steps'):
                raise ValueError('E2B steps without a bound node')
            continue
        llm = event['controller_llm_floor']
        before, after = llm['before_stats'], llm['after_stats']
        if (llm['protocol'] != 'served-controller-build-action-v1' or
                llm['node_id'] != iteration['node_id'] or
                before['cursor'] != llm['start_cursor'] or after['cursor'] != llm['end_cursor'] or
                llm['end_cursor']-llm['start_cursor'] != llm['served'] or
                after['n_served']-before['n_served'] != llm['served']):
            raise ValueError('E2B controller LLM floor identity mismatch')
        actions, steps = event['action_events'], iteration['e2b_steps']
        if event['n_worker_actions'] != len(actions) or len(actions) != len(steps):
            raise ValueError('E2B action/step population mismatch')
        values = [llm['recorded_ms']]
        for action, step in zip(actions, steps):
            if action['node_id'] != iteration['node_id'] or step.get('ok') is not True:
                raise ValueError('E2B action identity or success mismatch')
            values.append(action['action_wall_ms'])
            for key in ('resume_ms', 'checkpoint_persist_ms'):
                value = step[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError('Invalid E2B state timer')
                state += value
            count += 1
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values):
            raise ValueError('Invalid E2B LLM/action floor')
        floor += sum(values)
    if floor <= 0 or not count:
        raise ValueError('No positive E2B component population')
    return floor, state
