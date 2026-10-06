# table-02: Checkpoint/restore comparison

## Original DeltaBox archive source

We apologize for the packaging error: the earlier public bundle selected a later
DeltaBox batch from a reused canonical directory. Its twelve inputs and event
counts matched the original cohort, but its run IDs and measurements differed.
Default archived Table 2 analysis now selects the original twelve records bound
by [paper-source-lock.json](paper-source-lock.json), preserving their bytes.

From the repository root:

```sh
python3 ae/scripts/paper_data.py import
python3 ae/scripts/verify_table2_paper_source.py
python3 -m ae.repro.analysis --output ae/results/archive-analysis
```

The import command verifies the existing main bundle and the committed
`ae/datasets/deltabox-table2-paper-source.tar.gz` supplement. The supplemental
[index](paper-source-bundle.json) materializes twelve separate content-addressed
objects under `data/records/deltabox-paper/results/`. Default archive analysis
requires every original run ID and raw-content hash to match the fixed lock; it
fails clearly if the supplement is absent or replaced. It computes event-weighted
means over 317 checkpoint and 334 restore events; no latency target is used for
selection. The standalone verifier prints each family's and the overall means.

The later batch remains unchanged under `data/records/deltabox-fast/results/`.
The main bundle and all original `files.jsonl` manifests are unchanged. Table 3,
Figure 7, the other archived experiments, and fresh experiment selection keep
their existing inputs. The new supplement is registered separately and does not
replace any shared canonical data path.

This correction restores the source of the published numbers; it is not a fresh
performance measurement. Historical restore timing is the internal critical
interval (`restore_wall_ms == restore_critical_ms`), not full external API latency.
The recorded Git HEAD does not capture the complete deployed source. Historical
success flags also do not establish that every worker action succeeded: Django
14672 event 14 contains a test command returning rc=4. The original records and
these limitations are retained. Detailed evidence and remaining deviations are
in the consolidated ledger at
`/mnt/disk2/dyp/deltabox-runtime/ae/report/README.md` on the AE machine.

## Backend cohorts

Each backend retains its own cohort. DeltaBox/Cube use 12 inputs; E2B uses a
different 8. FC/CRIU/Replay use their existing larger pools.

| Cohort | Input rows | Distinct instances |
|---|---:|---:|
| [deltabox](cohort-deltabox.csv) | 12 | 12 |
| [cube](cohort-cube.csv) | 12 | 12 |
| [e2b](cohort-e2b.csv) | 8 | 8 |
| [fc-diff](cohort-fc-diff.csv) | 238 | 238 |
| [criu-attempts](cohort-criu-attempts.csv) | 244 | 244 |
| [replay](cohort-replay.csv) | 244 | 244 |

`data/` is locally materialized and excluded from Git. Existing published files
remain listed in `files.jsonl`; the original Table 2 records are listed in
`paper-source-bundle.json`. To verify both bundles and all materialized data:

```sh
python3 ae/scripts/paper_data.py verify
```

The original twelve gzip record members are unchanged from the historical source.
The supplement includes allowlisted run conditions, source hashes, and a standalone
recomputation script. In the main bundle, saved JSON API-key fields are cleared;
`source_sha256` identifies the unchanged local original and `sha256` the published
copy. Prompts, actions and measurements are unchanged.
