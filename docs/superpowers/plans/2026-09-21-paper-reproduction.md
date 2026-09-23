# Paper experiment reproduction implementation

Base: origin/main, c74c8f58819b1665c192c5b03d35b8ad8464a751.
Branch: feat/paper-reproduction. Paper scope: submitted atc26-paper158.pdf; GPU experiments explicitly skipped by the author.

The author requested scripts and traces in this runtime repository to rerun all
paper experiments. Preserve the paper and existing runtime semantics. Archived
analysis and fresh measurements must be separate, with no hard-coded fallback
numbers or successful status for absent inputs, failed experiments, or dry runs.

Tasks:

1. Bundle the existing curated AE traces, image recipes, per-figure manifests,
   and source provenance under ae/. Verify hashes from a fresh extraction.
   Create one CLI for list/prepare/doctor/plan/run/analyze/plot/all. Explicitly
   classify qualitative figures, CPU experiments, GPU measurements and derived
   metrics. Retain all original source attribution and recorded baseline limits.
2. Port the proven DeltaFS table4 VM harness into ae/runners/deltabox/, using
   the root repository's backends/deltabox/gsd modules and pycriu rather than
   a second runtime copy. Feed trace/schedule inputs from ae/paper/. Preserve
   per-run source, environment, input and image hashes; own all VM resources;
   validate expected events and worker restoration; support fast/slow and
   accurate API vs critical-path timing. Run host tests and real VM smoke.
3. Recover and parameterize original baseline, motivation, memory-depth,
   adaptive checkpoint, CPU fan-out and write-amplification experiment drivers.
   Use pinned sources where a historical baseline is required. Provide actual
   executable commands and artifact validators, not success stubs. Connect
   each empirical figure/table and the correctness suite to its inputs and
   resource requirements. No unrelated service changes or broad cleanup.
4. Build deterministic aggregation and plots for fresh outputs and separately
   for archived evidence. Preserve cohort boundaries, show measured/modelled
   distinctions, validate sample completeness and numeric fields. Mark GPU-dependent panels skipped and execute E2B 4x16 batches
   rather than calling an estimate a measurement.
5. Verify CLI and input bundles from a clean checkout, test failure propagation
   and path/resource safety, run local analyses and available remote smoke.
   Perform independent specification review followed by code-quality review;
   fix findings. Commit scripts/traces/docs to the new branch and report exact
   commands, executed checks, and remaining hardware-dependent verification.

Ownership: root coordinates integration/data/orchestration. One implementation
subagent at a time may own the VM adapter task; independent read-only research
and reviews can run alongside it. Source repos and the current runtime checkout
are read-only; all changes go to this isolated worktree. Do not alter the paper,
silently select a different cohort, or label async full dump as incremental.

Progress:

- [x] Verify default branch; create isolated branch/worktree.
- [x] Task 1: packaged data, recipes and unified CLI.
- [x] Task 2: current-runtime VM replay adapter and real smoke.
- [x] Task 3: full experiment driver coverage.
- [x] Task 4: analyses and plotting.
- [x] Task 5: clean verification, reviews and branch commit.

CPU coverage is delivered as executable entry points with explicit compatibility limits in ae/docs/runtime-validation.md. All GPU work is skipped at author request. Independent spec and quality reviews passed after fixing findings.
