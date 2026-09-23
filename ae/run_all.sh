#!/usr/bin/env bash
# One command for preparation, current CPU/GPU experiments, analysis, and comparison.
set -euo pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -n ${AE_HOSTED_LAUNCHER:-} ]] && (( EUID != 0 )); then
    exec sudo -n -- "$AE_HOSTED_LAUNCHER" --checkout "$repo" "$@"
fi
args=()
runtime_repo=
while (($#)); do
    case "$1" in
        --runtime-repo)
            if (($# < 2)) || [[ -n "$runtime_repo" ]]; then
                echo 'Use --runtime-repo PATH exactly once.' >&2
                exit 2
            fi
            runtime_repo=$2
            shift 2
            ;;
        *) args+=("$1"); shift ;;
    esac
done
if [[ -n "$runtime_repo" ]]; then
    if [[ ! -f "$runtime_repo/replay/run_instance.py" || ! -f "$runtime_repo/release/lock.py" || ! -f "$runtime_repo/ae/scripts/run_review.py" || ! -f "$runtime_repo/ae/run_all.sh" ]]; then
        echo "Not a complete DeltaBox AE runtime checkout: $runtime_repo" >&2
        exit 2
    fi
    runtime_repo=$(cd "$runtime_repo" && pwd)
    echo "Runtime checkout: $runtime_repo"
    exec bash "$runtime_repo/ae/run_all.sh" "${args[@]}"
fi
if [[ ! -f "$repo/replay/run_instance.py" || ! -f "$repo/release/lock.py" || ! -f "$repo/ae/scripts/run_review.py" ]]; then
    echo "This checkout lacks the complete DeltaBox replay runtime: $repo" >&2
    echo 'Select a complete checkout explicitly with --runtime-repo PATH.' >&2
    exit 2
fi
python=${AE_PYTHON:-$repo/.venv/bin/python}
if [[ ! -x "$python" ]] && [[ -z ${AE_PYTHON:-} ]]; then
    python=python3
fi
exec "$python" "$repo/ae/scripts/run_review.py" "${args[@]}"
