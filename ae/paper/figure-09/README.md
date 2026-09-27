# Figure 9: write amplification

The runnable cohort is the fixed [80-input manifest](cohort-80.json). Every selected trajectory retains all its recorded edit actions and runs on each of ext4, XFS, and XFS reflink: **80 inputs × 3 filesystems = 240 jobs**, with 462 requested edit records per filesystem. There is no 185-input execution mode. The generic infrastructure-check limit can only take a prefix of this fixed 80-input cohort; it never restores the historical scope.

| Model / search strategy | Selected inputs |
|---|---:|
| Claude / linear | 32 |
| Claude / MCTS | 16 |
| MiMo / linear | 26 |
| MiMo / MCTS | 6 |

Selection covers all 10 projects and all 27 nonempty model/search/project groups in the source cohort. It retains all 19 trajectories with more than 10 edits (up to 41), all six MiMo/MCTS trajectories, and the first 26 source inputs that already had completed arms when the user stopped the previous run. Those 26 inputs account for 77 completed arms: 25 complete triples and two arms of the next input. Their historical results retain their original source identity; selecting them does not by itself authorize reuse without provenance checks.

Remaining slots were filled within the fixed pool quotas using underrepresented project, edit-count bin, and patch-byte bin, with a deterministic SHA256 tie break. The manifest records each selection reason, input SHA256, complete action-file SHA256, source CSV hash, and immutable key-list hash. No measured amplification or latency was used to select inputs. Patch bytes describe input text, not actual written bytes. This is an explicitly selected cohort, not a random sample or a claim that its distribution equals the historical cohort.

Run `bash ae/run_all.sh --experiment figure-09` from the public repository root on the hosted machine. The standard entry applies the configured NUMA and frequency policy. The standalone `bash ae/run_figure09.sh --output ae/results/figure09-new-run` also uses the same fixed 80 inputs and its own documented environment options. Each measured edit retains the historical base-file mapping and first-hunk-to-EOF suffix-write protocol. Known out-of-bounds legacy diffs remain explicit excluded-input observations, never zero-valued writes.

## Historical source records

The original [185-row cohort](cohort-war.csv) remains evidence: 185 pool/instance trajectories, 136 distinct instances, and 646 edit records per filesystem. Historical records include 603 `applied_ok` edits per filesystem. These counts describe the original data, not the new 80-input execution scope or a newly completed measurement.

`data/` contains materialized inputs and historical measurements and is excluded from Git. `files.jsonl` records source paths and SHA256 values. Import the separate data bundle with `python3 scripts/paper_data.py import PATH_TO_BUNDLE`, then use `python3 scripts/paper_data.py verify`.

Saved JSON API-key fields are cleared in the publication copy. The original and published input hashes remain separate. Prompts and edit actions are not shortened by this selection. The historical [6728d7c47a22 report](https://github.com/delta-box/deltabox-runtime/blob/main/ae/results/6728d7c47a22/figure09-memory/README.md) is retained under its own source identity; it is not a result for the fixed 80 cohort.
