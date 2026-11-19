# DeltaBox — ATC 2026 artifact

DeltaBox checkpoints, restores, and branches an agent sandbox's filesystem and process state. This repository provides its runtime, a prebuilt DeltaFS kernel, experiment drivers, and analysis scripts. The [v4 Zenodo archive](https://zenodo.org/records/24358847) provides the experiment images and recorded inputs/results.

The environment has two boot profiles. DeltaBox runs directly inside a 4-vCPU/8-GiB VM with the supplied DeltaFS kernel. The baseline profile starts the preconfigured services for E2B, CubeSandbox, FC-Diff+dm, CRIU+cp, and copy+replay. Both profiles use the same downloaded base disk. A small qcow2 layer adds the DeltaBox boot entry and functional checks.

**English** | [简体中文](README-zh.md)

## Download the environment

Use an x86-64 Linux host with KVM, QEMU/KVM, `qemu-utils`, Python 3.10+, and `zstd`. Your account needs access to `/dev/kvm`. Allow approximately 200 GiB of free storage for download, decompression, and baseline working files.

```sh
git clone https://github.com/delta-box/delta-box-ae.git
cd delta-box-ae
python3 ae/appliance/download.py --output-dir ../images
```

The downloader verifies SHA-256 checksums and reuses verified local files. Keep the base image and `deltabox-direct.qcow2` in the same directory.

## Run DeltaBox directly

Choose free host CPUs before starting. Adjust the example CPU list for your machine.

```sh
taskset -c 0-3 python3 ae/appliance/deltabox.py check --images ../images --log check.log
taskset -c 0-3 python3 ae/appliance/deltabox.py fanout --images ../images --log fanout.log
python3 ae/appliance/deltabox.py shell --images ../images --log shell.log
```

`check` runs the included runtime through coupled filesystem/process checkpoint and restore. It checks that a saved file returns, a later file disappears, and the worker's in-memory counter resumes from the checkpoint. `fanout` checks all pages of 64 MiB inherited state for N=1/4/16/64 children. Successful runs print `DELTABOX_DIRECT_EXIT=0` and return exit code 0.

The interactive shell exposes the runtime under `/repo`, CRIU, a read-only rootfs under `/mnt/base`, and the `deltabox-check` and `deltabox-fanout` commands. For example, run `deltabox-fanout --forks 1 4 --out /tmp/forks.json`. Writable sandbox state lives in noswap tmpfs. The launcher closes its VM when the command completes. These checks validate artifact functionality. Recorded paper measurements are described below.

## Run the five baselines

The baseline profile requires nested KVM support, OpenSSH, `genisoimage`, and `util-linux`. Its full configuration uses 48 host CPUs across four NUMA nodes and 120 GiB guest RAM. For a short functional check, add `--functional-only --memory-gib 64` and supply 12 allocated host CPUs.

```sh
python3 ae/appliance/appliance.py start --image ../images/DeltaBox-ATC2026-v3-l1.qcow2 --cpus 12-35,56-67,72-83
python3 ae/appliance/appliance.py ssh
```

Inside the VM, run from the public repository.

```sh
cd /home/atc-ae/delta-box-ae
sudo python3 ae/appliance/reproduce.py e2b --smoke --out /home/atc-ae/delta-box-ae/ae/results/e2b-smoke
```

Replace `e2b` with `cube`, `fc-diff`, `criu`, or `replay`. `table3` runs CRIU inference overlap. Omit `--smoke` for the recorded 12-trace cohort, or select inputs with `--instance`. Each output directory must be new. The adapter runs trajectories serially using their recorded virtual NUMA assignment. Detailed placement, timings, and original orchestration are in the [data provenance](ae/refresh-20261111/README.md).

Stop this profile from the host with `python3 ae/appliance/appliance.py stop`.

## Recorded results and the final paper

The archive contains the selected November baseline records for the online final paper, historical DeltaBox records, and Figure 1/7/8/9 inputs. The [data index](ae/refresh-20261111/README.md) identifies adopted batches and exact values. Online Table 2 uses CRIU 567.6/609.5 ms. The overlap batch supplies Table 3's 91.1 ms remaining wait.

Download `DeltaBox-ATC2026-v3-source-data.tar.zst` from [Zenodo](https://zenodo.org/records/24358847) and extract it into a separate directory.

```sh
mkdir recorded
zstd -d --stdout DeltaBox-ATC2026-v3-source-data.tar.zst | tar -xf - -C recorded
python3 recorded/delta-box-ae/ae/appliance/verify_results.py --evidence recorded/delta-box-ae/ae/refresh-20261111/evidence --out recorded/recomputed.json
```

To draw the current figures from this checkout, install `numpy` and `matplotlib`, then run `python3 ae/appliance/draw_figures.py`.

[Image build and acceptance](ae/appliance/direct/README.md) · [Historical hosted-machine guide](README-v2.md) · [Experiment ledger](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/README.md)
