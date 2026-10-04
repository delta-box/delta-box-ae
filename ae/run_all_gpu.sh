#!/usr/bin/env bash
# GPU-only Figure 8(b), using the existing remote admission and result pipeline.
set -euo pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
args=()
devices=0,3,6,7
device_seen=0
case_seen=0
output_seen=0
while (($#)); do
    option=${1%%=*}
    case "$option" in
        -h|--help)
            cat <<'HELP'
Usage: bash ae/run_all_gpu.sh [--output PATH] [--gpu-devices ID,...]

Run Figure 8(b) on the configured allinai2plus host, then produce Figure 8(c).
Defaults: physical GPUs 0,3,6,7; all eight generation/training cases;
a new timestamped result directory. Four idle GPUs cover the full matrix.
Busy GPUs are excluded; no fallback to devices outside the allowlist.
Fewer than four idle GPUs may produce a partial result, never full success.
At completion, print the eight-case summary and exact SUMMARY.md path.

Options:
  --output PATH       New result directory.
  --resume PATH       Explicitly continue an existing GPU-only result directory.
  --gpu-devices ID,... Physical GPU indices 0–7 (default: 0,3,6,7).
  --gpu-cases CASE,... Explicit case subset, e.g. training-B16,training-B64.
  --help              Show help without contacting the GPU host.

As in the paper, Figure 8(c) applies Equation 1 to these GPU times and the
fan-out times of the newest finished ae/run_all_no_gpu.sh run under ae/results.
HELP
            exit 0
            ;;
        --output|--resume|--gpu-devices|--gpu-cases)
            if [[ $1 == *=* ]]; then
                value=${1#*=}; shift
            else
                if (($# < 2)) || [[ -z $2 || $2 == --* ]]; then
                    echo "$option requires a value." >&2; exit 2
                fi
                value=$2; shift 2
            fi
            [[ -n $value ]] || { echo "$option requires a value." >&2; exit 2; }
            case "$option" in
                --gpu-devices)
                    ((device_seen == 0)) || { echo 'Use --gpu-devices exactly once.' >&2; exit 2; }
                    device_seen=1; devices=$value
                    ;;
                --gpu-cases)
                    ((case_seen == 0)) || { echo 'Use --gpu-cases exactly once.' >&2; exit 2; }
                    case_seen=1; args+=("$option" "$value")
                    ;;
                *)
                    ((output_seen == 0)) || { echo 'Use only one of --output or --resume.' >&2; exit 2; }
                    output_seen=1; args+=("$option" "$value")
                    ;;
            esac
            ;;
        *) echo "Unsupported GPU-only option: $1. See --help." >&2; exit 2 ;;
    esac
done
if ((output_seen == 0)); then
    args+=(--output "$repo/ae/results/selected/gpu-only-$(date -u +%Y%m%dT%H%M%SZ)-$$")
fi
exec bash "$repo/ae/run_all.sh" --group gpu --gpu-devices "$devices" "${args[@]}"
