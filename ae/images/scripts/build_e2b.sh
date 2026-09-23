#!/usr/bin/env bash
# Run INSIDE a prepared E2B host/L1; this builds the E2B template, not the L1 VM.
set -euo pipefail
[[ $# -ge 3 && $# -le 4 ]] || { echo "Usage: $0 E2B_INFRA_DIR NEW_STORAGE_DIR BUILD_UUID [BASE_OCI_IMAGE]" >&2; exit 2; }
[[ $EUID == 0 ]] || { echo 'Run as root on the prepared E2B host/L1 (mount and NBD access)' >&2; exit 1; }
infra=$(realpath "$1")
storage=$2
build=$3
image=${4:-e2bdev/base:latest}
[[ ! -e $storage && ! -L $storage ]] || { echo 'Refusing existing template storage' >&2; exit 1; }
[[ $build =~ ^[0-9a-fA-F-]{36}$ ]] || { echo 'BUILD_UUID must be a UUID' >&2; exit 2; }
expected=f9a52875167889d130c9f505b1315fa56a01d749
[[ $(git -C "$infra" rev-parse HEAD) == "$expected" ]] || { echo "Expected captured infra revision $expected" >&2; exit 1; }
grep -q 'flag.String("image"' "$infra/packages/orchestrator/cmd/create-build/main.go" || {
    echo 'Apply images/patches/e2b-create-build.patch to this checkout first.' >&2; exit 1;
}
[[ -c /dev/kvm ]] || { echo 'E2B requires /dev/kvm, including nested KVM if run in L1' >&2; exit 1; }
mkdir -p "$storage"
storage=$(realpath "$storage")
cd "$infra/packages/orchestrator"
git rev-parse HEAD > "$storage/source-commit.txt"
# Hash changed build inputs without exporting unrelated local configuration.
sha256sum cmd/create-build/main.go > "$storage/create-build.sha256"
go build -o "$storage/create-build" ./cmd/create-build
"$storage/create-build" -image "$image" -to-build "$build" -storage "$storage" \
    -kernel "${E2B_KERNEL:-vmlinux-6.1.158}" \
    -firecracker "${E2B_FC_VERSION:-v1.14.1_458ca91}" \
    -memory "${E2B_MEM_MIB:-1024}" -vcpu "${E2B_VCPUS:-1}" \
    -disk "${E2B_DISK_MB:-10240}" -hugepages=false -timeout 12 \
    2>&1 | tee "$storage/build.log"
echo "Built $build; inspect $storage/templates/$build. Payload injection is runner-specific."
