# November 2026 data and runtime refresh

This directory records the final baseline reruns separately from the historical DeltaBox runs. Original results and failed attempts are retained in `evidence/`; `recomputed-results.json` is generated from the selected per-event records, not hand-entered plot values.

## Recompute the reported values

From the archive root:

```sh
python3 ae/appliance/verify_results.py --evidence ae/refresh-20261111/evidence --out ae/results/recomputed.json
```

The five snapshot systems use the same 12 trace IDs. Four rerun snapshot baselines recorded 360 checkpoints; the paper selection keeps the 317 checkpoint nodes present in the DeltaBox schedule and all 334 restore targets. The excluded 43 expansion checkpoints remain in the raw files. Copy+replay has 12 initial repository copies and 334 restores; its checkpoint column denotes that initial copy, not a process snapshot. The groups contain 2 Django, 2 SymPy, 5 Scientific, and 3 Tools/Small traces.

| Selected batch | Checkpoint mean (ms) | Restore mean (ms) |
|---|---:|---:|
| E2B, e2b-phase7 | 1072.008659 | 424.746271 |
| CubeSandbox, cube-balance | 839.821419 | 643.867149 |
| FC-Diff+dm, reflink merge | 647.933415 | 2567.917555 |
| CRIU+cp, online-paper batch | 567.587395 | 609.533809 |
| copy+replay | 313.150194 | 14220.584528 |
| Historical DeltaBox | 10.83 | 1.86 |

The online final PDF uses CRIU 567.6/609.5 ms. The later overlap batch has complete-operation means of 556.163283/613.194388 ms and was incorporated into the later local manuscript. Both batches are included and remain distinct. Table 3 reports **remaining wait after inference**, 91.139622 ms across 305 aligned nonterminal checkpoint windows; 12 terminal checkpoints are excluded from that metric. It is not the full checkpoint latency.

The `local-manuscript-data.json` and `manuscript-data-index.md` files are explicitly snapshots of the later local manuscript. They do not overwrite the online-paper selection above.

## Measurement and figure identities

The November baseline runs use nested KVM: a Linux L0 host runs a 48-vCPU/120-GiB L1; E2B, Cube and Firecracker create 4-vCPU/8-GiB L2 guests. Four virtual NUMA nodes are mapped to the four physical CPU pools. The original L1 qcow2 was on NVMe, with `cache=none`; the experiment host was locked near 2.1 GHz. Cube snapshot storage is XFS in an L1 loop image. These runs do not change the historical DeltaBox tmpfs measurements. The supplied image preserves the runtime and workload dependencies, while the launcher's CPU list is chosen for the receiving host.

- Figure 1: use the delivered phase logs and the selected Table 2 batches. Guest readiness is an observed interval; it is not relabeled as file or memory copying.
- Figure 7: `(LLM + action + aligned CK + RS) / (LLM + action)`, with the 317 selected checkpoints and 334 restores. It is a component-sum execution metric, not an unadjusted wall-clock run duration.
- Figure 8(a): `plot/figure8-provenance.json` identifies every selected file. Cube N=1/4/16 are four-run medians and N=64 is one successful run. E2B N=1/4 are four-run medians, N=16 is the median of three successes with one retained failure, and N=64 is the earlier selected successful acceptance run (3843.726369 ms). The later successful N=64 rerun remains separately available. These points are not uniformly four-run means. Every child reads and verifies the 64 MiB inherited state before the readiness timer ends; clients are reused in the corrected Cube path and the corrected E2B template uses 2-MiB memory blocks.
- Figure 8(b/c) use the included GPU/trainer inputs and the selected fan-out points. The underlying training records are historical, not rerun by this packaging task.
- Figure 9(a) measures actual loop-device sectors written, including data, journal and metadata. Reflink ranges from 65.1 to 95.9 KiB per edit; the earlier theoretical 4-KiB series is superseded. Figure 9(b) retains the real-edit measurement, and (c/d) retain their original datasets.
- Two integer display choices in the manuscript differ from ordinary rounding: Replay SymPy 19887.764... is displayed as 19887, and Tools/Small 10624.380... as 10625. Exact observations and computed means remain unchanged in the artifact.

`evidence/MANIFEST.json` records each source hash and distributed hash. Credentials were cleared only in distribution copies; numeric observations were retained. Historical raw objects are materialized under `ae/traces/objects` and linked from `ae/paper/*/data`, with their manifest in this directory.

## Draw the current figures

```sh
python3 -m pip install numpy matplotlib
python3 ae/appliance/draw_figures.py
```

Outputs are written below this directory in `source/` and `build-info/`. No paper source is modified. See the single experiment ledger at `ae/report/README.md` for the measured deviations and unresolved conditions.
