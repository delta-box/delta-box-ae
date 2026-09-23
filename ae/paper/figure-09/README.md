# figure-09: Write amplification

185 pool+instance inputs represent 136 distinct instances. Each filesystem has 646 edit records, 603 applied_ok. Plot filtering remains part of the reproduction workflow.

| Cohort | Input rows | Distinct instances |
|---|---:|---:|
| [war](cohort-war.csv) | 185 | 136 |

`data/` contains local materialized inputs, schedules and saved measurements. It is excluded from Git. Every file is listed in `files.jsonl` with its source and SHA-256.

After obtaining the separate data bundle, run `python3 scripts/paper_data.py import PATH_TO_BUNDLE` at the repository root. This reconstructs these paths. To verify: `python3 scripts/paper_data.py verify`.

Saved JSON API-key fields are cleared in the publication copy. `source_sha256` identifies the unchanged local original; `sha256` identifies the published copy. Prompts, actions and measurements are unchanged.

This directory organizes existing records. It does not claim a new system execution or completed independent reproduction. Runtime/benchmark drivers are not included in the data bundle.

## New full-cohort measurements

Run `bash ae/run_figure09.sh --output ae/results/figure09-new-run` from the repository root on the configured Linux host. It uses noswap RAM storage and pinned NUMA 2 CPUs. The [6728d7c47a22 report](https://github.com/delta-box/deltabox-runtime/blob/main/ae/results/6728d7c47a22/figure09-memory/README.md) contains fresh curves, raw evidence and explicit legacy-input exclusions. These results remain distinct from the historical records described above.
