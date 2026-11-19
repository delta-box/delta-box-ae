#!/usr/bin/env python3
"""Recompute selected November event means from the delivered raw records."""
import argparse,csv,hashlib,json,statistics
from pathlib import Path
from collections import defaultdict
p=argparse.ArgumentParser();p.add_argument('--evidence',type=Path,required=True);p.add_argument('--out',type=Path);a=p.parse_args()
E=a.evidence.resolve(); B=E/'ae/evidence/table2-unified12-20261107';A=B/'nested-full-audit-20261108'
schedules={r['instance']:r for r in json.loads((A/'e2b-phase7/schedules.json').read_text())}
assert len(schedules)==12
sources=[];events=[]
def read(p):
 raw=p.read_bytes();sources.append({'path':str(p.relative_to(E)),'sha256':hashlib.sha256(raw).hexdigest()});return json.loads(raw)
def add(system,inst,kind,ms):
 assert ms>0;(events.append({'system':system,'instance':inst,'group':schedules[inst]['group'],'metric':kind,'ms':ms}))
def schedule(inst):
 es=schedules[inst]['events'];cs=[e for e in es if e['type']=='ckpt'];m={e['ckpt_id']:e.get('node_id',0) for e in cs}
 return [e.get('node_id',0) for e in cs],[m[e['restore_to_ckpt_id']] for e in es if e['type']=='restore']
def check_order(cks,rss,nodes,targets,node_fn):
 kept=[c for c in cks if node_fn(c) in nodes]
 assert [node_fn(c) for c in kept]==nodes
 assert [r['selected_node_id'] for r in rss]==targets
 return kept
for system,base in [('CubeSandbox',A/'raw/nested-cube-host/cube-balance'),('E2B',A/'e2b-phase7/combined')]:
 assert {x.parent.name for x in base.glob('*/pilot_result.json')}==set(schedules)
 for inst in schedules:
  d=read(base/inst/'pilot_result.json');assert d['ok'];ns,ts=schedule(inst)
  assert len(d['checkpoints'])==30
  ck=check_order(d['checkpoints'],d['restores'],ns,ts,lambda x:x['node_id'])
  for c in ck:add(system,inst,'ck',c['blocking_ms'])
  if system=='E2B':assert len(d['kills'])==len(d['restores'])
  for i,r in enumerate(d['restores']):
   assert r['cold'] and r['verified_state'] and r['ping']['ok']
   assert r['ping']['node_id']==r['selected_node_id']
   add(system,inst,'rs',r['blocking_ms']+(d['kills'][i] if system=='E2B' else 0))
# FC uses continuous wall-clock intervals, including kill/spawn and synchronous post-ready work.
fc=read(B/'fc-reflink-20261109/summary.json')
for row in fc['sources']:
 inst=row['instance'];f=B/'fc-reflink-20261109'/row['path'].split('/fc-reflink-20261109/',1)[1];d=read(f);assert d['ok'];ns,ts=schedule(inst)
 ck=check_order(d['ckpts'],d['restore_events'],ns,ts,lambda x:x['state']['node_id'])
 for c in ck:
  assert abs(c['checkpoint_total_ms']-(c['checkpoint_end_ns']-c['checkpoint_start_ns'])/1e6)<1e-6
  add('FC-Diff+dm',inst,'ck',c['checkpoint_total_ms'])
 for r in d['restore_events']:
  assert abs(r['restore_total_ms']-(r['restore_end_ns']-r['restore_start_ns'])/1e6)<1e-6
  add('FC-Diff+dm',inst,'rs',r['restore_total_ms'])
# The main CRIU batch used in the online Table 2 is distinct from the later overlap batch.
criu={}
for f in (A/'remaining-three/l1-evidence').rglob('pilot_result.json'):
 d=read(f)
 if d.get('ok') and d.get('status')=='OK':
  assert d['instance'] not in criu;criu[d['instance']]=d
assert set(criu)==set(schedules)
for inst,d in criu.items():
 ns,ts=schedule(inst);ck=check_order(d['checkpoints'],d['restores'],ns,ts,lambda x:x['node_id'])
 assert d['conditions']['criu_restore_mode']=='lazy-pages'
 assert d['conditions']['checkpoint_fs_criu']=='concurrent after quiesce'
 for c in ck:add('CRIU+cp (paper)',inst,'ck',c['checkpoint_total_ms'])
 for r in d['restores']:add('CRIU+cp (paper)',inst,'rs',r['restore_total_ms'])
for inst in schedules:
 f=A/'raw/nested-replay-host/replay12'/inst/'restores.csv';raw=f.read_bytes();sources.append({'path':str(f.relative_to(E)),'sha256':hashlib.sha256(raw).hexdigest()})
 rs=list(csv.DictReader(f.open()));ns,ts=schedule(inst);assert [int(r['target_node']) for r in rs]==ts
 assert int(rs[0]['restore_index'])==0
 add('copy+replay',inst,'ck',float(rs[0]['setup_copy_ms']))
 for r in rs:
  assert r['ok']=='True' and r['rc']=='0'
  assert abs(float(r['restore_ms'])-sum(float(r[k]) for k in ['rmtree_ms','copytree_ms','replay_ms']))<1e-6
  add('copy+replay',inst,'rs',float(r['restore_ms']))
report={'systems':{},'sources':sources,'semantics':{'E2B restore':'recorded blocking_ms plus synchronous old-sandbox termination','Replay checkpoint':'one initial pristine repository copy per trace, 12 observations','Replay restore':'rmtree + copytree + full replay process, 334 observations','selection':'317 schedule checkpoint nodes; exclude 43 additional expansion snapshots; all 334 restore targets match'}}
for system in sorted({e['system'] for e in events}):
 report['systems'][system]={}
 for group in ['Event Avg',*sorted({e['group'] for e in events})]:
  metrics={}
  for kind in ['ck','rs']:
   rows=[e for e in events if e['system']==system and e['metric']==kind and (group=='Event Avg' or e['group']==group)]
   metrics[kind]={'n':len(rows),'mean_ms':statistics.mean(r['ms'] for r in rows)}
  report['systems'][system][group]=metrics
 total=report['systems'][system]['Event Avg'];assert total['ck']['n']==(12 if system=='copy+replay' else 317) and total['rs']['n']==334
ck=list(csv.DictReader((B/'criu-overlap-20261109/checkpoint-events.csv').open()));rs=list(csv.DictReader((B/'criu-overlap-20261109/restore-events.csv').open()))
ck=[r for r in ck if r['aligned_ck']=='True' and r['unhidden_checkpoint_ms'] not in ('','None')];assert len(ck)==305 and len(rs)==334
report['Table3_CRIU_remaining_wait']={'n':305,'mean_ms':statistics.mean(float(r['unhidden_checkpoint_ms']) for r in ck),'terminal_checkpoints_excluded':12}
for system,(ck,rs) in {'CubeSandbox':(839.8,643.9),'E2B':(1072.0,424.7),'FC-Diff+dm':(647.9,2567.9),'CRIU+cp (paper)':(567.6,609.5),'copy+replay':(313.2,14220.6)}.items():
 v=report['systems'][system]['Event Avg'];assert abs(v['ck']['mean_ms']-ck)<.051 and abs(v['rs']['mean_ms']-rs)<.051,(system,v)
assert abs(report['Table3_CRIU_remaining_wait']['mean_ms']-91.1)<.05
report['status']='passed'
if a.out:a.out.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({s:v['Event Avg'] for s,v in report['systems'].items()},indent=2));print('Table 3:',report['Table3_CRIU_remaining_wait']);print('PASS: event counts, order, timing formulas, and published rounded means')
