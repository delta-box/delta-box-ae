#!/usr/bin/env bash
# Check one DeltaBox input before running the complete experiments.
set -euo pipefail
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec bash "$script_dir/run_all.sh" --test "$@"
