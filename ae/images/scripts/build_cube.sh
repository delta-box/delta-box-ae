#!/usr/bin/env bash
# Build a local OCI image; this script does not publish to a registry.
set -euo pipefail
[[ $# == 1 ]] || { echo "Usage: $0 NEW_OCI_IMAGE_TAG" >&2; exit 2; }
root=$(cd "$(dirname "$0")/.." && pwd)
if docker image inspect "$1" >/dev/null 2>&1; then
    echo 'Refusing to replace an existing image tag' >&2; exit 1
fi
# Empty build context ensures traces, source downloads, keys and images aren't sent.
docker build --platform linux/amd64 \
    --build-arg "CUBE_BASE_IMAGE=${CUBE_BASE_IMAGE:-ghcr.io/tencentcloud/cubesandbox-base:2026.16}" \
    -t "$1" - < "$root/Dockerfile.cube"
docker image inspect "$1" --format '{{.Id}} {{json .RepoDigests}}'
