#!/usr/bin/env bash
# One CPU selection and option parser for both fixed NUMA layouts.
set -euo pipefail
ae_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
layout=${1:-}
case "$layout" in
    numa12) nodes=1/2; control_cpus=32-35; control_node=1; entry=run_all_no_gpu.sh ;;
    numa03) nodes=0/3; control_cpus=4-7; control_node=0; entry=run_all_no_gpu_numa03.sh ;;
    *) echo 'CPU entry requires the fixed numa12 or numa03 layout.' >&2; exit 2 ;;
esac
shift
args=(--group cpu --cpu-parallel)
resume_failures=0
if [[ $layout == numa03 ]]; then args+=(--cpu-layout numa03); fi
while (($#)); do
    case "$1" in
        -h|--help)
            cat <<HELP
Usage: bash ae/$entry [options]

Run all 16 normal CPU experiment groups on NUMA$nodes with the original job caps.
Idle nodes claim the next experiment; input jobs within each group stay serial.
Cube/E2B service changes never overlap. Results feed one combined report.
Figure 8(a) remains included. GPU probing and Figure 8(b)(c) are skipped.

Options forwarded unchanged to the normal entry:
  --output PATH         New result directory
  --resume PATH         Resume a previous two-lane run
  --limit N             Input limit; configured job cap still applies
  --resume-failures N   Hosted NUMA1/2 only: 0..3 resumes after verified cleanup
  --max-events N        Explicit event prefix
  --baseline-inputs 44|all
  --list                Show the shared experiment catalogue without running
HELP
            if [[ $layout == numa03 ]]; then
                cat <<'HELP'

NUMA0 CPU0-3 and NUMA3 CPU72-75; hosted reviewer requests priority.
The background entry uses the installed protected hosted launcher, cleans up its
owned experiment, then resumes verified results after review without reducing inputs.
HELP
            else
                cat <<'HELP'

NUMA1 CPU28-31 and NUMA2 CPU48-51; reviewer work has priority.
  --config PATH --runtime-repo PATH   Self-managed options; hosted rules apply
HELP
            fi
            echo 'Placement and experiment selection are fixed. Use ae/run_test.sh for a quick test.'
            exit 0
            ;;
        --resume-failures|--resume-failures=*)
            if [[ $1 == *=* ]]; then value=${1#*=}; shift
            else
                if (($# < 2)); then echo 'Missing --resume-failures value' >&2; exit 2; fi
                value=$2; shift 2
            fi
            if [[ $layout != numa12 || ! $value =~ ^[0-3]$ ]]; then
                echo '--resume-failures requires hosted NUMA1/2 and a value 0..3' >&2; exit 2
            fi
            if (( value > 0 )); then args+=(--resume-failures "$value"); fi
            resume_failures=$value
            ;;
        --config|--config=*|--runtime-repo)
            if [[ $layout != numa12 ]]; then
                echo "Unsupported option: $1. NUMA0/3 requires the fixed hosted configuration." >&2
                exit 2
            fi
            if [[ $1 == --config=* ]]; then args+=("$1"); shift; continue; fi
            if (($# < 2)) || [[ $2 == --* ]]; then
                echo "Missing value for $1" >&2; exit 2
            fi
            args+=("$1" "$2"); shift 2
            ;;
        --output|--resume|--limit|--max-events|--baseline-inputs)
            if (($# < 2)) || [[ $2 == --* ]]; then
                echo "Missing value for $1" >&2; exit 2
            fi
            args+=("$1" "$2"); shift 2
            ;;
        --output=*|--resume=*|--limit=*|--max-events=*|--baseline-inputs=*|--list)
            args+=("$1"); shift
            ;;
        *)
            echo "Unsupported option: $1. This entry fixes CPU selection and NUMA$nodes; see --help." >&2
            exit 2
            ;;
    esac
done
launcher=${AE_HOSTED_LAUNCHER:-}
if (( resume_failures > 0 )); then
    if [[ -n $launcher && $launcher != /usr/local/sbin/deltabox-ae-run ]]; then
        echo 'Failure resumes require the protected hosted launcher' >&2; exit 2
    fi
    launcher=/usr/local/sbin/deltabox-ae-run
fi
if [[ $layout == numa03 ]]; then
    launcher=${launcher:-/usr/local/sbin/deltabox-ae-run}
    if [[ $launcher != /usr/local/sbin/deltabox-ae-run ]]; then
        echo 'NUMA0/3 background validation requires /usr/local/sbin/deltabox-ae-run for reviewer priority.' >&2
        exit 2
    fi
fi
command=(bash "$ae_dir/run_all.sh")
# Preserve the reviewer's existing root/self-managed dispatch. The background
# entry always takes the protected launcher, including explicit root invocations.
if [[ $layout == numa03 ]] || (( resume_failures > 0 )) || { [[ -n $launcher ]] && (( EUID != 0 )); }; then
    repo=$(cd "$ae_dir/.." && pwd)
    command=(sudo -n -- "$launcher" --checkout "$repo")
fi
exec numactl --all --physcpubind="$control_cpus" --membind="$control_node" "${command[@]}" "${args[@]}"
