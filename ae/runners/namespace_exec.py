#!/usr/bin/env python3
"""Enable loopback inside the caller-owned network namespace, then exec argv."""
import os
import subprocess
import sys
if len(sys.argv) < 2:
    raise SystemExit('missing command')
subprocess.run(['ip', 'link', 'set', 'lo', 'up'], check=True)
os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
