#!/usr/bin/env bash
# Fixed CPU layout; the shared helper owns options and execution.
set -euo pipefail
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec bash "$script_dir/scripts/run_no_gpu_entry.sh" numa03 "$@"
