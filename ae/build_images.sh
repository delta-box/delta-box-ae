#!/usr/bin/env bash
set -euo pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
python=${AE_PYTHON:-python3}
exec "$python" "$repo/ae/images/scripts/build_bundle.py" "$@"
