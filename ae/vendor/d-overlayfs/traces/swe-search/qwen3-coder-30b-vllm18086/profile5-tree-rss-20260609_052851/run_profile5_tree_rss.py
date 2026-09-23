#!/usr/bin/env python3
from __future__ import annotations
import json, os, subprocess, sys, time, signal
from pathlib import Path

RUN_ROOT=Path(os.environ['RUN_ROOT'])
SRC=Path(os.environ.get('SRC','/mnt/disk2/dyp/spr_payload/moatless-det-src'))
PY=os.environ.get('PY','/mnt/disk2/dyp/moatless_det_venv/bin/python')
LOCAL_PROXY_BASE=os.environ.get('LOCAL_PROXY_BASE','http://127.0.0.1:18086/v1')
NUMA_NODE=os.environ.get('NUMA_NODE','2')
SAMPLE_INTERVAL=float(os.environ.get('SAMPLE_INTERVAL','0.5'))
TIMEOUT_S=float(os.environ.get('TIMEOUT_S','3600'))
MODEL_CONFIG=os.environ.get('MODEL_CONFIG','qwen3_coder')
MODEL_NAME=os.environ.get('MODEL_NAME','openai/qwen3-coder-30b')
MAX_ITERATIONS=os.environ.get('MAX_ITERATIONS','30')
MAX_EXPANSIONS=os.environ.get('MAX_EXPANSIONS','2')
MAX_COST=os.environ.get('MAX_COST','10.0')
REPO_DIR=RUN_ROOT/'repos'
INDEX_STORE_DIR=Path(os.environ.get('INDEX_STORE_DIR','/mnt/disk2/dyp/spr_payload/index_store'))
REPO_CACHE=Path(os.environ.get('REPO_CACHE','/mnt/disk2/dyp/spr_payload/repos'))
WORKLIST=RUN_ROOT/'worklist.tsv'

RUN_ROOT.mkdir(parents=True, exist_ok=True)
(REPO_DIR).mkdir(exist_ok=True)
(RUN_ROOT/'evals').mkdir(exist_ok=True)
(RUN_ROOT/'step_metrics').mkdir(exist_ok=True)
(RUN_ROOT/'tmp').mkdir(exist_ok=True)

for line in WORKLIST.read_text().splitlines():
    inst, split, repo = line.split('\t')
    repo_dir=repo.replace('/','__')
    src=REPO_CACHE/f'swe-bench_{repo_dir}'
    dst=REPO_DIR/f'swe-bench_{repo_dir}'
    if src.is_dir() and not dst.exists():
        dst.symlink_to(src, target_is_directory=True)

