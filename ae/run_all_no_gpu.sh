#!/usr/bin/env bash
# Keep the normal one-click workflow, selecting only its CPU experiments.
set -euo pipefail
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
args=(--group cpu)
while (($#)); do
    case "$1" in
        -h|--help)
            cat <<'HELP'
Usage: bash ae/run_all_no_gpu.sh [options]

Run the same bounded, sequential CPU experiments and reports as run_all.sh.
Skip GPU admission, Figure 8(b), and the GPU-dependent Figure 8(c).
Figure 8(a) CPU fan-out remains included. No NUMA node is hardcoded.

Options forwarded unchanged to run_all.sh:
  --output PATH         New result directory
  --resume PATH         Resume a previous CPU-only run
  --limit N             Input limit (the configured job cap still applies)
  --max-events N        Explicit event prefix
  --baseline-inputs 44|all
  --numa-node N --cpus LIST
  --list                Show the shared entry's complete experiment catalogue
  --config PATH --experiment-config EXPERIMENT=PATH
  --runtime-repo PATH --no-pin --available
                       Self-managed entry options; hosted restrictions still apply

Experiment selection is fixed to --group cpu. Use ae/run_test.sh for the quick test.
HELP
            exit 0
            ;;
        --output|--resume|--limit|--max-events|--baseline-inputs|--numa-node|--cpus|--config|--experiment-config|--runtime-repo)
            if (($# < 2)) || [[ $2 == --* ]]; then
                echo "Missing value for $1" >&2
                exit 2
            fi
            args+=("$1" "$2")
            shift 2
            ;;
        --output=*|--resume=*|--limit=*|--max-events=*|--baseline-inputs=*|--numa-node=*|--cpus=*|--config=*|--experiment-config=*)
            args+=("$1")
            shift
            ;;
        --list|--no-pin|--available)
            args+=("$1")
            shift
            ;;
        *)
            echo "Unsupported option: $1. This entry fixes selection to --group cpu; see --help." >&2
            exit 2
            ;;
    esac
done
exec bash "$script_dir/run_all.sh" "${args[@]}"
