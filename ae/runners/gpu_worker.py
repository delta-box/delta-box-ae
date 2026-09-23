#!/usr/bin/env python3
"""One real Figure 8(b) GPU case. GPU frameworks are deliberately lazy imports."""
from __future__ import annotations

import argparse
from datetime import timedelta
import importlib.metadata
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

# Direct script execution adds runners/ to sys.path. Its profile.py shadows
# the stdlib profile module imported by cProfile inside PyTorch/Transformers.
_runner_dir = Path(__file__).resolve().parent
sys.path[:] = [entry for entry in sys.path if Path(entry).resolve() != _runner_dir]
sys.path.insert(0, str(_runner_dir.parent))
from repro.common import digest, stats
from repro import gpu_protocol as protocol


def packages(phase):
    names = ('torch', 'vllm', 'transformers') if phase == 'generation' else ('torch', 'transformers', 'peft', 'accelerate')
    versions = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return dict(ok=sys.version_info >= (3, 10) and all(versions.values()), packages=versions,
                python=sys.version, python_executable=sys.executable)


def die_with_parent():
    """Torchrun workers may have their own session; don't outlive their agent."""
    if sys.platform != 'linux':
        raise RuntimeError('GPU measurement requires Linux')
    import ctypes
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), 'cannot bind GPU worker lifetime to its parent')
    if os.getppid() != parent:
        raise RuntimeError('GPU worker parent exited during startup')


def driver_device_uuid(driver, device):
    import ctypes
    uuid_bytes = (ctypes.c_ubyte * 16)()
    driver.cuDeviceGetUuid.argtypes = [ctypes.c_void_p, ctypes.c_int]
    driver.cuDeviceGetUuid.restype = ctypes.c_int
    code = driver.cuDeviceGetUuid(ctypes.byref(uuid_bytes), device)
    if code:
        raise RuntimeError(f'cuDeviceGetUuid failed with CUDA error {code}')
    return 'GPU-' + str(uuid.UUID(bytes=bytes(uuid_bytes)))


def current_cuda_uuid():
    """Read the actual CUDA context's device identity, including on torch 2.4."""
    import ctypes
    driver = ctypes.CDLL('libcuda.so.1')
    device = ctypes.c_int()
    driver.cuCtxGetDevice.argtypes = [ctypes.POINTER(ctypes.c_int)]
    driver.cuCtxGetDevice.restype = ctypes.c_int
    code = driver.cuCtxGetDevice(ctypes.byref(device))
    if code:
        raise RuntimeError(f'cuCtxGetDevice failed with CUDA error {code}')
    return driver_device_uuid(driver, device)


def verify_visible_cuda_devices(expected):
    """Verify numeric NVML/CUDA mapping without allocating a model or context.

    vLLM 0.8 parses visible IDs as integers. PCI ordering normally aligns with
    NVML; if it does not, fail before loading a model on an unintended device.
    """
    visible = os.environ.get('CUDA_VISIBLE_DEVICES', '')
    indices = visible.split(',')
    if len(indices) != len(expected) or any(not item.isdigit() for item in indices):
        raise ValueError('worker requires verified numeric CUDA_VISIBLE_DEVICES; use the GPU timing supervisor')
    if os.environ.get('CUDA_DEVICE_ORDER') != 'PCI_BUS_ID':
        raise ValueError('worker requires CUDA_DEVICE_ORDER=PCI_BUS_ID from the supervisor')
    import ctypes
    driver = ctypes.CDLL('libcuda.so.1')
    driver.cuInit.argtypes = [ctypes.c_uint]
    driver.cuInit.restype = ctypes.c_int
    code = driver.cuInit(0)
    if code:
        raise RuntimeError(f'cuInit failed with CUDA error {code}')
    count = ctypes.c_int()
    driver.cuDeviceGetCount.argtypes = [ctypes.POINTER(ctypes.c_int)]
    driver.cuDeviceGetCount.restype = ctypes.c_int
    code = driver.cuDeviceGetCount(ctypes.byref(count))
    if code or count.value != len(expected):
        raise RuntimeError(f'CUDA device count mismatch: error={code}, count={count.value}, expected={len(expected)}')
    driver.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
    driver.cuDeviceGet.restype = ctypes.c_int
    actual = []
    for ordinal in range(count.value):
        device = ctypes.c_int()
        code = driver.cuDeviceGet(ctypes.byref(device), ordinal)
        if code:
            raise RuntimeError(f'cuDeviceGet failed with CUDA error {code}')
        actual.append(driver_device_uuid(driver, device))
    if actual != expected:
        raise RuntimeError(f'CUDA/NVML GPU UUID mapping differs: expected={expected}, actual={actual}')
    return dict(cuda_visible_devices=visible, cuda_device_order='PCI_BUS_ID', verified_device_uuids=actual)