def children_of(pid:int)->list[int]:
    out=[]
    try:
        txt=subprocess.check_output(['pgrep','-P',str(pid)], text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return out
    for s in txt.split():
        try: out.append(int(s))
        except ValueError: pass
    return out

def tree_pids(root:int)->list[int]:
    seen=[]; stack=[root]
    while stack:
        p=stack.pop()
        if p in seen: continue
        if not Path(f'/proc/{p}').exists(): continue
        seen.append(p)
        stack.extend(children_of(p))
    return seen

def rss_kb(pid:int)->int:
    try:
        for line in Path(f'/proc/{pid}/status').read_text(errors='replace').splitlines():
            if line.startswith('VmRSS:'):
                return int(line.split()[1])
    except Exception:
        return 0
    return 0

def cmdline(pid:int)->str:
    try:
        return Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace').strip()
    except Exception:
        return ''

def sample(proc:subprocess.Popen, samples:list[dict]):
    pids=tree_pids(proc.pid)
    rss_by={str(p):rss_kb(p) for p in pids}
    samples.append({
        't_wall_s': time.time(),
        'root_pid': proc.pid,
        'pids': pids,
        'rss_kb_total': sum(rss_by.values()),
        'rss_kb_by_pid': rss_by,
        'cmd_by_pid': {str(p):cmdline(p) for p in pids},
    })

base_env=os.environ.copy()
base_env.update({
    'OPENAI_API_BASE': LOCAL_PROXY_BASE,
    'OPENAI_BASE_URL': LOCAL_PROXY_BASE,
    'OPENAI_API_KEY':'dummy',
    'CUSTOM_LLM_API_KEY':'dummy',
    'NO_PROXY':'localhost,127.0.0.1,::1,192.168.1.8',
    'no_proxy':'localhost,127.0.0.1,::1,192.168.1.8',
    'REPO_DIR':str(REPO_DIR),
    'INDEX_STORE_DIR':str(INDEX_STORE_DIR),
    'MOATLESS_DIR':str(RUN_ROOT/'evals'),
    'MOATLESS_STEP_METRICS_DIR':str(RUN_ROOT/'step_metrics'),
    'TMPDIR':str(RUN_ROOT/'tmp'),
    'PYTHONHASHSEED':'0',
    'OPENBLAS_NUM_THREADS':'1',
    'OMP_NUM_THREADS':'1',
    'MKL_NUM_THREADS':'1',
    'NUMEXPR_MAX_THREADS':'1',
})
summary=[]
for idx,line in enumerate(WORKLIST.read_text().splitlines(),1):
    inst, split, repo=line.split('\t')
    final=RUN_ROOT/inst
    final.mkdir(exist_ok=True)
    if (final/'manifest.json').exists():
        try:
            m=json.loads((final/'manifest.json').read_text())
            if m.get('trajectory_exists') and m.get('rss_sample_count',0)>0:
                print(f'[{idx}] SKIP {inst}', flush=True)
                summary.append(m)
                continue
        except Exception:
            pass
    eval_name=f'profile5_tree_rss_mcts{MAX_ITERATIONS}_{inst}'
    cmd=[
        'numactl', f'--cpunodebind={NUMA_NODE}', f'--membind={NUMA_NODE}',
        PY, 'moatless/benchmark/run_evaluation.py',
        '--config', MODEL_CONFIG,
        '--model', MODEL_NAME,
        '--split', split,
        '--instance-ids', inst,
        '--num-workers','1',
        '--max-iterations', MAX_ITERATIONS,
        '--max-expansions', MAX_EXPANSIONS,
        '--max-cost', MAX_COST,
        '--evaluation-name', eval_name,
        '--no-testbed',
    ]
    print(f'[{idx}] START {inst} split={split} numa={NUMA_NODE}', flush=True)
    samples=[]
    start=time.time()
    with (final/'run.log').open('w') as log:
        proc=subprocess.Popen(cmd, cwd=SRC, env=base_env, stdout=log, stderr=subprocess.STDOUT, text=True, start_new_session=True)
        rc=None
        while True:
            rc=proc.poll()
            if rc is not None:
                sample(proc, samples)
                break
            sample(proc, samples)
            if time.time()-start > TIMEOUT_S:
                try: os.killpg(proc.pid, signal.SIGTERM)
                except Exception: pass
                time.sleep(5)
                if proc.poll() is None:
                    try: os.killpg(proc.pid, signal.SIGKILL)
                    except Exception: pass
                rc=proc.wait()
                break
            time.sleep(SAMPLE_INTERVAL)
    end=time.time()
    eval_inst=RUN_ROOT/'evals'/eval_name/inst
    for name in ('trajectory.json','eval_result.json'):
        src=eval_inst/name
        if src.exists():
            (final/name).write_bytes(src.read_bytes())
    step_src=RUN_ROOT/'step_metrics'/f'{inst}.jsonl'
    if step_src.exists():
        (final/'step_metrics.jsonl').write_bytes(step_src.read_bytes())
    per_step_dirty=[]
    per_step_dirty_pages=[]
    per_step_dirty_mb=[]
    if (final/'step_metrics.jsonl').exists():
        for raw in (final/'step_metrics.jsonl').read_text(encoding='utf-8').splitlines():
            try:
                sr=json.loads(raw)
            except Exception:
                continue
            pages=sr.get('soft_dirty_pages')
            mb=sr.get('soft_dirty_mb')
            b=sr.get('soft_dirty_bytes')
            per_step_dirty.append({
                'iteration': sr.get('iteration'),
                'node_id': sr.get('node_id'),
                'parent_node_id': sr.get('parent_node_id'),
                'action_name': sr.get('action_name'),
                'action_class': sr.get('action_class'),
                'soft_dirty_pages': pages,
                'soft_dirty_bytes': b,
                'soft_dirty_mb': mb,
                'vmrss_mb_after': (sr.get('after_proc_snapshot') or {}).get('vmrss_kb') / 1024 if isinstance((sr.get('after_proc_snapshot') or {}).get('vmrss_kb'), (int, float)) else None,
                'smaps_rss_mb_after': (sr.get('after_proc_snapshot') or {}).get('smaps_rss_kb') / 1024 if isinstance((sr.get('after_proc_snapshot') or {}).get('smaps_rss_kb'), (int, float)) else None,
            })
            if isinstance(pages, (int, float)):
                per_step_dirty_pages.append(int(pages))
            if isinstance(mb, (int, float)):
                per_step_dirty_mb.append(float(mb))
    (final/'per_step_memory_dirty.json').write_text(json.dumps(per_step_dirty, indent=2, sort_keys=True)+'\n', encoding='utf-8')
    (final/'rss_samples.jsonl').write_text('\n'.join(json.dumps(s, sort_keys=True) for s in samples)+'\n')
    rss_vals=[s['rss_kb_total']/1024 for s in samples if s.get('rss_kb_total')]
    m={
        'instance_id':inst, 'split':split, 'repo':repo, 'evaluation_name':eval_name,
        'trace_kind':'native_moatless_swe_search_no_testbed_process_tree_rss',
        'numa_node':int(NUMA_NODE), 'sample_interval_s':SAMPLE_INTERVAL,
        'start_wall_s':start, 'end_wall_s':end, 'duration_s':end-start, 'return_code':rc,
        'rss_sample_count':len(samples),
        'process_tree_rss_mean_mb': sum(rss_vals)/len(rss_vals) if rss_vals else None,
        'process_tree_rss_max_mb': max(rss_vals) if rss_vals else None,
        'process_tree_rss_min_mb': min(rss_vals) if rss_vals else None,
        'per_step_soft_dirty_pages_series': per_step_dirty_pages,
        'per_step_soft_dirty_mb_series': per_step_dirty_mb,
        'per_step_soft_dirty_mean_mb': sum(per_step_dirty_mb)/len(per_step_dirty_mb) if per_step_dirty_mb else None,
        'per_step_soft_dirty_max_mb': max(per_step_dirty_mb) if per_step_dirty_mb else None,
        'per_step_soft_dirty_mean_pages': sum(per_step_dirty_pages)/len(per_step_dirty_pages) if per_step_dirty_pages else None,
        'per_step_memory_dirty_path': str(final/'per_step_memory_dirty.json'),
        'trajectory_exists': (final/'trajectory.json').exists(),
        'step_metrics_lines': sum(1 for _ in (final/'step_metrics.jsonl').open()) if (final/'step_metrics.jsonl').exists() else 0,
    }
    (final/'manifest.json').write_text(json.dumps(m, indent=2, sort_keys=True)+'\n')
    print(f"[{idx}] DONE {inst} rc={rc} rss_mean={m['process_tree_rss_mean_mb']:.1f} rss_max={m['process_tree_rss_max_mb']:.1f} samples={len(samples)}", flush=True)
    summary.append(m)
valid_rss=[m['process_tree_rss_mean_mb'] for m in summary if m.get('process_tree_rss_mean_mb') is not None]
valid_dirty=[x for m in summary for x in (m.get('per_step_soft_dirty_mb_series') or [])]
valid_dirty_pages=[x for m in summary for x in (m.get('per_step_soft_dirty_pages_series') or [])]
(RUN_ROOT/'tree_rss_summary.json').write_text(json.dumps({
    'run_root':str(RUN_ROOT),
    'instances':summary,
    'mean_of_instance_mean_rss_mb':sum(valid_rss)/len(valid_rss) if valid_rss else None,
    'pooled_per_step_soft_dirty_mean_mb':sum(valid_dirty)/len(valid_dirty) if valid_dirty else None,
    'pooled_per_step_soft_dirty_max_mb':max(valid_dirty) if valid_dirty else None,
    'pooled_per_step_soft_dirty_mean_pages':sum(valid_dirty_pages)/len(valid_dirty_pages) if valid_dirty_pages else None,
    'pooled_per_step_count':len(valid_dirty),
}, indent=2, sort_keys=True)+'\n')
print('SUMMARY', RUN_ROOT/'tree_rss_summary.json', flush=True)
