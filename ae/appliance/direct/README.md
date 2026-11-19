# DeltaBox appliance boot profile

The direct boot profile uses the published Linux 6.8 DeltaFS kernel with QEMU/KVM microvm. It mounts the appliance root read-only, then uses the included XFS rootfs as a read-only lower layer for a noswap tmpfs upper. A private mount namespace and pivot_root expose the runtime and dependencies. DeltaBox processes run in this VM.

The v4 qcow2 layer contains `/usr/local/lib/deltabox-direct/init` and `check.py`. Their sources are in this directory. The layer has the relative backing filename `DeltaBox-ATC2026-v3-l1.qcow2`. Place both images in the same directory. `DIRECT-APPLIANCE.json` pins image and kernel checksums.

For this update, the layer was created with `qemu-img create -f qcow2 -F qcow2 -b DeltaBox-ATC2026-v3-l1.qcow2 deltabox-direct.qcow2`. The two guest files were installed with libguestfs, with executable mode 0755 on `init`. `qemu-img check` passed. The base image retains its v3 SHA-256.

`VALIDATION.json` and the two acceptance logs record the functional checks. Coupled rollback checks both files and a running worker's memory. Fan-out validates every page of 64 MiB in all children at N=1/4/16/64. CRIU logs include a missing socket diagnostic module warning during probing, followed by successful dump and restore. These checks exercise the shipped runtime. Paper measurement sources are indexed in `ae/refresh-20261111/README.md`.

The direct profile is offline. Its writable state is discarded on exit. The interactive shell provides `/repo`, `/mnt/base`, CRIU, `deltabox-check`, and `deltabox-fanout`.
