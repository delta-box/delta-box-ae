"""mock_llm_server.py — HTTP server that replays trajectory.json LLM completions.

Wire compatibility: OpenAI Chat Completions API. moatless's litellm client
configured with `model_base_url=http://<this_server>/v1` reaches this server
exactly as it reached the original vLLM endpoint.

Replay design (per user-confirmed decisions 2026-05-25/26):
  - **Sequence order**: trajectories are pre-flattened by `trajectory_index.py`
    in ascending `response.created` order. We serve `sequence[cursor]` on each
    request, advancing cursor monotonically.
  - **Message policy**: audit (default) records message differences in bounded
    memory and returns exactly sequence[cursor].response. Strict closes the
    connection on a difference. Neither policy searches for another response.
    Comparison preserves canonical JSON semantics; diagnostic hashes and
    differences are rendered only by the explicit post-measurement flush.
  - **Latency policy**: recorded (default) compensates sleep by `dur_s - elapsed`
    so NPD sees the recorded LLM RTT. Explicit zero returns the same recorded
    completion without injected sleep, as required by the Replay+copy baseline.
  - **Single-inflight**: server is single-threaded, kernel-queues new
    connections while one is being served. Prevents racing on cursor.
  - **No upstream**: this server never calls a real LLM. If trajectory is not
    loaded or cursor overruns recording, requests are refused (socket close).

Control API (newline-delimited JSON over the same HTTP server):

  POST /admin/load
    body: {"instance_id": "django__django-12184", "variant": "ms"}
    resp: {"ok": true, "n_completions": 31, "purpose_mix": {...}}

  POST /admin/rewind
    body: {"cursor": 7}    # set cursor; used at MCTS restore events
    resp: {"ok": true, "cursor": 7}

  GET  /admin/stats
    resp: {"ok": true, "instance_id": "...", "variant": "...",
           "cursor": 5, "total": 31, "n_served": 5, "n_mismatch": 0}

  POST /admin/audit/flush
    body: {}  # driver calls after measurement, before stopping the server
    resp: {"ok": true, "schema_version": 1, "message_policy": "audit",
           "stats": {...}, "records": [...], "buffer": {...}, "flush_id": 1}
    Exports structured differences and drains the pending buffer. No automatic
    disk writes occur; the driver saves this response outside its timing window.

  POST /admin/reset
    resp: {"ok": true}     # clear loaded trajectory

CLI:
  python -m benchmarks.finalbench.mock_llm_server \
    --unix-socket /tmp/finalbench_mock_llm.sock \
    --traces-root ~/d-overlayfs/traces/swe-search
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import NamedTuple, Optional

from protocol import canonical_messages_hash
from trajectory_index import Completion, load_trajectory


log = logging.getLogger("mock_llm_server")


def _json_equal(left, right) -> bool:
    """Compare JSON values as canonical_messages_hash does, without encoding.

    Python's ordinary equality conflates true/1/1.0 and signed float zero.
    json.loads also accepts NaN/Infinity, so preserve their encoding semantics.
    Object keys are strings because both inputs originate from JSON.
    """
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_equal(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, float):
        if math.isnan(left):
            return math.isnan(right)
        if left == right == 0:
            return math.copysign(1, left) == math.copysign(1, right)
    return left == right


class AuditEvent(NamedTuple):
    kind: str
    cursor: int
    completion: Optional[Completion]
    request_n_msg: Optional[int]
    body: Optional[bytes]
    body_bytes: int
    protocol_error: Optional[str]


def _message_differences(actual: list, expected: list) -> list[dict]:
    """Formatting is intentionally deferred until /admin/audit/flush."""
    differences = []
    for index in range(max(len(actual), len(expected))):
        if index >= len(actual):
            differences.append({'index': index, 'kind': 'missing_request_message',
                                'expected': expected[index]})
        elif index >= len(expected):
            differences.append({'index': index, 'kind': 'extra_request_message',
                                'request': actual[index]})
        elif not _json_equal(actual[index], expected[index]):
            differences.append({'index': index, 'kind': 'changed_message',
                                'request': actual[index], 'expected': expected[index]})
    return differences


def _resolve_trajectory_path(traces_root: Path, instance_id: str, variant: str) -> Path:
    """Map (instance_id, variant) → trajectory.json path.

    Mirrors the manifest line format `<instance_id>__<variant>` used by
    `finalbench_manifest.txt`.
    """
    if variant == "ms":
        return traces_root / "qwen3-coder-30b-ms" / instance_id / "trajectory.json"
    if variant == "p-eagle-ms":
        return (traces_root / "qwen3-coder-30b-p-eagle-ms" / "mcts-iter30"
                / instance_id / "trajectory.json")
    raise ValueError(f"unknown variant: {variant!r} (expected 'ms' or 'p-eagle-ms')")


class ServerState:
    """Mutable, single-instance, lock-protected session state."""

    def __init__(self, traces_root: Path, *, message_policy: Optional[str] = None,
                 latency_policy: Optional[str] = None,
                 audit_max_records: Optional[int] = None,
                 audit_max_bytes: Optional[int] = None):
        self.traces_root = traces_root
        self.lock = threading.Lock()
        self.message_policy = os.environ.get('MOCK_MESSAGE_POLICY', 'audit') if message_policy is None else message_policy
        if self.message_policy not in ('audit', 'strict'):
            raise ValueError('MOCK_MESSAGE_POLICY must be audit or strict')
        self.latency_policy = os.environ.get('MOCK_LATENCY_POLICY', 'recorded') if latency_policy is None else latency_policy
        if self.latency_policy not in ('recorded', 'zero'):
            raise ValueError('MOCK_LATENCY_POLICY must be recorded or zero')
        self.audit_max_records = int(os.environ.get('MOCK_AUDIT_MAX_RECORDS', '256')) if audit_max_records is None else audit_max_records
        self.audit_max_bytes = int(os.environ.get('MOCK_AUDIT_MAX_BYTES', str(16 * 1024 * 1024))) if audit_max_bytes is None else audit_max_bytes
        if self.audit_max_records < 0 or self.audit_max_bytes < 0:
            raise ValueError('audit buffer limits must be nonnegative')
        self._clear_audit()
        # Cleared by reset / overwritten by load
        self.instance_id: Optional[str] = None
        self.variant: Optional[str] = None
        self.sequence: list[Completion] = []
        self.cursor: int = 0
        self.n_served: int = 0
        self.n_mismatch: int = 0
        self.n_protocol_errors: int = 0
        self.sleep_wall_s: float = 0.0

    def _clear_audit(self) -> None:
        self._audit_events: list[AuditEvent] = []
        self._audit_payload_bytes = 0
        self._audit_pending = 0
        self._audit_dropped_pending = 0
        self._audit_omitted_pending = 0
        self.audit_records_dropped = 0
        self.audit_payloads_omitted = 0
        self._flush_id = 0

    def _require_flushed(self) -> None:
        # Includes dropped records and protocol errors even when capacity is 0.
        if self._audit_pending:
            raise ValueError('unflushed audit events; call /admin/audit/flush first')

    def record_event(self, kind: str, body: bytes, *, completion=None,
                     request_n_msg=None, protocol_error=None) -> None:
        """Only bounded retention and counters; no hashes, log formatting or I/O."""
        self._audit_pending += 1
        if len(self._audit_events) >= self.audit_max_records:
            self.audit_records_dropped += 1
            self._audit_dropped_pending += 1
            return
        size = len(body)
        if size > self.audit_max_bytes - self._audit_payload_bytes:
            retained = None
            self.audit_payloads_omitted += 1
            self._audit_omitted_pending += 1
        else:
            retained = body
            self._audit_payload_bytes += size
        self._audit_events.append(AuditEvent(kind, self.cursor, completion,
                                           request_n_msg, retained, size, protocol_error))

    def flush_audit(self) -> dict:
        """Render/export pending evidence explicitly outside measurement."""
        records = []
        for event in self._audit_events:
            record = {'kind': event.kind, 'cursor': event.cursor,
                      'request_n_msg': event.request_n_msg,
                      'request_body_bytes': event.body_bytes,
                      'request_payload_omitted': event.body is None,
                      'protocol_error': event.protocol_error}
            comp = event.completion
            if comp is not None:
                record.update(purpose=comp.purpose, node_id=comp.node_id,
                              expected_n_msg=len(comp.input), expected_hash=comp.input_hash)
            if event.body is not None:
                try:
                    request = json.loads(event.body)
                except (ValueError, UnicodeError):
                    record['malformed_request_body'] = event.body.decode('utf-8', errors='replace')
                else:
                    messages = request.get('messages') if isinstance(request, dict) else None
                    record['request_messages'] = messages
                    if isinstance(messages, list):
                        record['request_hash'] = canonical_messages_hash(messages)
                        if comp is not None:
                            record['differences'] = _message_differences(messages, comp.input)
            records.append(record)
        buffer = {'max_records': self.audit_max_records, 'max_request_bytes': self.audit_max_bytes,
                  'events_since_previous_flush': self._audit_pending,
                  'records_exported': len(records), 'request_bytes_exported': self._audit_payload_bytes,
                  'records_dropped_since_previous_flush': self._audit_dropped_pending,
                  'payloads_omitted_since_previous_flush': self._audit_omitted_pending}
        self._audit_events = []
        self._audit_payload_bytes = 0
        self._audit_pending = 0
        self._audit_dropped_pending = 0
        self._audit_omitted_pending = 0
        self._flush_id += 1
        return {'ok': True, 'schema_version': 1, 'message_policy': self.message_policy,
                'latency_policy': self.latency_policy, 'stats': self.stats(), 'records': records, 'buffer': buffer, 'flush_id': self._flush_id}

    def load(self, instance_id: str, variant: str) -> dict:
        self._require_flushed()
        p = _resolve_trajectory_path(self.traces_root, instance_id, variant)
        if not p.exists():
            raise FileNotFoundError(f"trajectory.json not found: {p}")
        seq = load_trajectory(p)
        self.instance_id = instance_id
        self.variant = variant
        self.sequence = seq
        self.cursor = 0
        self.n_served = 0
        self.n_mismatch = 0
        self.n_protocol_errors = 0
        self.sleep_wall_s = 0.0
        self._clear_audit()
        from collections import Counter
        mix = Counter(c.purpose for c in seq)
        return {"ok": True, "n_completions": len(seq), "purpose_mix": dict(mix)}

    def rewind(self, cursor: int) -> dict:
        error = None
        if type(cursor) is not int:
            error = 'rewind_cursor_must_be_integer'
        elif not self.sequence:
            error = 'rewind_without_trajectory'
        elif cursor < 0 or cursor > len(self.sequence):
            error = 'rewind_cursor_out_of_range'
        if error:
            self.n_protocol_errors += 1
            self.record_event('protocol_error', b'', protocol_error=error)
            return {'ok': False, 'error': error}
        self.cursor = cursor
        return {"ok": True, "cursor": cursor}

    def stats(self) -> dict:
        return {
            "ok": True,
            "instance_id": self.instance_id,
            "variant": self.variant,
            "cursor": self.cursor,
            "total": len(self.sequence),
            "n_served": self.n_served,
            "n_mismatch": self.n_mismatch,
            "n_protocol_errors": self.n_protocol_errors,
            "message_policy": self.message_policy,
            "latency_policy": self.latency_policy,
            "audit_events_pending": self._audit_pending,
            "audit_records_pending": len(self._audit_events),
            "audit_payload_bytes": self._audit_payload_bytes,
            "audit_records_dropped": self.audit_records_dropped,
            "audit_payloads_omitted": self.audit_payloads_omitted,
            "sleep_wall_s": self.sleep_wall_s,
        }

    def reset(self) -> dict:
        self._require_flushed()
        self.instance_id = None
        self.variant = None
        self.sequence = []
        self.cursor = 0
        self.n_served = 0
        self.n_mismatch = 0
        self.n_protocol_errors = 0
        self.sleep_wall_s = 0.0
        self._clear_audit()
        return {"ok": True}


class MockHandler(BaseHTTPRequestHandler):
    """HTTP handler. State is on `self.server.state` (ServerState)."""

    # Even successful HTTP access logs would add print/format/I/O to timings.
    def log_message(self, fmt: str, *args) -> None:
        pass

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length > 0 else b""

    def _send_json(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------------- routing ----------------

    def do_POST(self) -> None:
        if self.path == "/v1/chat/completions":
            self._handle_chat_completions()
        elif self.path == "/admin/load":
            self._handle_admin_load()
        elif self.path == "/admin/rewind":
            self._handle_admin_rewind()
        elif self.path == "/admin/reset":
            self._handle_admin_reset()
        elif self.path == "/admin/audit/flush":
            self._handle_admin_audit_flush()
        else:
            self._send_json(404, {"ok": False, "error": f"unknown path {self.path}"})

    def do_GET(self) -> None:
        if self.path == "/admin/stats":
            self._handle_admin_stats()
        elif self.path == "/admin/healthz":
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"ok": False, "error": f"unknown path {self.path}"})

    # ---------------- handlers ----------------

    def _handle_admin_load(self) -> None:
        body = b''
        try:
            body = self._read_body()
            req = json.loads(body)
            inst = req["instance_id"]
            variant = req["variant"]
        except (KeyError, ValueError, TypeError, UnicodeError) as e:
            self._record_protocol_error('malformed_admin_load', body)
            self._send_json(400, {"ok": False, "error": f"bad request: {e}"})
            return
        state: ServerState = self.server.state
        with state.lock:
            try:
                resp = state.load(inst, variant)
            except (FileNotFoundError, ValueError) as e:
                state.n_protocol_errors += 1
                state.record_event('protocol_error', body, protocol_error='admin_load_failed')
                self._send_json(404, {"ok": False, "error": str(e)})
                return
        self._send_json(200, resp)

    def _handle_admin_rewind(self) -> None:
        body = b''
        try:
            body = self._read_body()
            req = json.loads(body)
            cursor = req["cursor"]
        except (KeyError, ValueError, TypeError, UnicodeError) as e:
            self._record_protocol_error('malformed_admin_rewind', body)
            self._send_json(400, {"ok": False, "error": f"bad request: {e}"})
            return
        state: ServerState = self.server.state
        with state.lock:
            resp = state.rewind(cursor)
        code = 200 if resp.get("ok") else 400
        self._send_json(code, resp)

    def _handle_admin_stats(self) -> None:
        state: ServerState = self.server.state
        with state.lock:
            resp = state.stats()
        self._send_json(200, resp)

    def _record_protocol_error(self, code: str, body: bytes = b'') -> None:
        state = self.server.state
        with state.lock:
            state.n_protocol_errors += 1
            state.record_event('protocol_error', body, protocol_error=code)

    def _handle_admin_reset(self) -> None:
        state: ServerState = self.server.state
        with state.lock:
            try:
                resp = state.reset()
            except ValueError as error:
                state.n_protocol_errors += 1
                state.record_event('protocol_error', b'', protocol_error='admin_reset_unflushed')
                self._send_json(409, {'ok': False, 'error': str(error)})
                return
        self._send_json(200, resp)

    def _handle_admin_audit_flush(self) -> None:
        state: ServerState = self.server.state
        with state.lock:
            resp = state.flush_audit()
        self._send_json(200, resp)

    def _handle_chat_completions(self) -> None:
        t0 = time.perf_counter()
        state: ServerState = self.server.state
        body = b''
        # Parse request
        try:
            body = self._read_body()
            req = json.loads(body)
            if not isinstance(req, dict):
                raise ValueError('request must be an object')
            messages = req["messages"]
            if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
                raise ValueError('messages must be an array of objects')
        except (KeyError, ValueError, TypeError, UnicodeError):
            # Bad request — close hard to surface client bugs immediately
            with state.lock:
                state.n_protocol_errors += 1
                state.record_event('protocol_error', body, protocol_error='malformed_chat_request')
            self.close_connection = True
            return

        with state.lock:
            # No trajectory loaded → strict-fail
            if not state.sequence:
                state.n_protocol_errors += 1
                state.record_event('protocol_error', body, request_n_msg=len(messages),
                                   protocol_error='no_trajectory_loaded')
                self.close_connection = True
                return
            # Cursor overrun → strict-fail
            if state.cursor < 0 or state.cursor >= len(state.sequence):
                state.n_protocol_errors += 1
                state.record_event('protocol_error', body, request_n_msg=len(messages),
                                   protocol_error='cursor_out_of_range')
                self.close_connection = True
                return
            comp = state.sequence[state.cursor]
            # Same canonical JSON semantics; hashes are diagnostic flush work.
            if not _json_equal(messages, comp.input):
                state.n_mismatch += 1
                state.record_event('message_mismatch', body, completion=comp,
                                   request_n_msg=len(messages))
                if state.message_policy == 'strict':
                    self.close_connection = True
                    return
            # Audit differences still consume the same fixed local cursor.
            state.cursor += 1
            state.n_served += 1
            response_obj = comp.response
            recorded_dur_s = comp.dur_s

        # Zero policy never calls sleep, including for long recorded completions.
        if state.latency_policy == 'recorded' and recorded_dur_s > 0:
            elapsed = time.perf_counter() - t0
            remaining = recorded_dur_s - elapsed
            if remaining > 0:
                sleep_start = time.perf_counter()
                time.sleep(remaining)
                with state.lock:
                    state.sleep_wall_s += time.perf_counter() - sleep_start

        self._send_json(200, response_obj)


# ---------------- server ----------------

class _QuietServerErrors:
    def handle_error(self, request, client_address):
        # socketserver's default prints a traceback in the request window.
        state = self.state
        with state.lock:
            state.n_protocol_errors += 1
            state.record_event('protocol_error', b'',
                               protocol_error=type(sys.exc_info()[1]).__name__)


class UnixHTTPServer(_QuietServerErrors, socketserver.UnixStreamServer):
    """Unix-socket HTTP server. Single-threaded by design (per-conn handler
    runs to completion before next accept) — this gives us cursor safety
    without an extra mutex around the serve loop itself.
    """
    # BaseHTTPRequestHandler expects this attribute
    allow_reuse_address = True

    def __init__(self, sock_path: str, state: ServerState):
        # Remove stale socket
        try:
            os.unlink(sock_path)
        except FileNotFoundError:
            pass
        super().__init__(sock_path, MockHandler)
        os.chmod(sock_path, 0o660)
        self.state = state

    # BaseHTTPRequestHandler.handle expects a client_address tuple-like;
    # Unix socket gives empty string. Wrap to satisfy.
    def get_request(self):
        request, _ = self.socket.accept()
        # Provide a fake client_address with .__str__()
        return request, ("unix", 0)


class TCPHTTPServer(_QuietServerErrors, socketserver.TCPServer):
    """TCP HTTP server (for litellm / OpenAI client which only speaks TCP).

    Bound to 127.0.0.1 by default — this is for HOST-side use by docker /
    container / process baselines. For VM use, see vsock listener (TODO).
    """
    allow_reuse_address = True

    def __init__(self, host: str, port: int, state: ServerState):
        super().__init__((host, port), MockHandler)
        self.state = state


def _serve(srv, label: str) -> None:
    log.info("mock_llm_server listening on %s (single-threaded)", label)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--unix-socket", default=None,
                    help="Unix socket path to listen on (mutually exclusive with --tcp-port)")
    ap.add_argument("--tcp-host", default="127.0.0.1",
                    help="TCP bind host (default 127.0.0.1)")
    ap.add_argument("--tcp-port", type=int, default=None,
                    help="TCP port to listen on (default: pick one of unix/tcp)")
    ap.add_argument("--traces-root", default=str(Path.home() / "d-overlayfs"
                                                  / "traces" / "swe-search"),
                    help="Path containing qwen3-coder-30b-*/ directories")
    ap.add_argument('--message-policy', choices=('audit', 'strict'), default=None,
                    help='Message comparison policy (default MOCK_MESSAGE_POLICY or audit)')
    ap.add_argument('--latency-policy', choices=('recorded', 'zero'), default=None,
                    help='Injected LLM latency (default MOCK_LATENCY_POLICY or recorded)')
    args = ap.parse_args()

    if not args.unix_socket and not args.tcp_port:
        # default to TCP on a fixed local port for moatless's litellm path
        args.tcp_port = 9999

    traces_root = Path(args.traces_root)
    if not traces_root.exists():
        log.error("traces root not found: %s", traces_root)
        return 2
    state = ServerState(traces_root, message_policy=args.message_policy, latency_policy=args.latency_policy)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if args.unix_socket:
        srv = UnixHTTPServer(args.unix_socket, state)
        _serve(srv, f"unix:{args.unix_socket}")
        try: os.unlink(args.unix_socket)
        except FileNotFoundError: pass
    else:
        srv = TCPHTTPServer(args.tcp_host, args.tcp_port, state)
        _serve(srv, f"tcp:{args.tcp_host}:{args.tcp_port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
