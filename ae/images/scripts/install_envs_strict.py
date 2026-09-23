#!/usr/bin/env python3
"""Wrap the recovered installer: dependency failures or skipped specs are fatal."""
import importlib.util
import json
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location('historical_installer', Path(__file__).with_name('install_envs.py'))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
original_run = installer.run
failures = []


def strict_run(cmd, cwd=None, check=True, timeout=900):
    # A failed reflink probe is expected on a Docker build filesystem.
    optional = cmd.startswith(('cp --reflink=always ', 'git fetch ',
                               '/opt/miniconda3/bin/conda clean ', 'apt-get clean '))
    try:
        return original_run(cmd, cwd=cwd, check=not optional, timeout=timeout)
    except Exception:
        failures.append(cmd)
        raise


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('usage: install_envs_strict.py env_specs.json')
    installer.run = strict_run
    installer.main()
    specs = json.loads(Path(sys.argv[1]).read_text())
    missing = []
    for item in specs.values():
        env = item['repo'].split('/')[1] + '__' + item['version']
        if not Path('/testbed', env).is_dir() or not Path('/opt/miniconda3/envs', env, 'bin/python').is_file():
            missing.append(env)
    if failures or missing:
        raise SystemExit(f'Incomplete master: {len(failures)} failed commands, missing specs: {missing}')
