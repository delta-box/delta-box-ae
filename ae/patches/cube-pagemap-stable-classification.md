# Cube private-RAM snapshot classification repair

The deployed Cube VMM cached `/proc/self/pagemap` for guest RAM, then queried
`/proc/kpageflags` using the cached PFNs. A PFN can change while the guest is
paused, because host memory relocation continues. The later flags can describe
the old, freed frame rather than the virtual page being snapshotted.

In the 2026-10-02 NUMA1 diagnostic, the N16 source's 512 MiB RAM differed from
its stored snapshot in 235 pages. Every differing page was a present private
anonymous page in the classifier's original pagemap buffer, was excluded by
its actual output bitmap, had a changed PFN at classifier return, and retained
that new PFN until the paused RAM copy. At observer readback the old PFNs had
KPF_BUDDY and the new PFNs had KPF_ANON. The corresponding N1 capture matched.
The flag readback is after classification, not a claim to have traced each
original kpageflags read. Original failed experiments remain failed.

The repair uses the file/shared-anon flag in the same cached pagemap entry,
retaining present/swapped handling and PFN-zero permission rejection. In this
MAP_PRIVATE, file-backed guest-RAM path, present non-file pages are the private
CoW pages to save. File-backed pages remain inherited from the base. Present
zero pages can be conservatively copied. This does not switch to full snapshots,
truncate workloads, retry commands, or relax checksum, timeout or cleanup checks.

`cube-pagemap-stable-classification.patch` shows the equivalent upstream source
change against TencentCloud/CubeSandbox a7b099dba3f7c789c93e4b608fa44943c316505e.
It is not proof that the whole installed VMM was rebuilt from that commit.
For the existing deployed x86-64 VMM, `make_classifier_patch.py` reproducibly
replaces exactly one 77-byte instruction region. It rejects every input except
SHA256 6e024b582a44167f93e868c9c26b3057dbafb19d5680dd08575a7f35146ca42c, checks
ELF address translation and original instructions, and requires fresh output
paths. All bytes outside that region are identical. The result is SHA256
d7d0f53923b4c72bb99ed0b0ccba2106484186f4830e7fa190cafe13bc45ea6e. No runtime
GDB hook is needed or enabled for ordinary measurements. ELF build ID and
version text are retained and must not be used instead of the full-file SHA.

Usage (build a candidate, never overwrite the input):

```sh
python3 make_classifier_patch.py ORIGINAL CANDIDATE RECEIPT.json
python3 validate_classifier_patch.py --candidate CANDIDATE --receipt RECEIPT.json --work CHECKS
```

Install only with the hosted/results/Cube/NUMA leases held, no active Cube VMs,
verified restoration and an original-byte backup. Installation is an atomic
replacement; any subsequent measurements must bind the new VMM SHA separately
from the AE source identity. A changed backend must start a fresh campaign,
not resume results frozen to the old backend.

Validation executes the actual candidate loop instructions on edge cases,
32,768 fixed-seed random pagemap entries, and both captured 131,072-entry maps.
All 235 missing pages are selected; overlaying the selected source pages on
the old stored snapshot leaves zero differences. PFN-zero and wrong/repeated
binary patch inputs are rejected. Live qualification is recorded separately;
its debugger and memory-copy timings are excluded from acceptance results.

Kernel semantics: https://docs.kernel.org/admin-guide/mm/pagemap.html
Original code: https://github.com/TencentCloud/CubeSandbox/blob/a7b099dba3f7c789c93e4b608fa44943c316505e/hypervisor/vmm/src/pagemap_anon.rs

Original NUMA0/3 success remains a distinct old-backend measurement. Historical
Cube checksum failures and every historical panic are not retroactively proved
to have this exact cause. E2B still uses its original token/field-presence verifier.

Observed live qualification on 2026-10-02 22:17 CST: original N1/N16, all
17 strict child checks passed, exit 0; both 512 MiB stored snapshots matched
the paused source RAM byte-for-byte. This verifies this repair in that bounded
case, not all historical failures or completion of a new full campaign.
