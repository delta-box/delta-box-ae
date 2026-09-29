#!/usr/bin/env bash
# Fixed two-lane CPU validation; reuse the normal hosted entry and runners.
set -euo pipefail
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
args=(--group cpu --cpu-parallel)
while (($#)); do
    case "$1" in
        -h|--help)
            cat <<'HELP'
Usage: bash ae/run_all_no_gpu.sh [options]

Run the normal bounded CPU experiments in two concurrent lanes:
  NUMA1, CPU28-31: DeltaBox, profiling, Figure 9 and correctness
  NUMA2, CPU48-51: baselines, including Cube and E2B
Input jobs within each lane run sequentially. Results feed one combined report.
Figure 8(a) remains included. GPU probing and Figure 8(b)(c) are skipped.

Options forwarded unchanged to the normal entry:
  --output PATH         New result directory
  --resume PATH         Resume a previous two-lane run
  --limit N             Input limit; configured job cap still applies
  --max-events N        Explicit event prefix
  --baseline-inputs 44|all
  --list                Show the shared experiment catalogue without running
  --config PATH --runtime-repo PATH   Self-managed options; hosted rules apply

Placement and experiment selection are fixed. Use ae/run_test.sh for a quick test.
HELP
            exit 0
            ;;
        --output|--resume|--limit|--max-events|--baseline-inputs|--config|--runtime-repo)
            if (($# < 2)) || [[ $2 == --* ]]; then
                echo "Missing value for $1" >&2
                exit 2
            fi
            args+=("$1" "$2")
            shift 2
            ;;
        --output=*|--resume=*|--limit=*|--max-events=*|--baseline-inputs=*|--config=*|--list)
            args+=("$1")
            shift
            ;;
        *)
            echo "Unsupported option: $1. This entry fixes CPU selection and NUMA1/2; see --help." >&2
            exit 2
            ;;
    esac
done
exec numactl --all --physcpubind=32-35 --membind=1 bash "$script_dir/run_all.sh" "${args[@]}"
