#!/usr/bin/env python3
"""ae/runners/baseline.py unchanged, except that its driver tree comes from ./vendor (the cold-cache patch of make_vendor.py)."""
import importlib.util, sys
from pathlib import Path
A = Path('/home/atc-ae/delta-box-ae/ae')
sys.path[:0] = [str(A.parent), str(A), str(A / 'scripts'), str(A / 'runners')]
spec = importlib.util.spec_from_file_location('baseline', A / 'runners/baseline.py')
m = importlib.util.module_from_spec(spec); sys.modules['baseline'] = m; spec.loader.exec_module(m)
m.VENDOR = Path(__file__).resolve().parent / 'vendor'
from repro.common import install_termination_handler  # noqa: E402
install_termination_handler()
raise SystemExit(m.main())
