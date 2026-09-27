# Cubelet build inputs

The original four-file timing patch was subsequently recovered and is preserved
in `ae/patches/cubelet-original-phase-instrumentation.patch`, applied to upstream
CubeSandbox `a7b099dba3f7c789c93e4b608fa44943c316505e`.
`ae/patches/cubelet-create-gc-race.patch` separately synchronizes container creation
with DeadGC using the existing lifecycle lock; it does not change phase timers
or clear genuine exit/pause state. Build the native cubecow library with Rust
1.89.0 and the upstream locked dependencies, then Cubelet with Go1.26.3/GCC11.4.
The patched deployment must be identified by its own binary SHA, independently
of the retained version string. Original and patched measurements remain separate
sources; the optional Cube paper-disk completed-result import preserves old bytes.
This supersedes the earlier source-recovery status in the immutable vendor provenance note. The vendor directory is retained byte-for-byte for completed-measurement reuse validation.
