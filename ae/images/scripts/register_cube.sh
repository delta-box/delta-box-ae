#!/usr/bin/env bash
# Requires a prepared CubeSandbox v0.3.0 cluster and an accessible OCI reference.
set -euo pipefail
[[ $# == 3 ]] || { echo "Usage: $0 PUBLISHED_OCI_IMAGE NEW_TEMPLATE_ID NEW_OUTPUT_DIR" >&2; exit 2; }
image=$1; template=$2; out=$3
[[ ! -e $out && ! -L $out ]] || { echo 'Refusing existing output directory' >&2; exit 1; }
mkdir -p "$out"
# v0.3.0 docker/Dockerfile.cube-base exposes envd /health on port 49983.
cubemastercli tpl create-from-image --image "$image" --template-id "$template" \
    --writable-layer-size 8G --cpu "${CUBE_CPU_MILLICORES:-4000}" \
    --memory "${CUBE_MEMORY_MIB:-4096}" --expose-port 49983 --probe 49983 \
    --probe-path /health --json > "$out/create.json"
job=$(jq -er '.job.job_id' "$out/create.json")
# v0.3.0 watch mixes progress lines with its final JSON; status returns clean JSON.
cubemastercli tpl watch --job-id "$job" > "$out/watch.log"
cubemastercli tpl status --job-id "$job" --json > "$out/ready.json"
jq -e '.job.status == "READY"' "$out/ready.json" >/dev/null
cubemastercli tpl info --template-id "$template" --include-request --json > "$out/template.json"
echo "Template ready: $template"
