"""Replay legacy Claude/MiMo state transitions without generating new model replies.

The old strategy schedule omitted executable actions. This adapter replays recorded
successful edit diffs and reads the recorded file context. It does not claim to
rerun the legacy agent's semantic-search implementation. Time between transitions
is pacing evidence, not an isolated LLM RTT measurement.
"""
from __future__ import annotations
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re


def identifier(instance, tid):
    return hashlib.sha1(f'{instance}::{tid}'.encode()).hexdigest()[:8]


def recorded_diff_op(diff, *, trace_sha256, instance, transition_id, repairs):
    digest = hashlib.sha256(diff.encode()).hexdigest()
    matches = [r for r in repairs if
               (r["trace_sha256"], r["instance"], r["transition"], r["original_diff_sha256"]) ==
               (trace_sha256, instance, transition_id, digest)]
    op = {"type": "apply_recorded_diff", "diff": diff}
    if not matches:
        return op
    if len(matches) != 1:
        raise ValueError("Ambiguous recorded diff repair")
    repair = matches[0]
    if hashlib.sha256(repair["repaired_diff"].encode()).hexdigest() != repair["repaired_diff_sha256"]:
        raise ValueError("Changed recorded diff repair")
    if any(not re.fullmatch(r"[0-9a-f]{64}", repair[k]) for k in ("before_sha256", "after_sha256")):
        raise ValueError("Recorded diff repair requires complete file hashes")
    op.update(diff=repair["repaired_diff"], original_diff=diff,
              recorded_file_validation={k: repair[k] for k in ("path", "before_sha256", "after_sha256")},
              repair_evidence={k: repair[k] for k in
                  ("trace_sha256", "transition", "original_diff_sha256", "repaired_diff_sha256",
                   "pre_snapshot_sha256", "post_snapshot_sha256", "corroborating_successor_ids")})
    return op


def convert_legacy(trace_dir: Path, instance: str, output: Path, adaptive=False, timing_policy='recorded-wall'):
    if timing_policy not in ('paper-zero','recorded-wall'):
        raise ValueError('legacy timing policy must be paper-zero or recorded-wall')
    data=json.loads((trace_dir/'trajectory.json').read_text())
    transitions=data['transitions']
    trace_sha256=hashlib.sha256((trace_dir/'trajectory.json').read_bytes()).hexdigest()
    repair_path=Path(__file__).with_name('legacy_diff_repairs.json')
    repair_data=json.loads(repair_path.read_text())
    if repair_data['schema_version']!=1:raise ValueError('Unsupported legacy diff repair schema')
    repaired=[]
    commit=(data.get('workspace',{}).get('repository') or {}).get('commit')
    if not re.fullmatch(r'[0-9a-f]{40}',str(commit)):raise ValueError('Legacy trace has no full base commit')
    root_id=identifier(instance,'bootstrap')
    events=[{'type':'ckpt','iter':0,'ckpt_id':root_id,'strategy':'standard','latency_ms':0.,'bootstrap':True,
             'worker_ops':[{'type':'noop','note':'initial physical state for legacy read-only roots'}],'worker_ops_required':True}]
    by_id={t['id']:t for t in transitions};known={};active=root_id
    def parent_checkpoint(t):
        seen=set();tid=t.get('previous_state_id')
        while tid is not None:
            if tid in known:return known[tid]
            if tid in seen:raise ValueError('Cycle in legacy state parents')
            seen.add(tid)
            if tid not in by_id:raise ValueError('Missing legacy state parent')
            tid=by_id[tid].get('previous_state_id')
        return root_id
    for i,tr in enumerate(transitions):
        if tr.get('name') in ('Pending','Rejected','Finished'):continue
        parent=parent_checkpoint(tr)
        if active!=parent:events.append({'type':'restore','iter':len(events),'restore_to_ckpt_id':parent})
        ops=[]
        context=(tr.get('snapshot',{}).get('file_context') or {}).get('files',[])
        for item in context:
            if item.get('file_path'):ops.append({'type':'view_file','path':item['file_path']})
        for action in tr.get('actions',[]):
            diff=((action.get('response') or {}).get('output') or {}).get('diff')
            if diff:
                op=recorded_diff_op(diff,trace_sha256=trace_sha256,instance=instance,
                                    transition_id=tr['id'],repairs=repair_data['repairs'])
                ops.append(op)
                if op.get('repair_evidence'):repaired.append(op['repair_evidence'])
        if not ops:ops=[{'type':'noop','note':'legacy state has no recorded file IO; classifier boundary only'}]
        if tr.get('name')!='EditCode' and any(op['type']=='apply_recorded_diff' for op in ops):
            raise ValueError('Legacy read-only classification contains a recorded write')
        latency=0.
        if i+1<len(transitions):
            start=datetime.fromisoformat(tr['created_at']);end=datetime.fromisoformat(transitions[i+1]['created_at'])
            latency=max(0.,(end-start).total_seconds()*1000)
        cid=identifier(instance,tr['id']);known[tr['id']]=cid;active=cid
        events.append({'type':'ckpt','iter':len(events),'ckpt_id':cid,
            'strategy':'lightweight' if adaptive and tr.get('name')!='EditCode' else 'standard',
            'latency_ms':0. if timing_policy=='paper-zero' else latency,
            'recorded_inter_transition_ms':latency,
            'latency_source':'paper-zero' if timing_policy=='paper-zero' else 'recorded-inter-transition-elapsed-not-isolated-LLM-RTT',
            'worker_ops':ops,'worker_ops_required':True,'legacy_transition_id':tr['id'],'legacy_state':tr['name']})
    metadata={'instance':instance,'repository_commit':commit,'conversion_policy':'legacy-recorded-diff-and-file-context',
        'legacy_timing_policy':timing_policy,
        'legacy_diff_repairs':repaired,'legacy_diff_repair_fixture_sha256':hashlib.sha256(repair_path.read_bytes()).hexdigest(),
        'n_ckpt':sum(e['type']=='ckpt' for e in events),'n_restore':sum(e['type']=='restore' for e in events),
        'bootstrap_checkpoints':1,'legacy_checkpoint_count':sum(e['type']=='ckpt' and not e.get('bootstrap') for e in events),
        'limitations':['Bootstrap physical checkpoint is reported separately from legacy classifier boundaries.',
                      'Replays recorded edit diffs and file context; does not re-execute semantic search or generate model output.',
                      ('Paper Figure 6 uses zero injected pacing; recorded inter-transition intervals remain diagnostic metadata.' if timing_policy=='paper-zero'
                       else 'Pacing uses recorded time between transitions, not isolated LLM RTT.')]}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(''.join(json.dumps(e)+'\n' for e in events))
    output.with_suffix('.meta.json').write_text(json.dumps(metadata,indent=2)+'\n')
    return metadata
