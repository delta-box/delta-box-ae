# DeltaBox — ATC 2026 Artifact Evaluation

**English** | [简体中文](README-zh.md)

**Badges sought: Available, Functional, Reproduced.**

DeltaBox checkpoints, restores, and branches the filesystem and process state of an agent sandbox, so that tree-search agents can explore alternatives cheaply. This artifact contains the DeltaBox runtime, recorded agent workloads, the experiment drivers, and the plotting scripts used to evaluate state-management overhead, memory use, and write amplification. CPU experiments replay recorded LLM responses, so no LLM API key is needed.

We recommend evaluating the artifact in three steps:

1. Run the [quick check](#quick-start) (about 5 minutes) to confirm that the setup works.
2. Run [all CPU experiments](#run-all) with `ae/run_all_no_gpu.sh` (about 2 hours).
3. Optionally, rerun individual experiments from the [experiment index](#experiments).

To keep the run time manageable, each experiment uses at most three inputs by default, fewer than in the paper; [`--limit N`](#run-all) raises this. Replay, CRIU, and Firecracker Diff (FC-Diff) draw their inputs from a fixed pool of 44 recorded trajectories. Figure 8(b) needs GPUs on a separate machine; if none are free, it is reported as skipped.

[Quick start](#quick-start) · [Experiment index](#experiments) · [Inspect results](#results) · [Troubleshooting](#troubleshooting) · [Self-hosting](#self-hosting) · [Hosted-machine details](#hosted-details)

## 1. Quick start

<a id="quick-start"></a>
<a id="快速检查"></a>
<a id="quick-check"></a>

### Log in and check the environment

Send us your SSH public key through the comments in the artifact submission system. Once you receive access instructions, log in and run the quick check, replacing `HOST` with the address you were given:

```bash
ssh atc-ae@HOST
cd ~/delta-box-ae
bash ae/run_test.sh
```

The AE machine already provides Linux x86-64 with KVM, the experiment images, the recorded inputs, and the baseline services, so you do not need to build anything. Run every command in this guide from the repository root (`~/delta-box-ae`). To use your own machine instead, see the [self-hosting guide](ae/docs/self-hosting.md).

The quick check starts DeltaBox, runs a short checkpoint/restore sequence, verifies the restored state, and analyzes and plots the result. It succeeded if it exits with status 0 and prints:

```text
ok: <result-directory>/SUMMARY.md
```

`SUMMARY.md` should then list every step as `ok`. The quick check only shows that the pipeline works; the experiments below test the paper's claims.

Quick-check results go to `ae/results/checks/quick-check-<timestamp>/`. Runs on the AE machine execute one at a time. CPU and memory placement come from the shared `measurement` configuration and apply to every CPU experiment in a run.

To pin a single run to a different NUMA node, set `AE_NUMA_NODE` and `AE_CPUS` to the node and its CPU list, and pass both:

```bash
bash ae/run_test.sh --numa-node "$AE_NUMA_NODE" --cpus "$AE_CPUS"
```

The CPUs must belong to that node. Memory availability checks and CPU-frequency restoration still apply.

### Run all CPU experiments

<a id="一键运行"></a>
<a id="run-all"></a>

On the AE machine, log in as `atc-ae` and run:

```bash
cd ~/delta-box-ae
bash ae/run_all_no_gpu.sh
```

No arguments are needed, and the run takes about 2 hours. It runs all 16 CPU experiments behind Tables 2 and 3, Figures 2, 6, 7, 8(a), and 9, and the filesystem correctness tests. Two fixed CPU sets share the work: CPUs 28–31 on NUMA node 1 and CPUs 48–51 on NUMA node 2. Each takes the next pending experiment from a shared queue and runs its jobs one at a time. Experiments that reconfigure the CubeSandbox or E2B services never run concurrently. The GPU experiments, Figure 8(b) and 8(c), are not included.

By default, each experiment uses up to three inputs. In experiments that replay recorded agent runs, an input is one recorded trajectory, and it always runs to the end; Figure 8(a) and the correctness tests do not replay trajectories and use fixed fan-out and test workloads instead. One input can produce several jobs: Figure 6(b) runs each input under two policies, and Figure 9 runs it on three filesystems.

The script creates a new result directory and prints its path; use `--output` to choose the directory yourself. The run succeeded if it exits with status 0 and `SUMMARY.md` in that directory lists all 16 experiments as `ok`. CPU placement is fixed and cannot be overridden.

The script tolerates occasional disruptions on the shared machine, such as temporary memory pressure or a slow baseline service. If an experiment fails, the script cleans up, verifies the cleanup, and resumes the same run, up to three times. Every failure stays in the record, and a run that needed a resume is reported as resumed, not as an uninterrupted pass. The script does not retry after an interruption such as Ctrl-C, after a cleanup it cannot verify, or if the code or configuration changed during the run. To continue a stopped run yourself, pass its directory to `--resume`; manual resumes are not retried automatically. Run `bash ae/run_all_no_gpu.sh --help` for all options.

To use more inputs, pass `--limit N` with N up to 10:

```bash
bash ae/run_all_no_gpu.sh --limit 5
```

This changes only the number of inputs; each input still runs in full, with all of its configurations. Each experiment is also capped at 10 jobs, so experiments with several configurations may use fewer than N inputs. For example, Figure 9 uses at most three inputs (nine jobs). A larger limit takes longer.

The authors may run background validation on NUMA nodes 0 and 3 (see [Hosted-machine details](#hosted-details)). This command takes priority: the background run stops and cleans up first, which can delay the start of your run by up to about 12 minutes.

### Optional: CPU and GPU experiments in sequence

`ae/run_all.sh` runs the CPU experiments one after another on a single NUMA placement, followed by the GPU experiments:

```bash
bash ae/run_all.sh --limit 3
```

To choose the placement:

```bash
bash ae/run_all.sh --limit 3 --numa-node "$AE_NUMA_NODE" --cpus "$AE_CPUS"
```

It uses the same three-input limit and input pools as above, then analyzes the results and builds the comparison pages. After the CPU experiments, it checks the GPUs reserved for this artifact on the GPU host `allinai2plus`. If no GPU is free, Figure 8(b) is recorded as skipped; with one to three free GPUs, six of its eight cases run; with four, all eight run. GPU availability does not affect the CPU results. Figure 8(c) is computed once all CPU and GPU inputs are complete; you can also [compute it manually](#figure-08-gpu).

### Input sets

Replay, CRIU, and FC-Diff draw their inputs from a fixed pool of 44 trajectories: 34 Django and 10 Astropy. Each backend keeps its own input order, and the first 10 inputs in that order are Astropy, so with a limit of 10 or less these three baselines are measured on Astropy inputs only. To draw from the original, larger pools instead (244 trajectories each for Replay and CRIU, 238 for FC-Diff), add `--baseline-inputs all`:

```bash
bash ae/run_all.sh --limit 3 --baseline-inputs all
```

This option selects the pool that inputs come from, not how many are used; the default is `44`. Use `--limit 1` for a smaller trial run. Events are never truncated unless you pass `--max-events`.

Each run records the inputs it used in every plan and in `suite.json`. Table 2 averages events over each backend's own inputs; the input sets behind each reported number are listed in the authors' ledger on the AE machine at `/mnt/disk2/dyp/deltabox-runtime/ae/report/README.md`.

**Table 2 archive correction.** We apologize for packaging a later DeltaBox batch in place of the original paper records. Run `python3 ae/scripts/paper_data.py import` to import the bundled original twelve records; default archived Table 2 analysis now verifies and uses them. The later batch remains available, and other experiments keep their existing inputs. See the [Table 2 source and verification instructions](ae/paper/table-02/README.md).

### Where results are written

When a run finishes, open `result.md` (identical to `SUMMARY.md`) for the status of each experiment and of the GPU stage. Then open **`comparison/attempt-NNN/README.md`** (English) or **`README-zh.md`** (Chinese), which compare your measurements with the paper; both are generated together and link to each other. The exact path is recorded under `outputs.comparison` in `review.json`. A failed step keeps its logs and makes the command exit with a nonzero status.

Each new run writes to its own directory under `ae/results/selected/`, and quick checks write under `ae/results/checks/`. A run refuses to write into an existing directory unless you resume it. See [Resume and troubleshoot](#troubleshooting) to continue a run.

## 2. Experiment index and individual runs

<a id="experiments"></a>
<a id="实验索引"></a>
<a id="4-论文图表--实验步骤"></a>
<a id="逐项实验"></a>
<a id="5-逐项实验"></a>
<a id="experiment-index"></a>
<a id="individual-experiments"></a>

`ae/run_all_no_gpu.sh` runs every CPU experiment below; the GPU part of Figure 8 requires `ae/run_all.sh`. To check a single claim, run its section directly; you do not need to run the whole suite first.

| Paper experiment | Evaluation question | Command option | Resources |
| --- | --- | --- | --- |
| [Table 2](#table-02) | What is each system's checkpoint/restore overhead? | `--group table-02` | CPU, KVM, baseline services |
| [Table 3](#table-03) | Which components contribute to DeltaBox overhead, and how do fast and slow restore differ? | `--group table-03` | CPU, KVM |
| [Figure 2](#figure-02) | How much state changes per step relative to total state? | `--group figure-02` | CPU |
| [Figure 6](#figure-06) | How do memory policies and lightweight-skip affect memory and latency? | `--group figure-06` | CPU, KVM |
| [Figure 7](#figure-07) | How much overhead does state management add relative to LLM and action time? | Derived from Table 2 (DeltaBox and E2B) | CPU, KVM, E2B services |
| [Figure 8(a)](#figure-08) | How does fan-out time grow with the number of branches? | `--group figure-08-cpu` | CPU, KVM, Cube/E2B services |
| [Figure 8(b)(c)](#figure-08-gpu) | How do GPU stage times affect modeled occupation? | `--group figure-08`: CPU + GPU + calculations | One and four GPUs; CPU fan-out |
| [Figure 9](#figure-09) | How does reflink affect copy-up and device writes during editing? | `--group figure-09` | CPU, KVM |
| [§6.3.3](#correctness) | Does checkpoint/restore preserve filesystem semantics? | `--experiment correctness` | CPU, KVM |

Table 1 and Figures 1 and 3–5 describe the motivation and design and have no associated experiments.

The commands below write under a common prefix. Set it once in your shell, replacing `reviewer-A` with your own identifier, and use a new identifier for each new evaluation:

```bash
export AE_RUN="$(pwd -P)/ae/results/reviewer-A"
```

Do not create the output directories yourself. The launcher applies the supplied configuration and handles CPU/NUMA pinning and frequency sampling. Run commands one at a time; every CPU experiment in a run uses that run's NUMA placement. Each driver selects its own inputs; use `--limit N` to use more or fewer, up to 10 jobs per experiment.

Each section below gives the goal, the command, the output, and how to read it. The images on the right are example outputs of the full run, shown for illustration; evaluate the comparison pages from your own run.

### 2.1 Table 2: checkpoint/restore overhead

<a id="table-02"></a>
<a id="对比table2"></a>
<a id="步骤1"></a>
<a id="基线实验"></a>
<a id="table-2-comparison"></a>
<a id="step-1"></a>
<a id="baseline-experiments"></a>

**Goal.** Measure how long DeltaBox, Replay, CRIU, FC-Diff, CubeSandbox, and E2B take to save and restore sandbox state, which is the cost of switching between agent branches.

**Run.**

```bash
bash ae/run_all.sh --group table-02 --output "$AE_RUN/table-02"
```

To run a single backend, use an option such as `--experiment table-02-e2b` with a new output directory.

**Output and interpretation.** Open `table-02-comparison.png`. Compare checkpoint and restore separately, first within each workload group and then in the `All` row (labeled `Event Avg` in the figure). `All` averages over all events, so it is not the plain mean of the four group averages. Values are in milliseconds; lower is better. The paper claims that DeltaBox has lower overhead than the baselines. When comparing systems, keep in mind how each system is configured and what its timings include.

<table>
<tr><th>Paper figure/table</th><th>Example script output</th></tr>
<tr>
<td align="center" valign="top"><a href="ae/reference/figures/table-02.png"><img src="ae/reference/figures/table-02.png" alt="Table 2: Paper figure/table" width="300"></a></td>
<!-- AE-RESULT:table-02:start -->
<td align="center" valign="top"><a href="docs/images/table-02-ae.png"><img src="docs/images/table-02-ae.png" alt="Table 2: Example script output" width="300"></a><br><small><a href="docs/images/table-02-comparison.png">Open full comparison</a></small></td>
<!-- AE-RESULT:table-02:end -->
</tr>
</table>

### 2.2 Table 3: DeltaBox component latency

<a id="table-03"></a>
<a id="对比table3"></a>
<a id="table-3-comparison"></a>

**Goal.** Break DeltaBox's checkpoint and restore time into components, and compare fast restore, which reuses a retained template, with slow restore, which rebuilds processes with CRIU.

**Run.**

```bash
bash ae/run_all.sh --group table-03 --output "$AE_RUN/table-03"
```

Both modes run on the same inputs. The [Table 3 guide](ae/docs/table3-method.md) explains runtime options such as lazy pages.

**Output and interpretation.** In `table-03-comparison.png`, look at the Overlay, fork/CRIU, and coordination components to find the dominant cost of each restore path. Fast restore is cheaper because reusing a template avoids most of the process reconstruction. The components add up to Table 3's total, but they cover narrower windows than the end-to-end API time in Table 2, so do not compare the two directly. `—` marks a component that does not apply to that path.

<table>
<tr><th>Paper figure/table</th><th>Example script output</th></tr>
<tr>
<td align="center" valign="top"><a href="ae/reference/figures/table-03.png"><img src="ae/reference/figures/table-03.png" alt="Table 3: Paper figure/table" width="300"></a></td>
<!-- AE-RESULT:table-03:start -->
<td align="center" valign="top"><a href="docs/images/table-03-ae.png"><img src="docs/images/table-03-ae.png" alt="Table 3: Example script output" width="300"></a><br><small><a href="docs/images/table-03-comparison.png">Open full comparison</a></small></td>
<!-- AE-RESULT:table-03:end -->
</tr>
</table>

### 2.3 Figure 2: state changes per step

<a id="figure-02"></a>
<a id="对比figure2"></a>
<a id="步骤3"></a>
<a id="figure-2-comparison"></a>
<a id="step-3"></a>

**Goal.** Check whether an agent action usually changes only a small fraction of the filesystem and memory state, which motivates incremental checkpoints.

**Run.**

```bash
bash ae/run_all.sh --group figure-02 --output "$AE_RUN/figure-02"
```

**Output and interpretation.** In `figure-02-comparison.png`, panel (a) compares the total state size with the change per step, and panel (b) shows the changes over the course of the search. Look at how small a typical change is relative to the total, and at the occasional large update. Filesystem and memory use different axes and units, so compare each only within its own panel.

<table>
<tr><th>Paper figure/table</th><th>Example script output</th></tr>
<tr>
<td align="center" valign="top"><a href="ae/reference/figures/figure-02.png"><img src="ae/reference/figures/figure-02.png" alt="Figure 2: Paper figure/table" width="300"></a></td>
<!-- AE-RESULT:figure-02:start -->
<td align="center" valign="top"><a href="docs/images/figure-02-ae.png"><img src="docs/images/figure-02-ae.png" alt="Figure 2: Example script output" width="300"></a><br><small><a href="docs/images/figure-02-comparison.png">Open full comparison</a></small></td>
<!-- AE-RESULT:figure-02:end -->
</tr>
</table>

### 2.4 Figure 6: memory policies and lightweight-skip

<a id="figure-06"></a>
<a id="对比figure6"></a>
<a id="步骤4"></a>
<a id="figure-6-comparison"></a>
<a id="step-4"></a>

**Goal.** Measure how memory grows as the search deepens, and check whether lightweight-skip (LW-skip) checkpoints reduce the cost of the steps that qualify for them.

**Run.**

```bash
bash ae/run_all.sh --group figure-06 --output "$AE_RUN/figure-06"
```

Panel (a) runs the `none`, `skip`, `gc`, and `warm` policies on the same SymPy workload. Panel (b) compares the standard-only and adaptive policies on the same inputs. To run one panel, use `--experiment figure-06-memory` or `--experiment figure-06-adaptive`.

**Output and interpretation.** In panel (a) of `figure-06-comparison.png`, compare the growth rate and peak of each policy to see whether LW-skip limits the memory retained during search. In panel (b), compare the latency distributions of standard and lightweight checkpoints, including the low-latency lightweight events and the standard checkpoints that the adaptive policy still takes. The horizontal axis (latency) is logarithmic, and the vertical axis counts events. Analyze the two panels separately; do not combine their policies or samples.

<table>
<tr><th>Paper figure/table</th><th>Example script output</th></tr>
<tr>
<td align="center" valign="top"><a href="ae/reference/figures/figure-06.png"><img src="ae/reference/figures/figure-06.png" alt="Figure 6: Paper figure/table" width="300"></a></td>
<!-- AE-RESULT:figure-06:start -->
<td align="center" valign="top"><a href="docs/images/figure-06-ae.png"><img src="docs/images/figure-06-ae.png" alt="Figure 6: Example script output" width="300"></a><br><small><a href="docs/images/figure-06-comparison.png">Open full comparison</a></small></td>
<!-- AE-RESULT:figure-06:end -->
</tr>
</table>

### 2.5 Figure 7: overhead relative to LLM and action time

<a id="figure-07"></a>
<a id="对比figure7"></a>
<a id="figure7映射"></a>
<a id="figure-7-comparison"></a>
<a id="figure-7-mapping"></a>

**Goal.** Measure state-management cost relative to the agent's own execution time, and check whether DeltaBox keeps the normalized time close to 1.0× across workloads.

**Run.**

```bash
bash ae/run_all.sh \
  --experiment table-02-deltabox --experiment table-02-e2b \
  --output "$AE_RUN/figure-07"
```

Figure 7 is computed from these complete trajectories and needs no separate timing run. If you have already run Table 2 or the full suite, use that output.

**Output and interpretation.** Open `figure-07-comparison.png`. The 1.0× line is the LLM and action time; anything above it is state-management overhead. Compare the Django, SymPy, Scientific, and Tools/Small groups separately, rather than judging the impact from absolute milliseconds. Each bar is a ratio of totals within its group (summed timing components), not an average of per-step ratios.

<table>
<tr><th>Paper figure/table</th><th>Example script output</th></tr>
<tr>
<td align="center" valign="top"><a href="ae/reference/figures/figure-07.png"><img src="ae/reference/figures/figure-07.png" alt="Figure 7: Paper figure/table" width="300"></a></td>
<!-- AE-RESULT:figure-07:start -->
<td align="center" valign="top"><a href="docs/images/figure-07-ae.png"><img src="docs/images/figure-07-ae.png" alt="Figure 7: Example script output" width="300"></a><br><small><a href="docs/images/figure-07-comparison.png">Open full comparison</a></small></td>
<!-- AE-RESULT:figure-07:end -->
</tr>
</table>

### 2.6 Figure 8(a): CPU fan-out

<a id="figure-08"></a>
<a id="对比figure8"></a>
<a id="步骤5"></a>
<a id="figure-8-comparison"></a>
<a id="step-5"></a>

**Goal.** Measure how long it takes to create usable branches from one frozen state, and how this cost grows with the number of branches.

**Run.**

```bash
bash ae/run_all.sh --group figure-08-cpu --output "$AE_RUN/figure-08-cpu"
```

DeltaBox and E2B create N = 1, 4, 16, and 64 branches; CubeSandbox creates N = 1 and 16. Every child reads back the state it inherited, but the check differs by system: DeltaBox compares every page with its expected value and CubeSandbox compares the memory checksum with its expected value. E2B checks the exact token, allocation byte count, and expected sum of the first byte in each 4096-byte page. For the 64 MiB source, the expected byte count is 67,108,864 and the checksum is 2,041,721. Each child's expected and observed values are retained in `memory_validation`. This verifies the deterministic page-touch pattern, rather than every byte in the allocation. Before this correction, the released E2B verifier checked only whether checksum and byte-count fields were present and did not retain their numeric values.

**Output and interpretation.** In `figure-08-cpu-comparison.png`, compare the time until all branches are ready at the same N, and how each curve grows with N. The paper argues that cheap branch creation enables a wider search. Passing its system's check is part of a branch's success.

<table>
<tr><th>Paper figure/table</th><th>Example script output</th></tr>
<tr>
<td align="center" valign="top"><a href="ae/reference/figures/figure-08.png"><img src="ae/reference/figures/figure-08.png" alt="Figure 8: Paper figure/table" width="300"></a></td>
<!-- AE-RESULT:figure-08:start -->
<td align="center" valign="top"><a href="docs/images/figure-08-cpu-ae.png"><img src="docs/images/figure-08-cpu-ae.png" alt="Figure 8: Example script output" width="300"></a><br><small><a href="docs/images/figure-08-cpu-comparison.png">Open full comparison</a></small></td>
<!-- AE-RESULT:figure-08:end -->
</tr>
</table>

### 2.7 Figure 8(b)(c): GPU latency and modeled occupation

<a id="figure-08-gpu"></a>
<a id="figure-8b-gpu"></a>
<a id="获得-gpu-后的命令"></a>

**Goal.** Measure LLM generation and training time on GPUs, and combine it with sandbox time to compute the GPU occupation and policy staleness modeled in the paper.

`ae/run_all.sh` and `--group figure-08` run panel (b) automatically over SSH, using the [remote configuration](ae/configs/figure08-remote.json). The [Figure 8 guide](ae/paper/figure-08/README.md) describes the setup, how GPUs are selected, and what happens when only some cases can run. Quick checks and analysis-only runs never start GPU work. To run only the GPU stage:

```bash
bash ae/run_all_gpu.sh
```

When the command finishes, it prints a GPU-only summary with the status, GPU count, repetitions and mean time for each of the eight cases, then the Figure 8(c) result, followed by the exact `SUMMARY.md` path. The same summary is saved as `result.md`; you do not need to locate the result directory yourself.

The GPU-only entry uses the GPUs reserved for this artifact on `allinai2plus` and creates a new result directory automatically. All eight cases need four idle GPUs: generation and training at batch 1/4 use one GPU; training at batch 16/64 uses four. Use `--output PATH` to choose the result directory. `--gpu-devices ID,...` selects a different set of GPUs; use it only if the authors assign you different GPUs. Busy devices are excluded, and the script never selects GPUs outside the specified set. Insufficient capacity is reported as partial or unavailable.

After panel (b), the same command produces panel (c). As in the paper, panel (c) applies the paper's Equation 1 to these GPU times and the fan-out times of a CPU run. The command takes the fan-out times from your newest finished `ae/run_all_no_gpu.sh` run under `ae/results` and names that run in the summary. The plot is written to `gpu/attempt-001/comparison/theory/plots/figure-08c.png` in the GPU result directory. CubeSandbox is measured at N = 1 and 16, so its N = 64 point is marked as not measured. If no CPU run has finished yet, panel (c) is reported as unavailable; run `ae/run_all_no_gpu.sh` first.

To rerun every panel of Figure 8:

```bash
bash ae/run_all.sh --group figure-08 --output "$AE_RUN/figure-08"
```

The authors maintain the remote model and Python environments. The script checks only the GPUs reserved for this artifact (the `devices` list in the remote configuration) and records busy or unavailable GPUs as skipped. If fewer GPUs are free, the run produces a result that is marked as partial; the parameters of each case stay the same. See the [GPU setup guide](ae/docs/self-hosting.md#gpu-setup).

**Output and interpretation.** Panel (b) produces per-repeat generation and training times and `gpu/attempt-NNN/plots/figure-08b.png`; compare the same stage at the same batch size. Once both the CPU fan-out and the GPU measurements have succeeded, panel (c) produces `gpu/attempt-NNN/comparison/theory/occupation.json` and its plots. Panel (c) follows the paper's Equation 1, the same method as the paper's Figure 8(c): check whether shorter sandbox time raises the expected GPU occupation and lowers staleness. Both panels appear in the run's English and Chinese comparison pages.

<table>
<tr><th>Paper figure/table</th><th>Example script output</th></tr>
<tr>
<td align="center" valign="top"><a href="ae/reference/figures/figure-08.png"><img src="ae/reference/figures/figure-08.png" alt="Figure 8: Paper figure/table" width="300"></a></td>
<!-- AE-RESULT:figure-08-gpu:start -->
<td align="center" valign="top"><a href="docs/images/figure-08b.png"><img src="docs/images/figure-08b.png" alt="Figure 8(b): Example script output" width="300"></a><br><small><a href="docs/images/figure-08b-comparison.png">Open full comparison</a></small></td>
<!-- AE-RESULT:figure-08-gpu:end -->
</tr>
</table>

### 2.8 Figure 9: write amplification

<a id="figure-09"></a>
<a id="对比figure9"></a>
<a id="步骤6"></a>
<a id="figure-9-comparison"></a>
<a id="step-6"></a>

**Goal.** Measure copy-up data and device writes when editing files on ext4, XFS, and XFS with reflink, and check whether shared data blocks reduce write amplification.

This artifact runs a fixed [set of 80 trajectories](ae/paper/figure-09/cohort-80.json), selected from the original [185-trajectory cohort](ae/paper/figure-09/cohort-war.csv); the selection covers four model/search combinations, ten projects, and long edit sequences, and is not a random sample. Each trajectory runs on all three filesystems, for 240 jobs in total; with the default limit, three trajectories (nine jobs) run. The 185-trajectory cohort is kept as evidence and cannot be rerun. `--baseline-inputs all` affects only the three Table 2 baselines, not Figure 9.

**Run.**

```bash
bash ae/run_all.sh --group figure-09 --output "$AE_RUN/figure-09"
```

A RAM-backed variant with its own entry point and configuration is described in the [self-hosting guide](ae/docs/self-hosting.md#specialized-runs).

To continue an interrupted Figure 9 run in a new directory, reuse its verified jobs and run only the missing ones:

```bash
bash ae/run_all.sh --group figure-09 \
  --reuse-completed-from "$AE_RUN/figure-09-old" \
  --output "$AE_RUN/figure-09-continued"
```

Before reusing a job, the script verifies its inputs, filesystem, resource settings, measurement code, and artifact hashes. Reused jobs keep the record of the code version that produced them, and the report shows which jobs were reused and which were newly measured. The old and new directories must be separate. This option applies only to Figure 9 and cannot be combined with `--resume`, quick checks, `--limit`, or `--max-events`.

**Output and interpretation.** In both panels of `figure-09-comparison.png`, compare the three curves within each file-size bin: does reflink reduce the private copy-up data, and how much of that reduction reaches the device? Filesystem journals and metadata also cause writes, so the two quantities need not match. The vertical axis is bytes per edit; device writes are read from loop-device write counters.

<table>
<tr><th>Paper figure/table</th><th>Example script output</th></tr>
<tr>
<td align="center" valign="top"><a href="ae/reference/figures/figure-09.png"><img src="ae/reference/figures/figure-09.png" alt="Figure 9: Paper figure/table" width="300"></a></td>
<!-- AE-RESULT:figure-09:start -->
<td align="center" valign="top"><a href="docs/images/figure-09-ae.png"><img src="docs/images/figure-09-ae.png" alt="Figure 9: Example script output" width="300"></a><br><small><a href="docs/images/figure-09-comparison.png">Open full comparison</a></small></td>
<!-- AE-RESULT:figure-09:end -->
</tr>
</table>

### 2.9 Filesystem correctness (§6.3.3)

<a id="correctness"></a>
<a id="步骤7"></a>
<a id="step-7"></a>

**Goal.** Check that file contents are the same before and after checkpoint/restore, that open file descriptors to deleted files behave correctly, and that writes are isolated across checkpoints.

```bash
bash ae/run_all.sh --experiment correctness --output "$AE_RUN/correctness"
```

This runs `test_full.sh`, `test_deleted_open_resurrect.sh`, and `test_cross_checkpoint_fd_cow.sh`. Their assertion logs are linked from `SUMMARY.md`. Each script should exit with status 0 and pass all of its named assertions on file contents and file-descriptor behavior.

## 3. Inspect, validate, and replot results

<a id="results"></a>
<a id="实验结果"></a>

Single experiments and full runs produce the same directory layout. Paths below are relative to the printed result directory; `review.json` identifies the current `attempt-NNN`.

| Path | Purpose |
| --- | --- |
| `result.md`, `SUMMARY.md`, `review.json` | Check status and locate outputs or failed steps |
| `comparison/attempt-NNN/README.md`, `README-zh.md` | English and Chinese pages comparing the paper with your measurements |
| `analysis/attempt-NNN/metrics.csv`, `series.csv` | Numerical summaries and plotted series |
| `gpu/attempt-NNN/` | GPU measurements, resource checks, plots, and Figure 8(c) calculations |
| `runs/`, `logs/attempt-NNN/` | Per-event data and execution logs |
| `environment/attempt-NNN/` | Configuration, CPU frequencies, and restoration records |

First confirm that the experiments succeeded, then use the guidance in each section above to evaluate the paper's claims. Small runs, such as `--limit 1`, only show that the workflow works. `N/A` and `—` mean "not measured", not zero.

<a id="绘图"></a>
<a id="发布图片"></a>
<a id="6-分析结果与绘图"></a>
<a id="plotting"></a>
<a id="publishing-figures"></a>

To regenerate the figures from existing raw results without new measurements, follow the [analysis guide](ae/docs/self-hosting.md#reanalyze).

## 4. Resume and troubleshoot

<a id="troubleshooting"></a>
<a id="运行提示"></a>
<a id="6-运行提示"></a>
<a id="7-运行提示"></a>
<a id="当前边界"></a>
<a id="running-tips"></a>
<a id="current-scope"></a>

To resume a run, keep the same code, configuration, and experiment selection, and replace `--output` with `--resume`. For example, to resume Figure 6:

```bash
bash ae/run_all.sh --group figure-06 --resume "$AE_RUN/figure-06"
```

| Situation | Action |
| --- | --- |
| The output directory already exists | Resume that run, or choose a new directory for a new run |
| A command reports that another AE run is active | Wait for the other run to finish, then retry |
| Your SSH session disconnected during a run | The run stops; continue it with `--resume <result-directory>` |
| No new terminal output for a long time | Check the log linked from `SUMMARY.md`, or `logs/attempt-NNN/<step>/stdout.log` |
| GPUs are unavailable or all busy | Ask the authors to allocate GPUs for the AE machine, then resume the same output directory |
| A dependency, permission, or template check fails | Save `SUMMARY.md` and the relevant logs, and contact the authors through the AE submission system |
| You want a smaller run | Add `--limit 1` to an individual command and use a separate output directory |
| You want the list of experiment names | Run `bash ae/run_all.sh --list` |

Keep the supplied resource configuration while measuring. On this shared machine, do not start other performance runs on the same CPUs or NUMA node. Account details are in the [hosted-access guide](ae/docs/hosted-access.md).

## 5. Self-hosting and further reading

<a id="self-hosting"></a>
<a id="环境准备"></a>
<a id="3-详细命令行指南"></a>
<a id="a2-构建镜像"></a>
<a id="baseline-配置"></a>
<a id="guest-内核"></a>
<a id="测量环境"></a>
<a id="路径-b母盘不可获取时的实验性重建"></a>
<a id="environment-setup"></a>
<a id="baseline-configuration"></a>
<a id="measurement-conditions"></a>
<a id="detailed-cli-guide"></a>

Running on your own machine requires Linux x86-64 with KVM, a DeltaBox guest kernel and disk images, the workload environments, and the baseline services you want to compare against. The [self-hosting guide](ae/docs/self-hosting.md) covers resources, build commands, configuration, and preflight checks. The GPU environment is separate from the CPU/KVM environment. `ae/run_all_no_gpu.sh` uses CPU and NUMA numbers specific to the AE machine; on other machines, use `ae/run_all.sh`.

- [Experiment inputs and file manifests](ae/paper/README.md)
- [Image builds and template preparation](ae/images/README.md)

## Appendix: hosted-machine details

<a id="hosted-details"></a>

These notes describe how the AE machine isolates and restores runs. You do not need them to evaluate the artifact.

**Managed execution.** On the AE machine, `ae/run_all_no_gpu.sh` runs inside its own systemd unit, in which processes started by the AE scripts cannot use swap. If the run is interrupted, the main process receives SIGINT and runs its normal cleanup; after a grace period, the unit stops any remaining processes. The launcher records the unit name and the final cleanup state.

**CubeSandbox memory.** When CubeSandbox runs from RAM, transparent huge pages are temporarily disabled for the Cube service. The AE machine's Cube VMM also includes a [pagemap classification fix](ae/patches/cube-pagemap-stable-classification.md): without it, the host relocating a page could change whether the VMM treats that page as anonymous, through a stale page-frame lookup. Source and child memory checksums are verified, and the original service settings are restored afterward.

**Figure 8 CubeSandbox profile.** Figure 8 runs CubeSandbox with N = 1 and N = 16. Within the run's NUMA node, it makes private, swap-free RAM copies of the Cube data and MySQL metadata, keeps the database durability settings, and verifies every copied file. Every child's inherited bytes, checksum, and token are checked, and the services and storage are restored afterward. If a resource cannot be released or restored, the private environment is kept for inspection, together with `RECOVERY_REQUIRED.json`. Setup and teardown are excluded from the timed clone and verification phases. Before each guest command, the driver checks with a read-only request that the guest agent (envd) is ready. This wait is part of source preparation or child verification, shares the command's deadline, and never causes a submitted command to be sent again.

**Background validation on NUMA nodes 0 and 3.** The authors use a second entry point to validate the artifact in the background, with a new output directory for each run:

```bash
bash ae/run_all_no_gpu_numa03.sh --output "$PWD/ae/results/selected/numa03-validation"
```

It runs the same 16 experiments with the same options, drivers, and reports, but on CPUs 0–3 of NUMA node 0 and CPUs 72–75 of NUMA node 3. Because both entry points use the same CubeSandbox and E2B services, the two runs never execute at the same time. A reviewer run takes priority: the background run cleans up its experiment and restores the services first, then later resumes in the same directory, reusing verified results. A stopped background run can also be resumed manually with `--resume` and the same directory; its CPU layout cannot change. Results are written under `ae/results/selected/<run-directory>/`, and the script prints the exact path. The run succeeded if it exits with status 0 and `SUMMARY.md` lists all 16 experiments as `ok`.

**Isolated validation of a single VM experiment.** To rerun one small VM experiment while another NUMA node is busy, use an isolated output directory and an explicit placement:

```bash
bash ae/run_all.sh --experiment figure-06-adaptive --limit 1 \
  --isolated-validation --numa-node "$AE_NUMA_NODE" --cpus "$AE_CPUS" \
  --output "$PWD/ae/results/selected/figure06-pilot"
```

This mode is limited to supported VM experiments; experiments that use the shared CubeSandbox or E2B services are excluded. It still takes exclusive use of the selected NUMA node and does not run during a results backup. To extend the run, resume the same directory with the same placement and a larger `--limit`; completed jobs are verified and reused, and completed and failed raw records stay separately identifiable. Every command locks its output directory before reading resume state, so two commands can never write to the same directory, or to a directory and one of its subdirectories, at the same time. `ae/results/checks/` is reserved for quick checks.

**Result directories.** Bounded runs do not use the older full-run backup and rotation path. If backup space is insufficient or other jobs are active, the launch stops and leaves existing results untouched. Each run records the exact source code it used, without requiring a release lock.
