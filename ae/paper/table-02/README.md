# table-02: Checkpoint/restore comparison

Preserves each backend cohort separately. DeltaBox/Cube use 12 inputs; original E2B uses a different 8. FC/CRIU/Replay use a planned 244. Historical input versions and some paper timing values remain unresolved.

| Cohort | Input rows | Distinct instances |
|---|---:|---:|
| [deltabox](cohort-deltabox.csv) | 12 | 12 |
| [cube](cohort-cube.csv) | 12 | 12 |
| [e2b](cohort-e2b.csv) | 8 | 8 |
| [fc-diff](cohort-fc-diff.csv) | 238 | 238 |
| [criu-attempts](cohort-criu-attempts.csv) | 244 | 244 |
| [replay](cohort-replay.csv) | 244 | 244 |

`data/` contains local materialized inputs, schedules and saved measurements. It is excluded from Git. Every file is listed in `files.jsonl` with its source and SHA-256.

After obtaining the separate data bundle, run `python3 scripts/paper_data.py import PATH_TO_BUNDLE` at the repository root. This reconstructs these paths. To verify: `python3 scripts/paper_data.py verify`.

Saved JSON API-key fields are cleared in the publication copy. `source_sha256` identifies the unchanged local original; `sha256` identifies the published copy. Prompts, actions and measurements are unchanged.

This directory organizes existing records. It does not claim a new system execution or completed independent reproduction. Runtime/benchmark drivers are not included in the data bundle.
