#!/usr/bin/env python3
"""Checkpoint-external CodeIndex sidecar for slim DeltaBox workers.

The sidecar loads the real moatless CodeIndex and serves the small query surface
used by the SWE-search action set. It is intentionally outside the checkpointed
worker process; returned values are the real pydantic objects serialized over a
local JSON RPC endpoint, not mocked search results.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingTCPServer
from typing import Any


PAYLOAD = Path(os.environ.get("SPR_PAYLOAD", "/mnt/disk2/dyp/spr_payload"))
if str(PAYLOAD) not in sys.path:
    sys.path.insert(0, str(PAYLOAD))

log = logging.getLogger("index_sidecar")


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


class IndexService:
    def __init__(self, instance: str, repo_path: str, index_store_dir: str) -> None:
        from moatless.index import CodeIndex
        from moatless.repository.file import FileRepository

        self.repo = FileRepository(repo_path=repo_path)
        self.index = CodeIndex.from_index_name(
            instance,
            file_repo=self.repo,
            index_store_dir=index_store_dir,
        )
        log.info("loaded CodeIndex instance=%s repo=%s", instance, repo_path)

    def call(self, method: str, kwargs: dict[str, Any]) -> Any:
        if method not in {
            "semantic_search",
            "find_class",
            "find_function",
            "find_test_files",
        }:
            raise ValueError(f"unsupported index method: {method}")
        return _jsonable(getattr(self.index, method)(**kwargs))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"ok": False, "error": f"unknown path {self.path}"})

    def do_POST(self) -> None:
        if self.path != "/call":
            self._send(404, {"ok": False, "error": f"unknown path {self.path}"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length))
            method = str(req["method"])
            kwargs = req.get("kwargs") or {}
            result = self.server.service.call(method, kwargs)
            self._send(200, {"ok": True, "result": result})
        except Exception as e:
            log.exception("index RPC failed")
            self._send(500, {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
            })


class Server(ThreadingTCPServer):
    allow_reuse_address = True

    def __init__(self, addr, service: IndexService):
        super().__init__(addr, Handler)
        self.service = service


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", required=True)
    ap.add_argument("--repo-path", required=True)
    ap.add_argument("--index-store-dir", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    service = IndexService(args.instance, args.repo_path, args.index_store_dir)
    srv = Server((args.host, args.port), service)
    log.info("listening on http://%s:%d", args.host, args.port)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
