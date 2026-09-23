# Baseline source provenance

These are recovered experiment drivers, not a second DeltaBox production runtime.
The current DeltaBox runtime is injected by `ae/runners/deltabox/provenance.py` from
this checkout's `backends/deltabox/gsd` and `pycriu`.

The lock files retain the **source** SHA-256, original repository/path and Git
revision where recoverable. `remote-source-lock.json` explicitly records exported
working-tree files; those must not be represented as pristine Git blobs. The
Cube canonical driver came from the June 10 deployed working tree, not merely the
similarly named Git HEAD script. `adapted-sha256.json` records this branch's final
file bytes after the portability and validation changes below. Verify it with:

```sh
python3 ae/scripts/verify_runtime_sources.py
```

| File / source lock | Role |
|---|---|
| `source-lock.json` | finalbench `fa7e8e1d9bef60e6031480d91ec02709ccd1a90f`, d-overlayfs `6819771a572094191bd3ab594d3466cad9123e6f`, recovered baseline and guest sources |
| `remote-source-lock.json` | Exact exported Cube canonical driver, official fanout, profile instrumentation, legacy helpers; original export provenance retained |
| `payload-source-lock.json` | Recorded-response mock, replay driver, trajectory loader and protocol |
| `guest-extra-source-lock.json` | Nonshared extent measurement helper |
| `additional-source-lock.json` | Cube phase parser from d-overlayfs `d93e6610` |

Portability changes replace fixed host paths with explicitly supplied environment
variables, keep mutations within a new owned output tree, put fixed-address VM
networks/mounts in private namespaces, and clean owned child groups on timeouts.
FC API sockets use short owned temporary paths when output paths exceed AF_UNIX
limits. DM partial creation is tracked for cleanup. CRIU failures retain logs;
tail crashes are failures rather than completed cohorts. No global host cache,
frequency or unrelated service cleanup was added.

Replay mock records actual sleep duration. Per-restore output preserves raw elapsed time,
measured sleep and zero-LLM duration; no archived correction constant is used.
E2B action builds and fresh node/step identity are checked against the selected
trace. E2B fanout N64 now executes four real batches of 16 and includes inter-batch
cleanup in the measured elapsed time, replacing the historical estimate.
Fanout snapshots omit the optional name so E2B returns a canonical ID that SDK
2.25.1 can delete without an unescaped namespace separator. Explicit `False`
returns and exceptions from cleanup are recorded and fail the row; cleanup is
still attempted for all remaining owned resources. Failed inter-batch deletion keeps the child
owned for the final cleanup attempt. Final cleanup stays outside the measured
ready latency, and older SDKs returning `None` remain supported.

The September 22 recovery adds explicitly selected local-host execution to the
E2B Table 2 driver; SSH/L1 remains the default transport. Both invoke the same
configured E2B resume-build implementation. The local mode records its placement,
infra/source/binary identity and base snapshot files; it is not labeled as the
historical nested-VM environment. Offline dependencies are staged before timing,
and successful timing JSON must agree with a zero process exit code. Original
source locks retain the historical files; the adapted lock identifies these edits.

Cube API events record sandbox/snapshot identity and API latency.
The runner now records the live template's CPU, memory and writable-layer values,
and hashes the supplied Python SDK. Legacy CLI resource defaults did not configure
the sandbox and must not be used as evidence of its actual size.
Figure 1 optionally requires a newly appended instrumented Cubelet log and fails
on missing/ambiguous phases or retries. The installed instrumented Cubelet on
spr4numa was identified as SHA-256
`50c4bdfb9549dbf2ef4129267130b4ba739054d83710ee962d9d743b500b410e`.
Its timing patch source was not recovered; the existing binary/service or an
equivalently instrumented deployment is required. The SDK and service binaries
are not copied into this repository. Replaying with a different supplied binary
records its actual hash instead of claiming historical identity.

The Figure 2 instrumentation and historical requirements specification are
included; the installed full Moatless/index dependency tree is external and is
hashed at runtime. Existing external repositories are cloned at the trace base
commit before use. No new LLM generation or GPU experiment is performed.

The replay acceptance branch additionally stages real local NLTK resources into
FC-Diff's private rootfs and enables LiteLLM's packaged local model metadata.
This setup occurs before measurements. `adapted-sha256.json` tracks the new driver
bytes; all original source locks remain unchanged. The producer-side synthetic
history ordering adapter is staged into a private Moatless copy by the runner;
it does not rewrite incoming requests or relax the strict recorded-response mock.
