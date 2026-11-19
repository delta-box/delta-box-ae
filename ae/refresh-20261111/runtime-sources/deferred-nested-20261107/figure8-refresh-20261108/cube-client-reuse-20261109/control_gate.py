from pathlib import Path
import json,re,statistics,sys,time
P=Path(__file__).resolve().parent;D=P/'results/control';receipt=json.loads((D/'finished.json').read_text());assert receipt['ok'];groups={};count=0
for ref in receipt['rows']:
 d=json.loads((D/Path(ref['file']).name).read_text());n=d['forks'];assert d['success'] and d['success_count']==n and d['all_children_retained_until_ready']
 for child in d['children']:
  v=dict(re.findall(r'(\w+)=([^\s]+)',child['verification_stdout']));assert child['verify']['ok'] and v['pages']=='16384' and v['bytes']=='67108864' and v['errors']=='0' and v['requests']=='2' and v['checksum']=='2041721' and v['workers']=='4' and v['token']==d['run_id']
 assert all(x['status']=='ok' for x in d['cleanup'])
 if d['mode']=='reuse':
  c=d['client_configuration'];assert c['shared_contexts_created']==1 and c['contexts'][0]['verify_mode']==2 and c['contexts'][0]['check_hostname']
  assert c['contexts'][0]['epoch']<d['started_epoch']
 inside=[x for x in d['client_instrumentation'] if x['type']=='build_client' and d['started_epoch']<=x['started_epoch']<=d['ready_finished_epoch']]
 assert len(inside)==n and all(x['mode']==d['mode'] for x in inside),(ref,len(inside),n)
 if d['role']=='control':
  key=f"{d['mode']}-n{n}";groups.setdefault(key,[]).append({'file':ref['file'],'ready_ms':d['ready_e2e_ms'],'clone_ms':d['fork_wall_ms'],'verify_ms':d['verify_wall_ms'],'client_mean_ms':statistics.mean(x['ms'] for x in inside)});count+=n
assert count==68 and len(groups)==4 and all(len(v)==2 for v in groups.values())
result={'ok':True,'epoch':time.time(),'verified_control_children':count,'groups':groups,'gate':'correctness and unchanged semantics, not a speed threshold'}
(P/'control-gate.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)
