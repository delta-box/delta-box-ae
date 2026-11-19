import hashlib
import json
import math
from pathlib import Path


def audit_trace(directory, expected):
    directory = Path(directory)
    run = json.loads((directory / 'run.json').read_text())
    assert run['status'] == 'ok', run.get('error')
    result = directory / run['result']['path']
    assert hashlib.sha256(result.read_bytes()).hexdigest() == run['result']['sha256']
    data = json.loads(result.read_text())
    assert data['ok'] and data['instance'] == expected['instance']
    assert data['vm_config'] == {'mem_mib': 8192, 'vcpus': 4}
    assert data['snapshot_mode'] == 'diff'
    ck, rs = data['ckpts'], data['restore_events']
    assert len(ck) == expected['old_max_steps'] + 1
    assert [x['selected_node_id'] for x in rs] == expected['restore_nodes']
    allowed = set(expected['checkpoint_nodes'])
    chosen = [x for x in ck if x['state']['node_id'] in allowed]
    assert len(chosen) == len(allowed)
    assert {x['state']['node_id'] for x in chosen} == allowed
    for x in ck:
        assert math.isclose(x['checkpoint_total_ms'],
                            (x['checkpoint_end_ns']-x['checkpoint_start_ns'])/1e6,
                            abs_tol=1e-9)
        assert math.isfinite(x['checkpoint_total_ms']) and x['checkpoint_total_ms'] > 0
    for x in rs:
        assert x['timing_method'] == 'continuous-perf-counter-ns'
        assert x['restored_state']['ok']
        assert x['merge']['method'] in ['ficlone-official-v1.6.0-fsync', 'direct-base-no-diffs']
        if x['merge']['n_diffs'] == 0:
            assert 'merge' not in x['continuous_intervals']
        assert x['restored_state']['node_id'] == x['selected_node_id']
        assert math.isclose(x['restore_total_ms'],
                            (x['restore_end_ns']-x['restore_start_ns'])/1e6,
                            abs_tol=1e-9)
        assert math.isclose(x['restore_ready_ms']+x['post_ready_ms'],
                            x['restore_total_ms'], abs_tol=1e-6)
        assert x['restore_start_ns'] <= x['restore_ready_end_ns'] <= x['restore_end_ns']
        previous = x['restore_start_ns']
        for v in x['continuous_intervals'].values():
            assert previous <= v['start_ns'] <= v['end_ns'] <= x['restore_end_ns']
            assert math.isclose(v['ms'], (v['end_ns']-v['start_ns'])/1e6, abs_tol=1e-9)
            previous = v['end_ns']
        assert x['unattributed_ms'] >= -1e-6
        assert math.isfinite(x['restore_total_ms']) and x['restore_total_ms'] > 0
        assert x['load']['fc_spawn_ms'] >= 0 and x['load']['fc_kill_inside_load_ms'] >= 0
    return {'instance': expected['instance'], 'group': expected['group'],
            'path': str(result), 'sha256': run['result']['sha256'],
            'raw_ck': len(ck), 'aligned_ck': len(chosen), 'rs': len(rs),
            'dropped_nodes': [x['state']['node_id'] for x in ck if x not in chosen],
            'checkpoints': chosen, 'restores': rs}
