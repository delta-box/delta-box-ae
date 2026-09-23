# table-01: Mechanism comparison

Literature comparison; locally measured DeltaBox/Cube entries refer to Table 2/3. No independent trace cohort.

| Cohort | Input rows | Distinct instances |
|---|---:|---:|
| No independent trajectory cohort | — | — |

`data/` contains local materialized inputs, schedules and saved measurements. It is excluded from Git. Every file is listed in `files.jsonl` with its source and SHA-256.

After obtaining the separate data bundle, run `python3 scripts/paper_data.py import PATH_TO_BUNDLE` at the repository root. This reconstructs these paths. To verify: `python3 scripts/paper_data.py verify`.

Saved JSON API-key fields are cleared in the publication copy. `source_sha256` identifies the unchanged local original; `sha256` identifies the published copy. Prompts, actions and measurements are unchanged.

This directory organizes existing records. It does not claim a new system execution or completed independent reproduction. Runtime/benchmark drivers are not included in the data bundle.
