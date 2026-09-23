"""Replay-only observation; production controller methods are not modified."""
from __future__ import annotations

import time
import os


def timed_api_call(function, *args, **kwargs):
    started = time.perf_counter_ns()
    result = function(*args, **kwargs)
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    return result, elapsed_ms


if os.environ.get("DELTABOX_API_PROFILE") == "1":
    # Diagnostic-only: cProfile changes timing. Export after the observed API
    # interval and never use these runs as uninstrumented performance samples.
    import cProfile
    import json
    import pstats

    def timed_api_call(function, *args, **kwargs):
        profiler = cProfile.Profile()
        started = time.perf_counter_ns()
        try:
            result = profiler.runcall(function, *args, **kwargs)
        finally:
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            records = []
            for (filename, line, name), (primitive, calls, own, total, _) in pstats.Stats(profiler).stats.items():
                records.append(dict(file=filename, line=line, name=name,
                                    primitive_calls=primitive, calls=calls,
                                    self_ms=own * 1000, total_ms=total * 1000))
            with open("/tmp/api_profile.jsonl", "a") as output:
                output.write(json.dumps(dict(operation=function.__name__,
                                             elapsed_ms=elapsed_ms, records=records)) + "\n")
        return result, elapsed_ms


def settle_dumps(pending):
    """Surface even a terminal async dump failure that no restore would join."""
    rows = []
    for checkpoint, future, statistics in pending:
        try:
            future.result()
            rows.append({"kind": "dump_completion", "runtime_ckpt_id": checkpoint,
                         "ok": True, "statistics": dict(statistics)})
        except Exception as error:
            rows.append({"kind": "dump_completion", "runtime_ckpt_id": checkpoint,
                         "ok": False, "err": type(error).__name__, "msg": str(error)})
    return rows
