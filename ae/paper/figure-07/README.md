# figure-07: MCTS end-to-end comparison

DeltaBox 12 and original E2B 8 inputs remain separate. Saved E2B values model a warm-worker path from measured components; reading the aggregate is not a new end-to-end execution.

| Cohort | Input rows | Distinct instances |
|---|---:|---:|
| [deltabox](cohort-deltabox.csv) | 12 | 12 |
| [e2b](cohort-e2b.csv) | 8 | 8 |

`data/` contains local materialized inputs, schedules and saved measurements. It is excluded from Git. Every file is listed in `files.jsonl` with its source and SHA-256.

After obtaining the separate data bundle, run `python3 scripts/paper_data.py import PATH_TO_BUNDLE` at the repository root. This reconstructs these paths. To verify: `python3 scripts/paper_data.py verify`.

Saved JSON API-key fields are cleared in the publication copy. `source_sha256` identifies the unchanged local original; `sha256` identifies the published copy. Prompts, actions and measurements are unchanged.

This directory organizes existing records. It does not claim a new system execution or completed independent reproduction. Runtime/benchmark drivers are not included in the data bundle.
