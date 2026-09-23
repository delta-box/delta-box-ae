"""Compare message-check CPU cost; excludes HTTP, RTT and post-run export.

Run on the measurement host with the same affinity policy. No print or disk I/O
occurs in the timed loops. This is a diagnostic, not a paper latency sample.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ae/vendor/spr_payload'))
from mock_llm_server import ServerState, _json_equal
from protocol import canonical_messages_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--iterations', type=int, default=1000)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error('iterations must be positive')
    fixture = ROOT / 'ae/report/replay-fixes-20260921/criu-host/mock_mismatch_astropy__astropy-13033_c14.json'
    raw = fixture.read_bytes()
    case = json.loads(raw)
    expected = case['expected_messages']
    digest = canonical_messages_hash(expected)
    output = {'kind': 'diagnostic-message-check-only', 'fixture_sha256': hashlib.sha256(raw).hexdigest(),
              'iterations_per_round': args.iterations, 'rounds': 9, 'rows': []}
    for name, request in [('exact', copy.deepcopy(expected)), ('c14-difference', case['request_messages'])]:
        body = json.dumps({'messages': request}).encode()
        comp = SimpleNamespace(input=expected, input_hash=digest, purpose='build_action', node_id=13)
        costs = {'old_canonical_hash': [], 'audit_compare_and_buffer': []}
        for round_id in range(9):
            arms = list(costs)
            if round_id % 2:
                arms.reverse()
            for arm in arms:
                # Each measured request gets buffer capacity; no saturation fast path.
                state = ServerState(ROOT, audit_max_records=args.iterations,
                                    audit_max_bytes=len(body) * args.iterations)
                start = time.perf_counter_ns()
                if arm == 'old_canonical_hash':
                    for _ in range(args.iterations):
                        matched = canonical_messages_hash(request) == digest
                else:
                    for _ in range(args.iterations):
                        matched = _json_equal(request, expected)
                        if not matched:
                            state.n_mismatch += 1
                            state.record_event('message_mismatch', body, completion=comp,
                                               request_n_msg=len(request))
                elapsed = time.perf_counter_ns() - start
                costs[arm].append(elapsed / args.iterations / 1000)
                assert matched == (name == 'exact')
        output['rows'].append(dict(case=name, request_bytes=len(body),
                                   request_messages=len(request), expected_messages=len(expected),
                                   microseconds_per_request=costs,
                                   median_us={k: statistics.median(v) for k, v in costs.items()}))
    output['limitations'] = ['No HTTP parsing, recorded sleep, response encoding or post-run audit export measured.',
                            'Old arm excludes its mismatch print and disk writes; this is a CPU check comparison only.',
                            'Not a guarantee of unchanged whole-experiment latency.']
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2) + '\n')
    print(json.dumps({row['case']: row['median_us'] for row in output['rows']}, indent=2))


if __name__ == '__main__':
    main()
