# DeltaBox archive setup

[简体中文](ARCHIVE-zh.md) · [AE guide](README.md)

The Zenodo archive contains the DeltaBox implementation, experiment scripts, recorded inputs and results, and the prebuilt x86-64 guest kernel at `linux/vmlinux`. `linux/kernel.config` records the kernel build configuration. Component license notices are retained in their original directories.

`VERSION.json` records the upstream AE revision and the kernel identity. `FILE-MANIFEST.jsonl` and `SHA256SUMS` describe the files delivered in this archive. Git metadata is included so the experiment runners can record the source they execute.

## Prepare a Linux host

Extract the package and enter the `delta-box-ae` directory. Use Linux x86-64 with Python 3.10+, Git, and `zstd`:

```bash
cd delta-box-ae
python3 -m venv .venv
.venv/bin/pip install -r ae/requirements-analysis.txt
.venv/bin/python ae/reproduce.py prepare
```

For a new machine, continue with the [self-hosting guide](ae/docs/self-hosting.md), skipping its Git clone step. It describes KVM, Firecracker, CRIU, guest disks, and baseline services. The hosted evaluation machine already supplies these dependencies; its login and experiment commands are in the [AE guide](README.md).

## Use the prebuilt kernel

Pass the included kernel to the image builder with the matching XFS master disk and your public SSH key:

```bash
bash ae/build_images.sh \
  --master-xfs /path/to/ubuntu-24.04.xfs \
  --kernel "$PWD/linux/vmlinux" \
  --ssh-pubkey "$HOME/.ssh/id_ed25519.pub" \
  --output "$PWD/ae/work/images-local"
```

The output directory must be new. The master disk and the baseline environments are obtained or prepared as described in the self-hosting guide. After configuring the host, use the quick check and experiment index in the AE guide.
