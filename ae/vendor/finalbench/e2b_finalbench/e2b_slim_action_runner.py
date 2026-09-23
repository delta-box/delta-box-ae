#!/usr/bin/env python3
"""Execute one recorded Moatless action inside an E2B sandbox.

This is the E2B counterpart of deltabox_std/action_subprocess.py. The sandboxed
process executes the real Moatless action against the live repo, but heavyweight
checkpoint-external services stay outside the sandbox:

  * LLM calls go to the external mock_llm_server via the slim litellm shim.
  * CodeIndex queries go to the external index_sidecar via SlimIndexProxy.

No trace, mock server, or index data is loaded inside the E2B sandbox.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path


BASE = Path(os.environ.get("E2B_FINALBENCH_BASE", "/opt/finalbench"))
PAYLOAD = Path(os.environ.get("SPR_PAYLOAD", "/opt/spr_payload"))

for p in (
    str(BASE / "slim_shims"),
    str(BASE),
    str(PAYLOAD),
    str(PAYLOAD / "moatless-det-src"),
):
    if p not in sys.path:
        sys.path.insert(0, p)


def run(req: dict) -> dict:
    from moatless.actions.action import Action
    from moatless.actions.model import ActionArguments
    from moatless.file_context import FileContext
    from moatless.repository.file import FileRepository
    from slim_index_proxy import SlimIndexProxy

    completion_model = req.get("action_model", {}).get("completion_model")
    if isinstance(completion_model, dict):
        completion_model["model_base_url"] = req["mock_base_url"].rstrip("/") + "/v1"
        completion_model["model_api_key"] = "dummy"

    repo = FileRepository(repo_path=req["repo_path"])
    code_index = SlimIndexProxy(req["index_url"])
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
    materialized: list[str] = []
    materialize_enabled = (
        os.environ.get("E2B_MATERIALIZE_FILE_CONTEXT", "0") == "1"
        or bool(req.get("materialize_file_context"))
    )
    if materialize_enabled and file_context.was_edited():
        for path in file_context.get_edited_files() + file_context.get_created_files():
            context_file = file_context.get_context_file(path)
            if context_file is None:
                continue
            repo.save_file(path, context_file.content)
            materialized.append(path)
    return {
        "ok": True,
        "observation": observation.model_dump(),
        "file_context": file_context.model_dump(),
        "event": {
            "event_type": "e2b_slim_action_execute",
            "action": action_args.name,
            "node_id": req.get("node_id"),
            "action_wall_ms": (time.perf_counter() - t0) * 1000.0,
            "helper_pid": os.getpid(),
            "materialize_file_context": materialize_enabled,
            "materialized_files": materialized,
        },
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: e2b_slim_action_runner.py <request.json> <response.json>", file=sys.stderr)
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
