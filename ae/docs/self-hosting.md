# Self-hosting and targeted runs

[简体中文](self-hosting-zh.md) · [Back to the AE guide](../../README.md)

The hosted evaluation machine already supplies the experiment environment. Use this page to deploy on your own Linux machine, select a configuration, or reanalyze results. Run commands from the repository root.

## 1. Prepare the host and inputs

You need Linux x86-64, usable `/dev/kvm`, Firecracker, Python 3.10+, sudo privileges, and tools for XFS/ext4, device-mapper, CRIU, and network namespaces. Building from OCI also requires Docker. See the [image and template guide](../images/README.md) for installation and backend requirements.

DeltaBox typically uses 4 vCPUs and 8 GiB guest RAM; allow additional host memory for runners, images, and temporary state. Reserve at least 80 GiB for image building, plus space for experimental work disks, dumps, and caches. GPU requirements are in the [GPU guide](../paper/figure-08/README.md).

```bash
git clone --branch main https://github.com/delta-box/delta-box-ae.git
cd delta-box-ae
python3 -m venv .venv
.venv/bin/pip install -r ae/requirements-analysis.txt
.venv/bin/python ae/reproduce.py prepare
.venv/bin/python ae/scripts/paper_data.py verify

firecracker --version
sudo test -r /dev/kvm
sudo test -w /dev/kvm
```

Obtain repository access first. Input bundles are in `ae/datasets/`; `prepare` verifies and extracts them. The patched guest kernel is included at `linux/vmlinux`. Obtain the base/data disks and complete host workload environment from the authors, or build them using the image guide.

## 2. Build guest images

<a id="images"></a>

Use the matching XFS master disk and DeltaBox guest kernel to build the base and grouped data disks. The output directory must not exist:

```bash
bash ae/build_images.sh \
  --master-xfs /path/to/ubuntu-24.04.xfs \
  --kernel "$PWD/linux/vmlinux" \
  --ssh-pubkey "$HOME/.ssh/id_ed25519.pub" \
  --output "$PWD/ae/work/images-local"
```

`--ssh-pubkey` is the public key used for host-to-guest communication; the host account running the experiment keeps the private key. Generate your own key if needed, without overwriting an existing one.

The builder checks the selected inputs' hashes and produces `bundle.json`, logs, and `config.json`. To compile the kernel, replace `--kernel` with `--kernel-source /path/to/clean-linux-6.8`. For OCI and public base-image build paths, see the [build guide](../images/README.md#统一构建入口). Image provenance and methodological differences are documented in the [measurement report](https://github.com/delta-box/deltabox-runtime/blob/main/ae/report/README.md#conditions).

## 3. Configure baselines and measurement resources

The image builder does not deploy Cube/E2B services. Prepare the dependencies for your selected experiments:

| Backend | Configuration needed |
| --- | --- |
| DeltaBox | Kernel, base/data disks, guest SSH, CRIU |
| Replay / CRIU | Host workload payload, Python environment, repo/index; host CRIU and rsync for CRIU |
| FC-Diff | Device-mapper thin, loop devices, and a VM image with the replay environment |
| Cube | SDK, API, proxy, and workload templates; fan-out uses a separate template |
| E2B | Self-hosted infra, parent images, resume binary; SDK/API, template, and credentials for fan-out |

Field names and build entry points are in the [image guide](../images/README.md). Set local paths in the configuration and provide credentials through the environment.

Set available CPUs from one NUMA node in `measurement`. These numbers are examples; check your topology with `numactl --hardware` first:

```json
{
  "measurement": {"pin": true, "numa_node": 0, "cpus": "0-3"}
}
```

Pinning requires `numactl`, `turbostat`, and cpufreq permissions. The runner saves the original settings, samples actual frequencies, and restores settings on normal exit or handled interruptions. Avoid CPUs used by other tasks, and configure Cube/E2B servers consistently.

