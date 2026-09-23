#!/usr/bin/env bash
# Prepare an E2B nested-KVM Ubuntu 24.04 disk and cloud-init seed; do not boot it.
set -euo pipefail
[[ $# == 4 ]] || { echo "Usage: $0 NOBLE_CLOUD_IMAGE SHA256 SSH_PUBLIC_KEY NEW_OUTPUT_DIR" >&2; exit 2; }
base=$(realpath "$1")
checksum=$2
pubkey=$3
out=$4
[[ ! -e $out && ! -L $out ]] || { echo 'Refusing existing output directory' >&2; exit 1; }
[[ $checksum =~ ^[a-fA-F0-9]{64}$ ]] || exit 2
echo "$checksum  $base" | sha256sum -c -
grep -Eq '^(ssh-rsa|ssh-ed25519|ecdsa-sha2-[^ ]+) ' "$pubkey" || { echo 'Expected SSH public key' >&2; exit 2; }
mkdir -p "$out"
# Independent qcow2 avoids an absolute backing-file dependency after distribution.
qemu-img convert -f qcow2 -O qcow2 "$base" "$out/l1.qcow2"
qemu-img resize "$out/l1.qcow2" "${E2B_L1_DISK_SIZE:-120G}"
python3 - "$pubkey" "$out" <<'PY'
import json,sys
from pathlib import Path
key=Path(sys.argv[1]).read_text().strip(); out=Path(sys.argv[2])
config={'users':[{'name':'ubuntu','sudo':'ALL=(ALL) NOPASSWD:ALL','shell':'/bin/bash','ssh_authorized_keys':[key]}],
        'ssh_pwauth':False,'package_update':True,
        'packages':['openssh-server','ca-certificates','curl','make','gcc','git','rsync','iptables','iproute2','docker.io','jq'],
        'runcmd':['systemctl enable --now ssh docker','usermod -aG docker ubuntu','sysctl -w vm.unprivileged_userfaultfd=1']}
(out/'user-data').write_text('#cloud-config\n'+json.dumps(config,indent=2)+'\n')
(out/'meta-data').write_text('instance-id: ae-e2b-l1\nlocal-hostname: ae-e2b-l1\n')
PY
cloud-localds "$out/seed.img" "$out/user-data" "$out/meta-data"
sha256sum "$out/l1.qcow2" "$out/seed.img" > "$out/artifacts.sha256"
echo 'L1 prepared. Boot with KVM and -cpu host; then install the pinned E2B infra/toolchain inside L1.'
