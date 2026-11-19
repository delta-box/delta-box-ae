#!/usr/bin/env python3
import csv
import fcntl
import json
from pathlib import Path
import statistics
from audit_result import audit_trace

P = Path(__file__).resolve().parent
A = P/'l1-results/formal'
if not A.exists():
    A = P/'formal'
inputs = json.loads((P/'inputs.json').read_text())
assert json.loads((A/'finished.json').read_text())['ok']
rows = [audit_trace(A/'results'/i['instance'], i) for i in inputs]
assert sum(r['aligned_ck'] for r in rows)==317 and sum(r['rs'] for r in rows)==334
groups = {}
for name in ['Django','SymPy','Scientific','Tools/Small','Event Avg']:
    picked = [r for r in rows if name=='Event Avg' or r['group']==name]
    assert picked, name
    ck = [x for r in picked for x in r['checkpoints']]
    rs = [x for r in picked for x in r['restores']]
    phases = {k: statistics.fmean(x['continuous_intervals'].get(k,{}).get('ms',0) for x in rs)
              for k in ['vm_kill','merge','dm_restore','load','first_rpc','post_ready']}
    groups[name] = {'traces':len(picked),'ck_count':len(ck),'rs_count':len(rs),
                    'ck_ms':statistics.fmean(x['checkpoint_total_ms'] for x in ck),
                    'rs_ms':statistics.fmean(x['restore_total_ms'] for x in rs),
                    'ready_ms':statistics.fmean(x['restore_ready_ms'] for x in rs),
                    'post_ready_ms':statistics.fmean(x['post_ready_ms'] for x in rs),
                    'continuous_phases_ms':phases,
                    'unattributed_ms':statistics.fmean(x['unattributed_ms'] for x in rs),
                    'fc_spawn_ms':statistics.fmean(x['load']['fc_spawn_ms'] for x in rs),
                    'fc_load_ms':statistics.fmean(x['load']['fc_load_ms'] for x in rs),
                    'fc_kill_inside_load_ms':statistics.fmean(x['load']['fc_kill_inside_load_ms'] for x in rs)}
result = {'complete':True,'groups':groups,'sources':[{k:v for k,v in r.items() if k not in ['checkpoints','restores']} for r in rows]}
(P/'summary.json').write_text(json.dumps(result,indent=2))
with (P/'events.csv').open('w',newline='') as f:
    w=csv.writer(f);w.writerow(['instance','group','operation','node','continuous_ms','ready_ms','post_ready_ms'])
    for r in rows:
        for x in r['checkpoints']:w.writerow([r['instance'],r['group'],'ck',x['state']['node_id'],x['checkpoint_total_ms'],'',''])
        for x in r['restores']:w.writerow([r['instance'],r['group'],'rs',x['selected_node_id'],x['restore_total_ms'],x['restore_ready_ms'],x['post_ready_ms']])
lines=['# FC-Diff CoW rerun results','','| Group | Traces | CK | RS | CK ms | RS ms | Ready ms | Post-ready ms |','|---|---:|---:|---:|---:|---:|---:|---:|']
for name,g in groups.items():
    lines.append(f"| {name} | {g['traces']} | {g['ck_count']} | {g['rs_count']} | {g['ck_ms']:.3f} | {g['rs_ms']:.3f} | {g['ready_ms']:.3f} | {g['post_ready_ms']:.3f} |")
lines += ['', 'New CoW reconstruction cohort; continuous windows, 317 aligned CK /334 RS. Validation-only pilot is excluded. Paper unchanged.']
(P/'README.md').write_text('\n'.join(lines)+'\n')
print(json.dumps({'complete':True,'groups':groups}),flush=True)
