#!/usr/bin/env python3
"""Run recovered named assertions inside a disposable patched-kernel VM."""
import json
import os
from pathlib import Path
import re
import subprocess

out = Path('/tmp/ae-output'); out.mkdir(exist_ok=True)
# Read static superblock feature flags directly. Historical guests omit the root
# entry in /etc/mtab, so xfs_info cannot resolve even the /dev/vda alias reliably.
fs = subprocess.check_output(['xfs_db', '-r', '-c', 'version', '/dev/vda'], text=True)
if 'REFLINK' not in fs:
    raise RuntimeError('Correctness suite requires root XFS reflink=1')
(out / 'filesystem-features.txt').write_text(fs)
rows = []
for name in ['test_full.sh', 'test_cross_checkpoint_fd_cow.sh', 'test_deleted_open_resurrect.sh']:
    env = dict(os.environ, WORK_BASE='/tmp/ae-test-' + name.removesuffix('.sh'))
    result = subprocess.run(['bash', '/app/agentfs/' + name], env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (out / (name + '.log')).write_text(result.stdout)
    plain = re.sub(r'\x1b\[[0-9;]*m', '', result.stdout)
    assertions = [{'status': s.lower(), 'label': label.strip()} for s, label in re.findall(r'\[(PASS|FAIL|WARN)\]\s*([^\n]*)', plain)]
    rows.append({'suite': name, 'returncode': result.returncode, 'assertions': assertions,
                 'ok': result.returncode == 0 and bool(assertions) and not any(a['status'] == 'fail' for a in assertions)})
(out / 'correctness.json').write_text(json.dumps({'rows': rows, 'ok': all(r['ok'] for r in rows),
    'scope': 'Recovered named assertions; final paper 53-case manifest was not recovered. Summary PASS lines are not counted as distinct tests.'}, indent=2))
raise SystemExit(0 if all(r['ok'] for r in rows) else 1)