def hardware(torch, device):
    with torch.cuda.device(device):
        props = torch.cuda.get_device_properties(device)
        physical_uuid = current_cuda_uuid()
    return dict(device=device, name=props.name, total_memory_bytes=props.total_memory,
                capability=list(torch.cuda.get_device_capability(device)),
                uuid=physical_uuid)


class UtilSampler:
    def __init__(self, device):
        self.device = device
        self.stop = threading.Event()
        self.samples, self.errors = [], []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set():
            try:
                result = subprocess.run(['nvidia-smi', '--query-gpu=utilization.gpu,memory.used',
                    '--format=csv,noheader,nounits', '-i', self.device], capture_output=True, text=True, timeout=2)
                if result.returncode:
                    raise RuntimeError(f'nvidia-smi rc={result.returncode}')
                for line in result.stdout.splitlines():
                    util, memory = [float(x.strip()) for x in line.split(',')]
                    self.samples.append(dict(monotonic_s=time.monotonic(), utilization_pct=util, memory_used_mib=memory))
            except Exception as error:
                self.errors.append(type(error).__name__)
            self.stop.wait(.1)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(3)


def generation_inputs(settings, batch, tokenizer):
    base = 'Solve this problem step by step: ' + ('x = 1 ' * (settings['in_tokens'] // 2))
    texts = [base + f' idx={i}' for i in range(batch)]
    if settings['prompt_mode'] == 'fixed-tokens':
        prompts = []
        for i in range(batch):
            text = f'Request {i}: ' + base
            ids = tokenizer.encode(text)
            while len(ids) < settings['in_tokens']:
                text += base
                ids = tokenizer.encode(text)
            prompts.append({'prompt_token_ids': ids[:settings['in_tokens']]})
        input_lengths = [settings['in_tokens']] * batch
    else:
        prompts = texts
        input_lengths = [len(tokenizer.encode(text)) for text in prompts]
    if max(input_lengths) >= settings['max_model_len']:
        raise ValueError('prompt fills the configured model context; no room for generation')
    # Make the original model-context clipping explicit instead of depending on
    # whether a vLLM version silently clips or rejects max_tokens=512.
    caps = [min(settings['out_tokens'], settings['max_model_len'] - size) for size in input_lengths]
    return prompts, input_lengths, caps


def generation(config, case, torch):
    from vllm import LLM, SamplingParams
    settings = config['generation']
    started = time.perf_counter()
    llm = LLM(model=config['model_path'], dtype='bfloat16', tensor_parallel_size=1,
              gpu_memory_utilization=settings['gpu_memory_utilization'],
              max_model_len=settings['max_model_len'], enforce_eager=False,
              enable_prefix_caching=settings['enable_prefix_caching'],
              trust_remote_code=False, seed=config['seed'])
    load_s = time.perf_counter() - started
    prompts, input_lengths, caps = generation_inputs(settings, case['batch'], llm.get_tokenizer())
    sampling = [SamplingParams(temperature=settings['temperature'], top_p=settings['top_p'],
                              max_tokens=cap, ignore_eos=True, seed=config['seed']) for cap in caps]
    for _ in range(case['warmup_reps']):
        llm.generate(prompts[:2], sampling[:2], use_tqdm=False)
    torch.cuda.synchronize()
    samples = []
    for repeat in range(case['reps']):
        with UtilSampler(config['devices'][0]) as monitor:
            torch.cuda.synchronize()
            started = time.perf_counter()
            outputs = llm.generate(prompts, sampling, use_tqdm=False)
            torch.cuda.synchronize()
            elapsed = time.perf_counter() - started
        if len(outputs) != case['batch'] or any(len(row.outputs) != 1 for row in outputs):
            raise RuntimeError('vLLM returned an incomplete batch')
        counts = [len(row.outputs[0].token_ids) for row in outputs]
        if any(count <= 0 or count > settings['out_tokens'] for count in counts):
            raise RuntimeError('invalid generated token count')
        if any(row.outputs[0].finish_reason == 'abort' for row in outputs):
            raise RuntimeError('vLLM aborted a request')
        if settings['prompt_mode'] == 'fixed-tokens' and any(count != settings['out_tokens'] for count in counts):
            raise RuntimeError('fixed-token case did not generate the requested length')
        if counts != caps:
            raise RuntimeError('generation did not reach the declared per-request token limit')
        actual_inputs = [len(row.prompt_token_ids) if row.prompt_token_ids is not None else input_lengths[i]
                         for i, row in enumerate(outputs)]
        samples.append(dict(rep=repeat, total_s=elapsed, output_token_counts=counts,
                            input_token_counts=actual_inputs, output_tokens_total=sum(counts),
                            tokens_per_s=sum(counts) / elapsed, telemetry=monitor.samples,
                            telemetry_errors=monitor.errors))
    return dict(samples=samples, load_s=load_s,
                generation_protocol=dict(prompt_mode=settings['prompt_mode'],
                    nominal_input_tokens=settings['in_tokens'], requested_max_output_tokens=settings['out_tokens'],
                    actual_input_lengths=input_lengths, effective_output_caps=caps, max_model_len=settings['max_model_len'],
                    context_limited=any(n + settings['out_tokens'] > settings['max_model_len'] for n in input_lengths),
                    prefix_caching=settings['enable_prefix_caching'],
                    warmup_batch=min(case['batch'], 2),
                    timing='synchronous llm.generate API wall; explicit warmup/load excluded; first-use work may remain'),
                hardware=[hardware(torch, 0)])


def training(config, case, torch):
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import LoraConfig, get_peft_model
    settings = config['training']
    multi = case['num_gpus'] > 1
    dist = None
    rank = int(os.environ.get('RANK', '0')) if multi else 0
    local_rank = int(os.environ.get('LOCAL_RANK', '0')) if multi else 0
    torch.cuda.set_device(local_rank)
    if not torch.cuda.is_bf16_supported():
        raise RuntimeError('paper recipe requires a BF16-capable CUDA GPU')
    try:
        if multi:
            import torch.distributed as dist
            if int(os.environ.get('WORLD_SIZE', '0')) != case['num_gpus']:
                raise RuntimeError('FSDP worker must be launched with the planned torchrun world size')
            dist.init_process_group('nccl', timeout=timedelta(seconds=config['timeout_s']))
        torch.manual_seed(config['seed'])
        torch.cuda.manual_seed_all(config['seed'])
        started = time.perf_counter()
        tokenizer = AutoTokenizer.from_pretrained(config['model_path'], local_files_only=True, trust_remote_code=False)
        kwargs = dict(torch_dtype=torch.bfloat16, local_files_only=True, trust_remote_code=False)
        if not multi:
            kwargs['device_map'] = {'': 0}
        model = AutoModelForCausalLM.from_pretrained(config['model_path'], **kwargs)
        if model.config.model_type != 'qwen2':
            raise ValueError('this paper recipe supports the Qwen2/Qwen2.5 architecture')
        model.config.use_cache = False
        model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant': False})
        model.enable_input_require_grads()
        model = get_peft_model(model, LoraConfig(r=settings['lora_r'], lora_alpha=settings['lora_alpha'],
            target_modules=['q_proj', 'k_proj', 'v_proj', 'o_proj'], lora_dropout=0.0, bias='none', task_type='CAUSAL_LM'))
        parameter_count = sum(p.numel() for p in model.parameters() if p.requires_grad)
        if multi:
            from functools import partial
            from torch.distributed.fsdp import FullyShardedDataParallel as FSDP, MixedPrecision, ShardingStrategy
            from torch.distributed.fsdp.wrap import transformer_auto_wrap_policy
            from transformers.models.qwen2.modeling_qwen2 import Qwen2DecoderLayer
            for parameter in model.parameters():
                if parameter.dtype == torch.float32:
                    parameter.data = parameter.data.to(torch.bfloat16)
            model = FSDP(model,
                auto_wrap_policy=partial(transformer_auto_wrap_policy, transformer_layer_cls={Qwen2DecoderLayer}),
                mixed_precision=MixedPrecision(param_dtype=torch.bfloat16, reduce_dtype=torch.bfloat16, buffer_dtype=torch.bfloat16),
                sharding_strategy=ShardingStrategy.FULL_SHARD, device_id=local_rank,
                limit_all_gathers=True, use_orig_params=True, sync_module_states=True)
        optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=settings['learning_rate'])
        model.train()
        load_s = time.perf_counter() - started
        torch.manual_seed(config['seed'] + rank)
        device = torch.device('cuda', local_rank)
        vocab_size = tokenizer.vocab_size
        def batch():
            ids = torch.randint(0, vocab_size, (case['per_gpu_batch'], settings['seq_len']), device=device)
            return ids, ids.clone()
        fixed = None if multi else batch()
        attention_mask = None if multi else torch.ones_like(fixed[0])
        def forward():
            ids, labels = batch() if multi else fixed
            kwargs = dict(input_ids=ids, labels=labels)
            if attention_mask is not None:
                kwargs['attention_mask'] = attention_mask
            return model(**kwargs)
        for _ in range(case['warmup_reps']):
            optimizer.zero_grad(set_to_none=True)
            for _ in range(case['grad_accum']):
                output = forward()
                (output.loss / case['grad_accum']).backward()
                del output
            optimizer.step()
        if not multi:
            optimizer.zero_grad(set_to_none=True)
        torch.cuda.synchronize()
        if multi:
            dist.barrier()
        warmup_peak = torch.cuda.max_memory_allocated() / (1024 ** 2)
        torch.cuda.reset_peak_memory_stats()
        samples = []
        for repeat in range(case['reps']):
            if multi:
                dist.barrier()
            torch.cuda.synchronize()
            started = time.perf_counter()
            if not multi:
                torch.cuda.synchronize()
                forward_started = time.perf_counter()
                output = forward()
                torch.cuda.synchronize()
                forward_done = time.perf_counter()
                last_loss = output.loss.item()
                output.loss.backward()
                torch.cuda.synchronize()
                backward_done = time.perf_counter()
                optimizer.step()
                optimizer.zero_grad(set_to_none=True)
                torch.cuda.synchronize()
                finished = time.perf_counter()
                del output
                if not math.isfinite(last_loss):
                    raise RuntimeError('training loss is not finite')
                samples.append(dict(rep=repeat, total_s=finished-started, loss=last_loss,
                                    forward_s=forward_done-forward_started,
                                    backward_s=backward_done-forward_done,
                                    optimizer_s=finished-backward_done))
                continue
            optimizer.zero_grad(set_to_none=True)
            last_loss = None
            for _ in range(case['grad_accum']):
                output = forward()
                (output.loss / case['grad_accum']).backward()
                last_loss = output.loss.item()
                del output
            optimizer.step()
            torch.cuda.synchronize()
            if multi:
                dist.barrier()
            elapsed = time.perf_counter() - started
            if not math.isfinite(last_loss):
                raise RuntimeError('training loss is not finite')
            samples.append(dict(rep=repeat, total_s=elapsed, loss=last_loss))
        details = dict(rank=rank, samples=samples, hardware=hardware(torch, local_rank),
                       peak_allocated_mib=max(warmup_peak, torch.cuda.max_memory_allocated() / (1024 ** 2)))
        if multi:
            ranks = [None] * case['num_gpus']
            dist.all_gather_object(ranks, details)
        else:
            ranks = [details]
        if multi:
            samples = [dict(rep=i, total_s=max(row['samples'][i]['total_s'] for row in ranks),
                            rank_losses=[row['samples'][i]['loss'] for row in ranks]) for i in range(case['reps'])]
        return dict(samples=samples, rank_records=ranks, load_s=load_s,
                    hardware=[row['hardware'] for row in ranks], trainable_params=parameter_count,
                    training_protocol=dict(seq_len=settings['seq_len'], lora_r=settings['lora_r'],
                        per_gpu_batch=case['per_gpu_batch'], grad_accum=case['grad_accum'],
                        timing='forward + backward + optimizer; FSDP includes collectives and batch generation',
                        gradient_checkpointing=True, optimizer='AdamW', learning_rate=settings['learning_rate']))
    finally:
        if dist is not None and dist.is_initialized():
            dist.destroy_process_group()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe-packages', choices=protocol.PHASES)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--case')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if args.probe_packages:
        result = packages(args.probe_packages)
        print(json.dumps(result))
        return 0 if result['ok'] else 2
    if not args.config or not args.case or not args.output:
        parser.error('--config, --case and --output are required')
    config = protocol.load_config(args.config)
    case = protocol.case_by_id(config, args.case)
    if args.output.exists() or args.output.is_symlink():
        raise FileExistsError(args.output)
    selected = config['devices'][:case['num_gpus']]
    if len(selected) != case['num_gpus'] or not config['model_path']:
        raise ValueError('configure the local model and allocated GPU IDs before execution')
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ.setdefault('VLLM_WORKER_MULTIPROC_METHOD', 'spawn')
    die_with_parent()
    selection = verify_visible_cuda_devices(selected)
    import torch
    if not torch.cuda.is_available() or torch.cuda.device_count() != case['num_gpus']:
        raise RuntimeError('actual CUDA device count differs from the planned case; no CPU fallback')
    if case['phase'] == 'generation':
        torch.cuda.set_device(0)
        if not torch.cuda.is_bf16_supported():
            raise RuntimeError('paper recipe requires BF16 support')
    measured = generation(config, case, torch) if case['phase'] == 'generation' else training(config, case, torch)
    if int(os.environ.get('RANK', '0')) == 0 or case['num_gpus'] == 1:
        result = dict(schema_version=1, kind='gpu-timing-result', status='ok', gpu_verified=True,
                      **case, **measured, device_selection=selection, config_sha256=digest(args.config),
                      timing_s=stats([row['total_s'] for row in measured['samples']]),
                      software=packages(case['phase']), cuda_version=torch.version.cuda,
                      torch_version=torch.__version__, worker_source_sha256=digest(Path(__file__)),
                      protocol_source_sha256=digest(Path(protocol.__file__)))
        protocol.publish_json(args.output, result)
        print(json.dumps({'case': case['case_id'], 'timing_s': result['timing_s']}), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
