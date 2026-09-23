"""Post-measurement validation of recorded-response replay diagnostics."""
from __future__ import annotations


def message_policy(value):
    if value not in ('audit', 'strict'):
        raise ValueError(f'Unknown replay message policy: {value!r}')
    return value


def counter(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f'Invalid replay audit counter {name}: {value!r}')
    return value


def validate_stats(stats, policy):
    """A prompt difference is diagnostic; a broken protocol is never success."""
    message_policy(policy)
    if stats.get('message_policy', 'strict') != policy:
        raise ValueError('Replay message policy differs from requested configuration')
    mismatches = counter(stats.get('n_mismatch'), 'n_mismatch')
    errors = counter(stats.get('n_protocol_errors', 0), 'n_protocol_errors')
    if errors:
        raise ValueError(f'Replay protocol failed ({errors} errors)')
    if mismatches and policy == 'strict':
        raise ValueError(f'Strict replay message mismatch ({mismatches} requests)')
    return mismatches


def summarize(reports, policy):
    """Consume explicit flush reports only after drivers have stopped timing."""
    total = dict(n_mismatch=0, n_protocol_errors=0,
                 audit_records_dropped=0, audit_payloads_omitted=0)
    for report in reports:
        if report.get('ok') is not True or report.get('schema_version') != 1 or report.get('audit_error'):
            raise ValueError('Replay audit export failed or has an unsupported schema')
        if report.get('message_policy') != policy:
            raise ValueError('Replay audit export policy mismatch')
        stats = report['stats']
        validate_stats(stats, policy)
        for name in total:
            total[name] += counter(stats.get(name, 0), name)
    return dict(message_policy=policy, reports=len(reports), **total,
                message_equivalence='different' if total['n_mismatch'] else 'exact',
                timing='diagnostic serialization and export after measurement',
                workload='local recorded responses; no live LLM or inferred test execution')
