"""Portable paths, manifests, and strict measurement utilities."""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import signal
from pathlib import Path
import statistics
import subprocess
from typing import Any

AE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = AE_ROOT.parent


def install_termination_handler() -> None:
    """Let owned-resource finally blocks run when an outer runner times out."""
    def interrupted(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt(f'Experiment interrupted by signal {signum}')
    signal.signal(signal.SIGTERM, interrupted)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    tmp.replace(path)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def file_record(path: Path) -> dict:
    return {'path': str(path.resolve()), 'sha256': digest(path), 'bytes': path.stat().st_size}


def jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open() as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise ValueError(f'{path}:{number}: invalid JSON') from exc
            if not isinstance(row, dict):
                raise ValueError(f'{path}:{number}: expected an object')
            rows.append(row)
    if not rows:
        raise ValueError(f'Empty JSONL: {path}')
    return rows


def number(value: Any, label: str, *, nonnegative: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f'{label}: expected finite numeric measurement, got {value!r}')
    if nonnegative and value < 0:
        raise ValueError(f'{label}: negative measurement {value}')
    return float(value)


def stats(values: list[float]) -> dict:
    if not values:
        raise ValueError('No measurements; refusing to substitute a reference value')
    xs = sorted(number(v, 'sample') for v in values)
    rank = (len(xs) - 1) * .95
    lower = int(rank)
    p95 = xs[lower] + (xs[min(lower + 1, len(xs) - 1)] - xs[lower]) * (rank - lower)
    return {'n': len(xs), 'mean': statistics.fmean(xs), 'median': statistics.median(xs),
            'p95': p95, 'min': xs[0], 'max': xs[-1]}


def public_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: '<redacted>' if any(word in key.lower() for word in ('password', 'secret', 'api_key', 'token'))
                else public_config(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_config(item) for item in value]
    return value


def load_config(path: Path | None) -> dict:
    if path is None:
        return {}
    config = read_json(path)
    if not isinstance(config, dict):
        raise ValueError('Experiment config must be a JSON object')
    def expand(value):
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return os.path.expandvars(value) if isinstance(value, str) else value
    config = expand(config)
    config['_config_dir'] = str(path.resolve().parent)
    return config


def configured_value(config: dict, name: str):
    value = config
    for part in name.split('.'):
        value = value.get(part) if isinstance(value, dict) else None
    if value is None or value == '' or isinstance(value, str) and '$' in value:
        raise ValueError(f'Missing or unresolved configuration: {name}')
    return value


def run_purpose(default: str) -> str:
    value = os.environ.get('AE_RUN_PURPOSE', default)
    if value not in ('smoke', 'full-trace', 'full-cohort'):
        raise ValueError('Invalid AE_RUN_PURPOSE')
    return value


def configured_path(config: dict, name: str, *, required: bool = True) -> Path | None:
    value: Any = config
    for part in name.split('.'):
        value = value.get(part) if isinstance(value, dict) else None
    if value is None or value == '':
        if required:
            raise ValueError(f'Missing configuration: {name}')
        return None
    if not isinstance(value, str):
        raise ValueError(f'{name} must be a path string')
    expanded = os.path.expandvars(os.path.expanduser(value))
    if '$' in expanded:
        raise ValueError(f'Unresolved environment variable in {name}')
    path = Path(expanded)
    return path if path.is_absolute() else Path(config.get('_config_dir', '.')) / path


def repository_state() -> dict:
    def git(*args):
        return subprocess.check_output(['git', '-C', str(REPO_ROOT), *args], text=True).strip()
    return {'commit': git('rev-parse', 'HEAD'), 'branch': git('rev-parse', '--abbrev-ref', 'HEAD'),
            'tracked_diff_sha256': hashlib.sha256(subprocess.check_output(
                ['git', '-C', str(REPO_ROOT), 'diff', 'HEAD', '--binary'])).hexdigest(),
            'status': git('status', '--porcelain=v1', '--untracked-files=normal')}


def host_state() -> dict:
    state = {'platform': platform.platform(), 'machine': platform.machine(),
             'python': platform.python_version(), 'cpu_count': os.cpu_count()}
    if hasattr(os, 'sched_getaffinity'):
        state['cpu_affinity'] = sorted(os.sched_getaffinity(0))
    cpuinfo = Path('/proc/cpuinfo')
    if cpuinfo.exists():
        state['cpu_model'] = next((line.split(':',1)[1].strip() for line in cpuinfo.read_text().splitlines()
                                   if line.startswith('model name')), None)
    for label in ('scaling_governor','scaling_min_freq','scaling_max_freq'):
        path = Path('/sys/devices/system/cpu/cpu0/cpufreq') / label
        state[label] = path.read_text().strip() if path.exists() else None
    status = Path('/proc/self/status')
    if status.exists():
        state['numa_allowed'] = next((line.split(':',1)[1].strip() for line in status.read_text().splitlines()
                                      if line.startswith('Mems_allowed_list:')), None)
    return state


def artifact_records(root: Path, paths) -> list[dict]:
    records = []
    for path in sorted(paths):
        path = Path(path)
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f'Artifact escapes owned output: {path}')
        records.append({'path': str(path.relative_to(root)), 'sha256': digest(path), 'bytes': path.stat().st_size})
    if not records:
        raise ValueError('No measured artifacts')
    return records
