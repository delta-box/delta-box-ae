"""Persist mock audit evidence after measurement and before server shutdown.

Drivers own this boundary. In particular, Replay's parent must call flush only
after its timed replay subprocess exits; the subprocess never calls this API.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import urllib.request


def message_policy():
    policy = os.environ.get('MOCK_MESSAGE_POLICY', 'audit')
    if policy not in ('audit', 'strict'):
        raise ValueError(f'unsupported MOCK_MESSAGE_POLICY: {policy!r}')
    return policy


def stats_ok(stats):
    """Messages may differ in audit mode; cursor/protocol failures never pass."""
    if not isinstance(stats, dict) or stats.get('ok') is not True:
        return False
    if stats.get('message_policy') != message_policy():
        return False
    for field in ('cursor', 'total', 'n_mismatch', 'n_protocol_errors'):
        value = stats.get(field)
        if type(value) is not int or value < 0:
            return False
    return (stats['cursor'] <= stats['total'] and stats['n_protocol_errors'] == 0
            and (stats['message_policy'] == 'audit' or stats['n_mismatch'] == 0))


def flush_audit(base_url, output, *, primary_error=None):
    """Save full response/error; an audit failure cannot silently succeed.

    Preserve a pre-existing workload exception, while still recording any
    independent audit_error. Callers must stop the server in their own finally.
    """
    output = Path(output)
    payload = None
    error = None
    try:
        request = urllib.request.Request(base_url.rstrip('/') + '/admin/audit/flush',
                                         data=b'{}', method='POST',
                                         headers={'Content-Type': 'application/json'})
        # execute() grants the driver 30 seconds after SIGTERM. Leave room for
        # owned-server termination/reaping if an in-flight recorded RTT blocks
        # the single-threaded mock. Failed runs get explicit best-effort error
        # evidence; never extend or shorten the measured request's own sleep.
        with urllib.request.urlopen(request, timeout=5.0 if primary_error is not None else 30.0) as response:
            payload = json.loads(response.read())
        if not isinstance(payload, dict):
            raise ValueError('mock audit response is not an object')
        if (payload.get('ok') is not True or payload.get('schema_version') != 1
                or payload.get('message_policy') != message_policy()
                or not isinstance(payload.get('records'), list)
                or not isinstance(payload.get('buffer'), dict)
                or 'flush_id' not in payload):
            raise ValueError('invalid mock audit response')
        if not stats_ok(payload.get('stats')):
            raise ValueError('mock protocol/cursor/strict-message validation failed')
    except Exception as caught:
        error = caught
        if not isinstance(payload, dict):
            payload = {'ok': False, 'message_policy': message_policy()}
        payload['audit_error'] = {'type': type(caught).__name__, 'message': str(caught)}
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + '.tmp')
        temporary.write_text(json.dumps(payload, indent=2) + '\n')
        temporary.replace(output)
    except Exception as export_error:
        if primary_error is None:
            raise RuntimeError(f'mock audit export failed: {output}: {export_error}') from export_error
        # Evidence storage can be full precisely when workload execution fails.
        # Python 3.9 has no add_note; emit one post-measurement diagnostic while
        # preserving the primary exception and letting caller cleanup proceed.
        print(f'[audit_error] cannot export {output}: {type(export_error).__name__}: '
              f'{export_error}; original error retained: {primary_error}', file=sys.stderr)
        payload['audit_error'] = {'type': type(export_error).__name__, 'message': str(export_error)}
        return payload
    if error is not None and primary_error is None:
        raise RuntimeError(f'mock audit failed; see {output}: {error}') from error
    return payload
