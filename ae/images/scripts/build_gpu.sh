#!/usr/bin/env bash
set -euo pipefail
[[ $# == 4 ]] || { echo "Usage: $0 CUDA_TORCH_BASE_AT_SHA256 REQUIREMENTS_LOCK NEW_WORK_DIR NEW_IMAGE_TAG" >&2; exit 2; }
base=$1; lock=$2; work=$3; tag=$4
[[ $base == *@sha256:* ]] || { echo 'Provide a digest-pinned CUDA/PyTorch base image' >&2; exit 2; }
[[ ! -e $work && ! -L $work ]] || { echo 'Refusing existing work directory' >&2; exit 1; }
if docker image inspect "$tag" >/dev/null 2>&1; then echo 'Image tag exists' >&2; exit 1; fi
root=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$work"
cp "$lock" "$work/requirements-gpu.lock"
cp "$root/Dockerfile.gpu" "$work/Dockerfile"
docker build --platform linux/amd64 --build-arg "BASE_IMAGE=$base" -t "$tag" "$work" 2>&1 | tee "$work/build.log"
docker image inspect "$tag" --format '{{.Id}} {{json .RepoDigests}}' > "$work/image-id.txt"
