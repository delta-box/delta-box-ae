# Historical synthetic-message serialization

Replay, host CRIU and FC-Diff stage a private copy of the pinned Moatless source. Only the
`CodeSpan` construction in the synthetic “Let's view the content in the updated
files” history branch receives `restore_span_order(file_path, live_span_ids)`.
Actual ViewCode actions, observations, code, file ordering, line ranges and
message ordering are unchanged. The shared payload is never edited.

The old serializer exposed Python set iteration order. The trace's
`file_context.spans` preserves source/insertion order, which differs from that
historical wire order in both first-round failures. Staging therefore extracts
an order table from the fixed trace's synthetic history messages. A key is the
exact file path and sorted span multiset, including multiplicity. Conflicting
orders for one key fail staging in strict mode. Audit mode omits ambiguous keys
from the table and records their count; the producer keeps the live order. An unknown live key preserves the live span list in audit mode and raises in strict mode; it never
selects an expected request, copies recorded contents, or consults a mock cursor.

`history-serialization.json` records the trace hash, upstream source hashes,
staged source/table hashes, and the current mock source hashes. Runtime lookups
only update scalar counters; they no longer serialize or print per-hit events.
Remaining message differences are retained by the mock and exported after the
measurement in `*mock_audit.json`. The AE default is audit, with strict matching
available through `replay_message_policy: "strict"`.

The payload helper files in `ae/vendor/spr_payload` are copied unchanged from this checkout.
`payload-source-lock.json` describes historical import sources: the existing
checkout's `mock_llm_server.py` already differs from that import hash due to its
mismatch-diagnostic changes. The adapter neither rewrites that source lock nor
claims the historical hash matches the current mock.

Verification: `python3 -m unittest tests.paper.test_history_order
tests.paper.test_cpu_contracts -v`. The two first-round mismatches and the saved
CRIU mismatch after successful restore in `fixes-baselines-002` pass the
explicit strict handler after producer-side ordering. Changed spans, paths,
duplicates, observations/code, actual actions, line ranges and message order
remain failures in strict mode and visible differences in audit mode. This is a compatibility repair, not evidence that a full
baseline has completed; affected baselines must be rerun on a new output directory.
