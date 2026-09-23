#!/usr/bin/env bash
# The supplied source tree must contain DeltaBox's patched Linux 6.8 overlayfs.
set -euo pipefail
# Reuse the exact kernel used by a known run without pretending to rebuild it
# from a source checkout that may have changed since that binary was built.
if [[ ${1:-} == --existing ]]; then
    [[ $# == 3 ]] || { echo "Usage: $0 --existing VMLINUX NEW_OUTPUT_DIR" >&2; exit 2; }
    src=$(realpath "$2")
    out=$3
    [[ -f $src ]] || { echo 'Missing vmlinux' >&2; exit 1; }
    [[ ! -e $out && ! -L $out ]] || { echo 'Refusing existing output directory' >&2; exit 1; }
    mkdir -p "$out"
    out=$(realpath "$out")
    sha256sum "$src" > "$out/source.sha256"
    cp --reflink=auto -- "$src" "$out/vmlinux"
    cmp -- "$src" "$out/vmlinux"
    sha256sum --check "$out/source.sha256"
    sha256sum "$out/vmlinux" > "$out/artifacts.sha256"
    printf '%s\n' 'Existing binary copied; source/config/compiler provenance is not reconstructed.' > "$out/origin.txt"
    exit 0
fi
[[ $# -ge 2 && $# -le 3 ]] || { echo "Usage: $0 PATCHED_LINUX_SOURCE NEW_OUTPUT_DIR [CONFIG]" >&2; exit 2; }
root=$(cd "$(dirname "$0")/.." && pwd)
src=$(realpath "$1")
out=$2
config=${3:-$root/configs/linux-6.8-spr4numa.config}
[[ ! -e $out && ! -L $out ]] || { echo 'Refusing existing output directory' >&2; exit 1; }
grep -q 'OVL_IOCTL_CHECKPOINT' "$src/fs/overlayfs/"*.[ch] || { echo 'Missing DeltaBox overlayfs changes' >&2; exit 1; }
[[ $(make -s -C "$src" kernelversion) == 6.8* ]] || { echo 'Expected Linux 6.8 source' >&2; exit 1; }
mkdir -p "$out"
out=$(realpath "$out")
cp "$config" "$out/.config"
git -C "$src" rev-parse HEAD > "$out/source-commit.txt"
# Hash the actual overlayfs files too: the experimental checkout may be dirty.
(cd "$src" && find fs/overlayfs -type f \( -name '*.c' -o -name '*.h' -o -name Makefile -o -name Kconfig \) -print0 | sort -z | xargs -0 sha256sum) > "$out/overlayfs-source.sha256"
make -C "$src" O="$out" olddefconfig
make -C "$src" O="$out" -j"${JOBS:-8}" vmlinux bzImage modules 2>&1 | tee "$out/build.log"
if grep -q '^CONFIG_OVERLAY_FS=m$' "$out/.config"; then
    echo 'Overlay is modular. Install fs/overlayfs/overlay.ko in the guest before running DeltaBox.'
else
    echo 'Overlay is built into vmlinux, matching the collected 79 configuration.'
fi
sha256sum "$out/vmlinux" "$out/arch/x86/boot/bzImage" "$out/.config" > "$out/artifacts.sha256"
