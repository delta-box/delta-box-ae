# Replay Table 2 reproduction

Run from the primary checkout on spr4numa:

    bash ae/run_all.sh --experiment table-02-replay --config ae/configs/spr4numa-table2-replay.json

The configuration selects all 244 paper portable trajectories (6,606 restore events),
16 concurrent trace workers, NUMA node 3 (CPUs 72–95), maximum requested CPU P-state,
and per-worker private noswap tmpfs. The source checkout stays at its canonical path;
only measurement work, cloned repositories and staged indexes occupy RAM. Results are
archived after measurement. CPU policy and achieved frequency are recorded.

For each restore, measure repository removal/copy and the replay subprocess including
startup, index loading and action reexecution. Serve the recorded completion responses
with their recorded RTT. The paper's zero-LLM view subtracts the sum of Completion.dur_s
through the mock's served cursor. Keep raw restore_ms, mock_completion_wait_ms,
mock_sleep_wall_ms and restore_zero_llm_ms. The scheduler's measured sleep is diagnostic,
not the paper's subtraction operand.

The selected trajectories and adjacent ms_trace.jsonl are hash-bound in files.jsonl and
the data bundle. All 244 historical restore event sequences and all 6,606 historical
served-cursor RTT sums match these inputs. The previous cohort used another trace
version with 6,670 restores. Portable trajectories omit repository.commit; the explicit
cohort commit binds the current recorded base commit, verified against the available
source repository HEAD. It is not independent proof of the historical repository tree.

Historical execution used NUMA0, 16 workers and approximately 2.101 GHz. This requested
rerun uses NUMA3, maximum P-state and RAM storage. Compare complete, event-weighted
family and All aggregates against the paper; inspect remaining differences at the
same instance/event before attributing them to hardware. Direct zero-delay mock runs
are a separate experimental configuration and do not replace paper RTT accounting.
