#!/usr/bin/env bash
# Figure 9: all 185 inputs, RAM-backed storage, NUMA 2, maximum P-state.
set -euo pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python=${AE_PYTHON:-$repo/.venv/bin/python}
if [[ ! -x "$python" ]]; then python=python3; fi
exec "$python" "$repo/ae/scripts/run_figure09_memory.py" "$@"
