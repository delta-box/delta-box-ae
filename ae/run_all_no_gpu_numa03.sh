#!/usr/bin/env bash
# Background NUMA0/3 CPU run; reuse the normal hosted entry and runners.
set -euo pipefail
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
args=(--group cpu --cpu-parallel --cpu-layout numa03)
while (($#)); do
    case "$1" in
        -h|--help)
            cat <<'HELP'
Usage: bash ae/run_all_no_gpu_numa03.sh [options]

Run all 16 normal CPU experiment groups with the original configured job caps:
  NUMA0, CPU0-3: claims the next pending experiment
  NUMA3, CPU72-75: claims the next pending experiment
Idle nodes claim the next experiment; configured input concurrency is unchanged.
Cube/E2B service changes never overlap. Results feed one combined report.
The hosted background runner gives reviewer requests priority: it cleans up its
owned experiment, then resumes verified results in the same output after review.
Inputs are not automatically reduced when the background run yields.
Figure 8(a) remains included. GPU probing and Figure 8(b)(c) are skipped.

Options forwarded unchanged to the normal entry:
  --output PATH         New result directory
  --resume PATH         Resume a previous two-lane run
  --limit N             Input limit; configured job cap still applies
  --max-events N        Explicit event prefix
  --baseline-inputs 44|all
  --list                Show the shared experiment catalogue without running

This background entry always uses the installed protected hosted launcher.

Placement and experiment selection are fixed. Use ae/run_test.sh for a quick test.
HELP
            exit 0
            ;;
        --output|--resume|--limit|--max-events|--baseline-inputs)
            if (($# < 2)) || [[ $2 == --* ]]; then
                echo "Missing value for $1" >&2
                exit 2
            fi
            args+=("$1" "$2")
            shift 2
            ;;
        --output=*|--resume=*|--limit=*|--max-events=*|--baseline-inputs=*|--list)
            args+=("$1")
            shift
            ;;
        *)
            echo "Unsupported option: $1. This entry fixes CPU selection and NUMA0/3; see --help." >&2
            exit 2
            ;;
    esac
done
launcher=${AE_HOSTED_LAUNCHER:-/usr/local/sbin/deltabox-ae-run}
if [[ $launcher != /usr/local/sbin/deltabox-ae-run ]]; then
    echo 'NUMA0/3 background validation requires /usr/local/sbin/deltabox-ae-run for reviewer priority.' >&2
    exit 2
fi
repo=$(cd "$script_dir/.." && pwd)
exec numactl --all --physcpubind=4-7 --membind=0 sudo -n -- "$launcher" --checkout "$repo" "${args[@]}"
