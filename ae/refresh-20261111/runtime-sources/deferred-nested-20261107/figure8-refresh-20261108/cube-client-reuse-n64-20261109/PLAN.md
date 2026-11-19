# Cube latest client-context reuse N64 — 2026-11-09

User: “Cube 最新代码的 N=64 也跑一下，成功一轮就停，然后补全 Figure8(a)，先别改论文。”

- One excluded N1 warmup, then exactly one formal N64 fan-out. Stop automatically after success; do not schedule further measured rounds. If failure occurs, retain everything and restore before diagnosis, never silently retry/discard.
- Same client_mode.py, bench_fanout.py and native verify_state bytes as latest cube-client-reuse-20261109 (SHA256 recorded in protocol-lock.json). Same process-local CA context reuse; TLS verification stays enabled; each child HTTP client is still constructed at its original first-command position inside the measured interval. No pre-reading child RAM.
- Same L1/QEMU/KVM, NVMe, 4vCPU/8GiB full-agent template cube-agent-full-4c8g-l1e, same MySQL/Redis mounts and flush configuration. Same existing pagemap-repaired VMM d7d0f539… as latest Cube batch, NOT the unmodified official release b2d043cc…. No server/SDK/VMM changes.
- Same 16-CPU VM pool, 8-CPU control pool, driver4–7, host52CPU min/max2.1GHz; restore previous placement/frequency afterwards. Cube explicit hugepages=false, THP=true, prefault=false. No hugepage reservation change.
- Same continuous start-to-completion clock: official SDK clone (snapshot + concurrent64 creates + temporary snapshot deletion), then all64 children's full64MiB page-marker check and command return. Four native reader threads perVM and maximum4VMs validating concurrently; keep all64 children until complete. Source preparation and final destruction stay outside.
- Validate each of16384 4KiB page markers per child, inherited token/PID, requests2, checksum2041721, errors0. This is not a bytewise64MiB comparison. Post-timer identity audit checks all children4c8g and every live source/child VMM executable hash. Audit work stays outside primary timing.
- Fixed45s quiescence per point, no per-point cache drop. Same >30GiB NVMe start guard, >15GiB run guard, guest free8GiB and availableRAM8GiB guards. Starting NVMe37.59GiB, prior Cube64 observed space drawdown~0.30GiB. No deletions for space.
- Archive raw results, client spans, same-window service logs, live VMM configuration, cleanup receipts, resource sampling, pre/post restoration. Canonical ledger ae/report/README.md.
- Complete the independent clean Figure8(a) preview only. Preserve latest N1/4/16 points and DeltaBox. E2B N64 stays the user-selected prior acceptance3843.726369ms; its latest single success3890.011512ms remains separately recorded. Document per-point provenance/statistics outside the image. Do not modify paper.

Run path: spr4numa:/mnt/disk2/dyp/deltabox-runtime/ae/work/deferred-nested-20261107/figure8-refresh-20261108/cube-client-reuse-n64-20261109/; Mac same-named evidence directory.