```bash
export AE_PYTHON="$PWD/.venv/bin/python"
export AE_CONFIG="$PWD/ae/work/images-local/config.json"

sudo -v
sudo -E "$AE_PYTHON" ae/reproduce.py doctor --config "$AE_CONFIG" --all
"$AE_PYTHON" ae/reproduce.py plan --config "$AE_CONFIG" --all \
  > ae/work/local-cpu-plan.json
bash ae/run_test.sh --config "$AE_CONFIG"
```

`doctor` checks prerequisites; `plan` lists workloads and commands without running experiments. After preflight, use the individual commands in the root README. The exported `AE_CONFIG` supplies their default configuration.

For a functional check, explicitly use `--no-pin`. Self-hosted setups can use `--available` for a subset with available dependencies; use the default strict mode for complete experiments. The hosted launcher uses a fixed configuration and does not accept these self-hosting options.

### Configure GPUs

<a id="gpu-setup"></a>

The one-click workflow uses only auto mode: SSH to `allinai2plus` and probe physical GPUs 0–7. The initiating host needs no GPU. Copy and adjust `ae/configs/figure08-remote.json`, then set `"gpu_remote_config": "/absolute/path/to/remote.json"` in the main `AE_CONFIG`; relative paths resolve against that configuration's directory. Configure the SSH host, remote directory, model/Python paths, idle thresholds and top-level version constraints. `gpu.config` and `gpu.enabled` no longer control one-click execution.

Prepare noninteractive SSH, rsync on both hosts, local plotting dependencies and the remote GPU environment. The default reuses deployed py312 without installing software; preflight records version drift. See the [Figure 8 guide](../paper/figure-08/README.md) for setup and exact parameters.

`--group cpu` selects CPU only; `--group gpu` automatically runs remote generation/training; `--group figure-08` also measures CPU fan-out and derives (c) when all eight GPU cases and CPU inputs are complete. Busy GPUs cause a skip, 1–3 idle GPUs allow six cases, and four allow eight. GPU failure does not change the CPU exit code; `result.md` preserves status and gaps. Resume creates a new GPU attempt; analysis-only never starts SSH measurements.

## 4. Specialized configurations and RAM-backed entry points

<a id="specialized-runs"></a>

On an experiment host you administer with sudo privileges, these entry points provide fixed RAM-backed configurations. Check kernel, image, CPU/NUMA, and dependency paths first:

```bash
# Table 3: async incremental and lazy-pages fast/slow comparison.
AE_CONFIG="$PWD/ae/configs/spr4numa-table3.json" \
  bash ae/run_table3.sh --output "$PWD/ae/results/table3-local"

# Figure 9: noswap tmpfs for active host disks and guest loop storage.
bash ae/run_figure09.sh --config "$AE_CONFIG" \
  --output "$PWD/ae/results/figure09-local"
```

The Table 3 configuration binds NUMA 2 / CPUs 48–51; the Figure 9 entry point binds NUMA 2 / CPUs 52–55. These settings target spr4numa and must not be used unchanged on a different topology. Temporary measurement I/O uses RAM disks; permanent results go to the requested output directory. Table 3 profiles and timing definitions are in the [method guide](table3-method.md).

## 5. Reanalyze existing results

<a id="reanalyze"></a>

In an analysis environment using the ordinary CLI entry point, replace the existing-run path and choose a new output:

```bash
bash ae/run_all.sh --analyze-existing /path/to/existing-run \
  --output "$PWD/ae/results/replot-local"
```

This analyzes and plots without executing experiments. The restricted hosted launcher does not expose this option. Normal reviewer runs already generate figures; for additional plotting, contact the authors or copy the complete result directory into an analysis environment.

CPU VM experiments require Linux/KVM; existing results can also be analyzed and plotted on macOS. See the [publication guide](publish-results.md) and [plotting notes](paper-plotting-reference.md) for lower-level commands.

For a self-hosted machine, set `review.results_backup_root` in your configuration to a separate backup filesystem. A new complete run copies and verifies the previous `ae/results` there before clearing it; the destination must retain at least 10 GiB free after the copy. Explicit `--output` and `--resume` do not rotate the complete results directory.
