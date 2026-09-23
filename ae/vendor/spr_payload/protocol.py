"""protocol.py — JSON-line protocol over a stream socket (Unix / vsock).

Wire format: each message is JSON terminated by a single '\n'. No HTTP, no
length-prefix framing — readers do `socket.makefile().readline()`.

Control ops (bench harness → mock server):
  {"op":"load","instance_id":"<inst>"}    -> {"ok":true,"n_completions":<N>}
  {"op":"reset"}                          -> {"ok":true}
  {"op":"stats"}                          -> {"ok":true,"instance_id":"...","cursor":i,"total":N}

LLM op (NPD → mock server):
  {"op":"complete","messages":[...],"temperature":<f>,"max_tokens":<n>}
  -> {"ok":true,"content":"...","usage":{...},"finish_reason":"stop","sleep_ms":<f>}

This module provides canonical message hashes and the original JSON-line
transport. The AE HTTP mock documents its independent audit/strict message
policy in mock_llm_server.py; protocol errors remain fatal in either mode.
"""
from __future__ import annotations

import hashlib
import json
import socket
from typing import Any


PROTO_VERSION = 1

# CID convention: host listens on VMADDR_CID_HOST (=2); each VM gets a unique
# guest CID assigned by the hypervisor.
DEFAULT_VSOCK_PORT = 9999
DEFAULT_UNIX_SOCKET = "/tmp/finalbench_mock_llm.sock"


def canonical_messages_hash(messages: list[dict]) -> str:
    """Stable hash over a `messages` list. Whitespace and key-order independent.

    This is the assertion key: if recorded `input` and replay `messages` agree
    byte-for-byte after canonicalisation, the recording matches.
    """
    canon = json.dumps(messages, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def send_msg(sock: socket.socket, obj: dict[str, Any]) -> None:
    """Send a JSON-line message, flushing immediately."""
    line = json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"
    sock.sendall(line)


def recv_msg(rfile) -> dict[str, Any] | None:
    """Read one JSON-line message. Returns None on EOF (connection closed)."""
    line = rfile.readline()
    if not line:
        return None
    return json.loads(line)
