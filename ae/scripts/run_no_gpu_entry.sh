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
resume_failures=
limit_set=0
output_set=0
resume_set=0
list_only=0
self_managed=0
hosted_reviewer=0
if [[ -n ${AE_HOSTED_LAUNCHER:-} ]] && (( EUID != 0 )); then
    hosted_reviewer=1
fi
if [[ $layout == numa03 ]]; then args+=(--cpu-layout numa03); fi
while (($#)); do
    case "$1" in
        -h|--help)
            cat <<HELP
Usage: bash ae/$entry [options]

Run all 16 normal CPU experiment groups on NUMA$nodes (default input limit: 3).
Idle nodes claim the next experiment; input jobs within each group stay serial.
Cube/E2B service changes never overlap. Results feed one combined report.
Figure 8(a) remains included. GPU probing and Figure 8(b)(c) are skipped.

Options forwarded unchanged to the normal entry:
  --output PATH         New result directory (generated automatically if omitted)
  --resume PATH         Resume a previous two-lane run
  --limit N             Input limit (default: 3); configured job cap still applies
  --resume-failures N   Hosted NUMA1/2 only: 0..3 resumes (default: 3 for fresh runs)
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
HELP
                if (( hosted_reviewer )); then
                    echo 'Hosted mode uses a fixed configuration and runtime checkout.'
                    echo '--config and --runtime-repo are unavailable in hosted mode.'
                else
                    cat <<'HELP'
Self-managed options (unavailable in hosted mode):
  --config PATH         Select the experiment configuration
  --runtime-repo PATH   Select a complete runtime checkout
HELP
                fi
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
            if [[ -n $resume_failures ]]; then
                echo 'Use --resume-failures exactly once.' >&2; exit 2
            fi
            resume_failures=$value
            ;;
        --config|--config=*|--runtime-repo)
            if (( hosted_reviewer )); then
                echo "${1%%=*} is only available in self-managed mode; hosted mode uses a fixed configuration and runtime checkout." >&2
                exit 2
            fi
            self_managed=1
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
            case "$1" in
                --limit) limit_set=1 ;;
                --output) output_set=1 ;;
                --resume) resume_set=1 ;;
            esac
            args+=("$1" "$2"); shift 2
            ;;
        --output=*|--resume=*|--limit=*|--max-events=*|--baseline-inputs=*|--list)
            case "$1" in
                --limit=*) limit_set=1 ;;
                --output=*) output_set=1 ;;
                --resume=*) resume_set=1 ;;
                --list) list_only=1 ;;
            esac
            args+=("$1"); shift
            ;;
        *)
            echo "Unsupported option: $1. This entry fixes CPU selection and NUMA$nodes; see --help." >&2
            exit 2
            ;;
    esac
done
# Defaults belong to fresh hosted reviewer campaigns. Listing, explicit manual
# resume and self-managed overrides must not silently acquire a retry budget.
if (( ! limit_set )); then args+=(--limit 3); fi
if [[ -z $resume_failures ]]; then
    resume_failures=0
    if [[ $layout == numa12 ]] && (( ! resume_set && ! list_only && ! self_managed )); then
        resume_failures=3
    fi
fi
if (( resume_failures > 0 )); then
    if (( resume_set || list_only || self_managed )); then
        echo 'Failure resumes require a fresh hosted run without --resume, --list or self-managed overrides.' >&2
        exit 2
    fi
    if (( ! output_set )); then
        repo=$(cd "$ae_dir/.." && pwd)
        # Do not pre-create the directory: admission and the output lock own it.
        args+=(--output "$repo/ae/results/selected/$layout-$(date -u +%Y%m%dT%H%M%SZ)-$$")
    fi
    args+=(--resume-failures "$resume_failures")
fi
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
