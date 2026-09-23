#!/usr/bin/env bash
# Usage: build_master.sh INSTALLER SHA256 NEW_WORK_DIR IMAGE_TAG [ENV_SPECS_JSON]
# Or: build_master.sh --from-ubuntu NEW_WORK_DIR IMAGE_TAG [ENV_SPECS_JSON]
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
download=false
if [[ ${1:-} == --from-ubuntu ]]; then
    [[ $# -ge 3 && $# -le 4 ]] || { echo "Usage: $0 --from-ubuntu NEW_WORK_DIR IMAGE_TAG [ENV_SPECS_JSON]" >&2; exit 2; }
    download=true
    work=$2
    tag=$3
    specs=${4:-$root/historical/env_specs.json}
else
    [[ $# -ge 4 && $# -le 5 ]] || { echo "Usage: $0 MINICONDA_INSTALLER SHA256 NEW_WORK_DIR IMAGE_TAG [ENV_SPECS_JSON]" >&2; exit 2; }
    installer=$(realpath "$1")
    checksum=$2
    work=$3
    tag=$4
    specs=${5:-$root/historical/env_specs.json}
    [[ $checksum =~ ^[a-fA-F0-9]{64}$ ]] || { echo 'SHA256 must be 64 hex characters' >&2; exit 2; }
fi
[[ ! -e $work && ! -L $work ]] || { echo "Refusing existing work directory: $work" >&2; exit 1; }
docker info >/dev/null
if docker image inspect "$tag" >/dev/null 2>&1; then
    echo "Refusing to replace existing image tag: $tag" >&2; exit 1
fi
mkdir -p "$work"
if $download; then
    checksum=$(python3 "$root/scripts/fetch_miniconda.py" "$work/miniconda.sh")
else
    cp "$installer" "$work/miniconda.sh"
fi
cp "$specs" "$work/env_specs.json"
cp "$root/historical/install_envs.py" "$root/scripts/install_envs_strict.py" "$work/"
cp "$root/Dockerfile.master" "$work/Dockerfile"
docker build --platform linux/amd64 --progress plain \
    --build-arg "MINICONDA_SHA256=$checksum" \
    --build-arg "UBUNTU_IMAGE=${UBUNTU_IMAGE:-ubuntu:24.04}" \
    --build-arg "CRIU_REF=${CRIU_REF:-30acbabcd}" \
    --build-arg "CONDA_CHANNEL=${CONDA_CHANNEL:-conda-forge}" \
    -t "$tag" "$work" 2>&1 | tee "$work/build.log"
docker image inspect "$tag" --format '{{.Id}} {{json .RepoDigests}}' > "$work/image-id.txt"
echo "Built master OCI image $tag. Next: build_xfs.py oci --source $tag ..."
