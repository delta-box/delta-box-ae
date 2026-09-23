#!/usr/bin/env python3
"""Execute one recorded Moatless action inside an E2B sandbox.

This is intentionally similar to deltabox_std/action_subprocess.py, but it
keeps the CodeIndex inside the sandbox for the first E2B same-workload harness.
The controller, SearchTree, and mock LLM cursor stay outside the sandbox.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path


PAYLOAD = Path(os.environ.get("SPR_PAYLOAD", "/opt/spr_payload"))
for p in (str(PAYLOAD), str(PAYLOAD / "moatless-det-src")):
    if p not in sys.path:
        sys.path.insert(0, p)


def run(req: dict) -> dict:
    from moatless.actions.action import Action
    from moatless.actions.model import ActionArguments
    from moatless.file_context import FileContext
    from moatless.index import CodeIndex
    from moatless.repository.file import FileRepository

    completion_model = req.get("action_model", {}).get("completion_model")
    if isinstance(completion_model, dict):
        completion_model["model_base_url"] = os.environ.get(
            "E2B_ACTION_MOCK_BASE_URL", "http://127.0.0.1:19999/v1"
        )
        completion_model["model_api_key"] = "dummy"

    instance = req["instance"]
    repo = FileRepository(repo_path=req["repo_path"])
    code_index = CodeIndex.from_index_name(
        instance,
        file_repo=repo,
        index_store_dir=req["index_store_dir"],
    )
    file_context = FileContext.from_dict(
        repo=repo,
        runtime=None,
        data=req["file_context"],
    )
    action_args = ActionArguments.model_validate(req["action"])
    action = Action.model_validate(
        req["action_model"],
        repository=repo,
        code_index=code_index,
        runtime=None,
    )

    t0 = time.perf_counter()
    observation = action.execute(action_args, file_context=file_context, workspace=None)
    return {
        "ok": True,
        "observation": observation.model_dump(),
        "file_context": file_context.model_dump(),
        "event": {
            "event_type": "e2b_action_execute",
            "action": action_args.name,
            "node_id": req.get("node_id"),
            "action_wall_ms": (time.perf_counter() - t0) * 1000.0,
            "helper_pid": os.getpid(),
        },
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: e2b_action_runner.py <request.json> <response.json>", file=sys.stderr)
        return 2
    req_path = Path(argv[1])
    resp_path = Path(argv[2])
    try:
        req = json.loads(req_path.read_text(encoding="utf-8"))
        out = run(req)
    except BaseException as e:
        out = {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        }
    tmp = resp_path.with_suffix(resp_path.suffix + ".tmp")
    tmp.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    tmp.replace(resp_path)
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
