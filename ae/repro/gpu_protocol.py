"""Portable Figure 8 GPU recipe and strict result contracts; no GPU imports."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

from .common import AE_ROOT, file_record, number, stats

DEFAULT_CONFIG = AE_ROOT / 'configs/figure08-gpu.json'
BATCHES = (1, 4, 16, 64)
PHASES = ('generation', 'training')


def positive_int(value, name, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < (0 if zero else 1):
        raise ValueError(f'{name} must be an integer >= {0 if zero else 1}')
    return value


def parse_devices(value):
    if value is None or value == '':
        return []
    items = value.split(',') if isinstance(value, str) else value
    if not isinstance(items, list):
        raise ValueError('devices must be a list or comma-separated IDs')
    result = [str(item).strip() for item in items]
    if len(set(result)) != len(result) or any(
        not re.fullmatch(r'(?:\d+|GPU-[A-Za-z0-9_-]+)', item) for item in result):
        raise ValueError('devices must be unique full-GPU NVIDIA indices or UUIDs')
    return result


def load_config(path=DEFAULT_CONFIG, **overrides):
    defaults = json.loads(DEFAULT_CONFIG.read_text())
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict) or set(raw) - set(defaults):
        raise ValueError('unknown or invalid GPU configuration fields')
    config = copy.deepcopy(defaults)
    for key, value in raw.items():
        if key in ('generation', 'training'):
            if not isinstance(value, dict) or set(value) - set(defaults[key]):
                raise ValueError(f'unknown or invalid {key} fields')
            config[key].update(value)
        else:
            config[key] = value
    for key, value in overrides.items():
        if value is None:
            continue
        if key == 'prompt_mode':
            config['generation']['prompt_mode'] = value
        elif key in ('quick_check', 'smoke'):
            if value:
                config['batches'] = [1]
                for phase in ('generation', 'training'):
                    config[phase]['reps'] = config[phase]['warmup_reps'] = 1
        elif key not in config:
            raise ValueError(f'unknown override: {key}')
        else:
            config[key] = value
    if not config['devices'] and os.environ.get('CUDA_VISIBLE_DEVICES'):
        config['devices'] = os.environ['CUDA_VISIBLE_DEVICES']
    config['devices'] = parse_devices(config['devices'])
    base = Path(path).resolve().parent
    for key in ('model_path', 'generation_python', 'training_python'):
        value = config[key]
        if value is None:
            if key.endswith('_python'):
                config[key] = sys.executable
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'{key} must be a path or null')
        value = os.path.expanduser(os.path.expandvars(value))
        if '$' in value:
            raise ValueError(f'unresolved {key}')
        # Preserve virtualenv executable symlinks rather than resolving to the
        # system Python and losing its installed GPU packages.
        config[key] = os.path.abspath(value if os.path.isabs(value) else base / value)
    validate_config(config)
    return config


def validate_config(config):
    if type(config.get('schema_version')) is not int or config['schema_version'] != 1:
        raise ValueError('unsupported GPU configuration schema')
    if not isinstance(config.get('model_label'), str) or not config['model_label'].strip():
        raise ValueError('model_label is required')
    for name, choices in (('batches', BATCHES), ('phases', PHASES)):
        values = config.get(name)
        if not isinstance(values, list) or not values:
            raise ValueError(f'{name} must be a nonempty unique list')
        expected_type = int if name == 'batches' else str
        if any(type(x) is not expected_type or x not in choices for x in values):
            raise ValueError(f'{name} must be selected from {choices}')
        if len(set(values)) != len(values):
            raise ValueError(f'{name} must be unique')
    positive_int(config['seed'], 'seed', zero=True)
    if config['seed'] >= 2 ** 32:
        raise ValueError('seed must be smaller than 2**32')
    if number(config['timeout_s'], 'timeout_s') <= 0:
        raise ValueError('timeout_s must be positive')
    if not isinstance(config['allow_busy'], bool):
        raise ValueError('allow_busy must be boolean')
    gen, train = config['generation'], config['training']
    if not isinstance(gen['enable_prefix_caching'], bool):
        raise ValueError('generation.enable_prefix_caching must be boolean')
    if gen['prompt_mode'] not in ('paper-template', 'fixed-tokens'):
        raise ValueError('prompt_mode must be paper-template or fixed-tokens')
    for key in ('in_tokens', 'out_tokens', 'max_model_len', 'reps'):
        positive_int(gen[key], 'generation.' + key)
    positive_int(gen['warmup_reps'], 'generation.warmup_reps', zero=True)
    if gen['max_model_len'] <= gen['in_tokens']:
        raise ValueError('max_model_len must leave room for generation')
    if gen['prompt_mode'] == 'fixed-tokens' and gen['max_model_len'] < gen['in_tokens'] + gen['out_tokens']:
        raise ValueError('fixed-tokens requires room for all requested tokens')
    if number(gen['temperature'], 'temperature') < 0:
        raise ValueError('temperature must be nonnegative')
    for key in ('top_p', 'gpu_memory_utilization'):
        value = number(gen[key], key)
        if not 0 < value <= 1:
            raise ValueError(f'{key} must be in (0,1]')
    for key in ('seq_len', 'lora_r', 'lora_alpha', 'fsdp_gpus', 'per_gpu_batch', 'reps'):
        positive_int(train[key], 'training.' + key)
    positive_int(train['warmup_reps'], 'training.warmup_reps', zero=True)
    if train['seq_len'] < 2 or train['fsdp_gpus'] < 2 or number(train['learning_rate'], 'learning_rate') <= 0:
        raise ValueError('training needs seq_len>=2, fsdp_gpus>=2 and positive learning_rate')
    for batch in config['batches']:
        if 'training' in config['phases'] and batch >= 16:
            divisor = train['fsdp_gpus'] * train['per_gpu_batch']
            if batch % divisor or batch < divisor:
                raise ValueError('effective training batch must equal GPUs * microbatch * accumulation')


def cases(config):
    result = []
    for phase in config['phases']:
        for batch in config['batches']:
            multi = phase == 'training' and batch >= 16
            gpus = config['training']['fsdp_gpus'] if multi else 1
            micro = config['training']['per_gpu_batch'] if multi else batch
            result.append(dict(case_id=f'{phase}-B{batch}', phase=phase, batch=batch,
                               num_gpus=gpus, per_gpu_batch=micro,
                               grad_accum=batch // (gpus * micro),
                               method='vllm-single' if phase == 'generation' else 'lora-fsdp' if multi else 'lora-single',
                               reps=config[phase]['reps'], warmup_reps=config[phase]['warmup_reps']))
    return result


def case_by_id(config, case_id):
    for case in cases(config):
        if case['case_id'] == case_id:
            return case
    raise ValueError(f'unknown case: {case_id}')


def child_environment(config, case, *, device_indices=None):
    env = os.environ.copy()
    for key in tuple(env):
        if key in ('RANK', 'LOCAL_RANK', 'WORLD_SIZE', 'LOCAL_WORLD_SIZE', 'GROUP_RANK',
                   'ROLE_RANK', 'ROLE_WORLD_SIZE', 'MASTER_ADDR', 'MASTER_PORT') or key.startswith('TORCHELASTIC_'):
            del env[key]
    selected = (config['devices'] if device_indices is None else device_indices)[:case['num_gpus']]
    if len(selected) != case['num_gpus'] or any(not isinstance(item, str) or not item.isdigit() for item in selected):
        raise ValueError(f"{case['case_id']} needs {case['num_gpus']} verified numeric GPU indices")
    env['CUDA_VISIBLE_DEVICES'] = ','.join(selected)
    env['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID'
    env['HF_HUB_OFFLINE'] = '1'
    # Selecting a Python executable alone does not expose its JIT build tools
    # (notably ninja) when the caller has not activated that environment.
    python_bin = str(Path(config[case['phase'] + '_python']).parent)
    env['PATH'] = python_bin + os.pathsep + env.get('PATH', '')
    return env


def command(config, case, config_path, result_path, worker_path):
    python = config[case['phase'] + '_python']
    argv = [python]
    if case['num_gpus'] > 1:
        argv += ['-m', 'torch.distributed.run', '--standalone', '--nnodes=1',
                 f"--nproc-per-node={case['num_gpus']}"]
    return argv + [str(worker_path), '--config', str(config_path), '--case', case['case_id'],
                   '--output', str(result_path)]


def model_identity(directory):
    directory = Path(directory)
    files = sorted(set(directory.glob('*.safetensors')) | set(directory.glob('pytorch_model*.bin')))
    if not files or not (directory / 'config.json').is_file():
        raise ValueError('local HF model requires config.json and safetensors/pytorch weight files')
    files += [p for p in sorted(directory.iterdir()) if p.is_file() and
              (p.suffix == '.json' or p.name in ('merges.txt', 'vocab.txt'))]
    records = []
    for path in sorted(set(files)):
        record = file_record(path)
        records.append(dict(name=path.name, sha256=record['sha256'], bytes=record['bytes']))
    encoded = json.dumps(records, sort_keys=True, separators=(',', ':')).encode()
    return dict(path=str(directory), sha256=hashlib.sha256(encoded).hexdigest(), files=records)


def validate_result(raw, case, config_sha256, devices):
    if not isinstance(raw, dict) or type(raw.get('schema_version')) is not int or raw.get('schema_version') != 1 or raw.get('kind') != 'gpu-timing-result' or raw.get('status') != 'ok':
        raise ValueError('worker did not return a successful GPU measurement')
    if raw.get('gpu_verified') is not True or raw.get('config_sha256') != config_sha256:
        raise ValueError('GPU/config identity missing or inconsistent')
    for key, expected in case.items():
        if raw.get(key) != expected or type(raw.get(key)) is not type(expected):
            raise ValueError(f'worker result has mismatched {key}')
    hardware = raw.get('hardware')
    if len(devices) != case['num_gpus'] or not isinstance(hardware, list) or len(hardware) != len(devices):
        raise ValueError('worker GPU hardware identity is missing or incomplete')
    for index, (gpu, expected) in enumerate(zip(hardware, devices)):
        if not isinstance(gpu, dict) or gpu.get('uuid') != expected or type(gpu.get('device')) is not int or gpu['device'] != index:
            raise ValueError('worker GPU device identity differs from the selected physical GPUs')
        if not isinstance(gpu.get('name'), str) or not gpu['name'].strip():
            raise ValueError('worker GPU name is missing')
        positive_int(gpu.get('total_memory_bytes'), 'GPU total_memory_bytes')
        capability = gpu.get('capability')
        if not isinstance(capability, list) or len(capability) != 2 or any(type(x) is not int or x < 0 for x in capability):
            raise ValueError('worker GPU capability is missing or invalid')
    software = raw.get('software')
    versions = software.get('packages') if isinstance(software, dict) else None
    required = ('torch', 'vllm', 'transformers') if case['phase'] == 'generation' else ('torch', 'transformers', 'peft', 'accelerate')
    if not isinstance(versions, dict) or software.get('ok') is not True or any(
        not isinstance(versions.get(name), str) or not versions[name].strip() for name in required):
        raise ValueError('worker software versions are missing or incomplete')
    for value in (software.get('python'), software.get('python_executable'), raw.get('cuda_version'), raw.get('torch_version')):
        if not isinstance(value, str) or not value.strip():
            raise ValueError('worker Python/CUDA/torch identity is missing')
    samples = raw.get('samples')
    if not isinstance(samples, list) or len(samples) != case['reps'] or not all(isinstance(row, dict) for row in samples):
        raise ValueError('worker result has missing repetitions')
    if any(type(row.get('rep')) is not int or row['rep'] != i for i, row in enumerate(samples)):
        raise ValueError('worker repetition IDs are missing, duplicated or out of order')
    times = [number(row.get('total_s'), 'total_s') for row in samples]
    if any(value <= 0 for value in times):
        raise ValueError('GPU timings must be positive')
    summary = stats(times)
    if 'timing_s' in raw:
        if not isinstance(raw['timing_s'], dict) or not math.isclose(
            number(raw['timing_s'].get('mean'), 'timing_s.mean'), summary['mean'], rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError('worker aggregate differs from its raw repetitions')
    return summary


def publish_json(path, value):
    """Publish a completed worker result without replacing existing evidence."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write('\n')
        os.link(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
