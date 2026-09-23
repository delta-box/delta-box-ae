# AE validation and measurement boundaries

## Figure 8: automatic remote execution

Validated on 2026-09-23 from public repository commit `4855272a87d6` on spr4numa:

```bash
AE_PYTHON=/mnt/disk2/dyp/deltabox-runtime/.venv/bin/python \
  bash ae/run_all.sh --group figure-08
```

The existing analysis Python environment was reused through `AE_PYTHON`; experiment source came from `/mnt/disk2/dyp/delta-box-ae`. Self-hosted users can instead prepare `.venv` with `ae/requirements-analysis.txt`. The checked-in spr4numa configuration uses existing host images, baseline services, the deployed kernel, and credentials loaded in memory from `/etc/deltabox-ae/environment.json`; it is not a portable environment installer.

Exit code: **0**. Preparation, input verification, all three CPU systems, analysis, plotting and both comparison pages passed. On allinai2plus only physical GPUs 0 and 3 were admitted: GPU coverage was **6/8**, with training B16/B64 omitted because four idle GPUs were unavailable. Figure 8(c) was explicitly unavailable because it requires all eight fresh GPU cases. No historical values filled those gaps.

### Measured CPU results

One run per point, all N=1/4/16/64 child-content checks passed. The metric is ready end-to-end time, including inherited-content verification, in milliseconds.

| Backend | N1 / N4 / N16 / N64 (ms) |
| --- | --- |
| cube | 1935.035 / 1685.586 / 2331.224 / 5376.438 |
| deltabox | 11.833 / 14.003 / 53.845 / 270.632 |
| e2b | 1641.327 / 2791.740 / 10600.699 / 40488.843 |

The configured kernel is `/mnt/disk2/dyp/d-overlayfs/linux-6.8/vmlinux`, replacing a nonexistent deployment path. Cube source creation may wait up to 60 seconds for capacity before the measured clone; this run admitted all four source instances on their first attempt. This does not prove asynchronous cleanup caused earlier capacity failures. E2B N64 is measured in batches of at most 16; the historical paper N64 used an extrapolated value. These are not identical timing protocols.

Compared with the previously recovered paper inputs, DeltaBox N1/N4, Cube N1/N4/N16/N64 and E2B N16/N64 differ by more than 50%. Historical resource, implementation and timing differences remain uncontrolled; the entrypoint repair does not explain those performance differences. This single run establishes functional coverage, not statistical reproduction or a controlled performance regression result.

### Measured GPU results

Qwen2.5-7B-Instruct on H20; generation uses vLLM, training uses LoRA. These six cases each use one GPU. Generation has three measured repetitions, training five; medians are seconds.

| Case | Repetitions | Median (s) |
| --- | --- | --- |
| generation-B1 | 3 | 1.684027 |
| generation-B4 | 3 | 1.819586 |
| generation-B16 | 3 | 1.937910 |
| generation-B64 | 3 | 3.528693 |
| training-B1 | 5 | 0.287304 |
| training-B4 | 5 | 1.049035 |

Hardware, software and explicit generation seed differ from historical measurements; no same-environment attribution is claimed. The remote environment's top-level package versions are checked by `ae/requirements-figure08-allinai2plus.txt`; that file is not a complete transitive dependency lock.

### Evidence and software checks

Raw evidence is retained on spr4numa at `/mnt/disk2/dyp/delta-box-ae/ae/results/4855272a87d6/full/` (ignored runtime outputs, not files shipped in Git): `result.md`, `review.json`, CPU `runs/`, GPU `gpu/attempt-001/manifest.json`, source/config hashes, per-case raw results, SSH logs, and `comparison/attempt-001/README{,-zh}.md`.

204 focused regression tests passed, covering entrypoints, remote admission, source/result integrity, timing protocols, occupation calculations, comparison pages, hosted launching and cleanup. The public `--test` interface and legacy `--smoke` alias both avoid SSH. Shell syntax and diff whitespace checks passed. Complete eight-case and all-busy behavior have automated coverage; this public-checkout end-to-end run exercised the six-case path.

Local executable source identity SHA-256: `d39cf48f823b725c65aea82e01e2d038d679e6c0832d3698a5d3c4ded5a95503` (the local executable source identity recorded by this run).

Future Figure 8 measurements and deviations should update this page while retaining their original raw evidence and distinct source identities.
