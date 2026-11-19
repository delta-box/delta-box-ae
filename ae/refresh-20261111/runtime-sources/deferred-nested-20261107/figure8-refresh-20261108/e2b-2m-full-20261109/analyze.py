from pathlib import Path
import json,statistics,re,hashlib,collections
P=Path(__file__).resolve().parent;D=P/'l1-results';records=[]
finished=json.loads((P/'finished.json').read_text());assert finished['ok'],finished
for cohort in ['acceptance','round1','round2','round3','round4']:
 d=D/'results'/cohort;receipt=json.loads((d/'finished.json').read_text());assert receipt['ok']
 for ref in receipt['rows']:
  p=d/Path(ref['file']).name;o=json.loads(p.read_text());n=o['forks'];assert o['success'] and o['success_count']==n and o['all_children_retained_until_ready'] and o['batch_size']==n
  assert len(o['children'])==n and len(o['child_creates'])==n and len(o['batches'])==1
  assert all(x['create']['ok'] for x in o['child_creates']);assert all(x['status']=='ok' for x in o['cleanup']);assert len(o['cleanup'])==n+2
  vs=[]
  for c in o['children']:
   v=dict(re.findall(r'(\w+)=([^\s]+)',c['verification_stdout']))
   assert c['verify']['ok'] and v['bytes']=='67108864' and v['pages']=='16384' and v['errors']=='0' and v['requests']=='2' and v['checksum']=='2041721' and v['token']==o['run_id']
   vs.append(float(v['read_ms']))
  assert len(o['diagnostics_after_timer'])==1
  a=o['diagnostics_after_timer'][0];assert len(a['vms'])==n+1
  for vm in a['vms']:
   cfg=vm['machine_config'];assert cfg['vcpu_count']==4 and cfg['mem_size_mib']==8192 and cfg['huge_pages']=='2M'
   assert any(m.get('KernelPageSize')==2048 and m['Size']>=100*1024 for m in vm['large_mappings'])
  q=json.loads((d/(p.stem+'-quiescence.json')).read_text());assert q['seconds']==45
  records.append({'cohort':cohort,'role':o['role'],'n':n,'ready_ms':o['ready_e2e_ms'],'snapshot_ms':o['freeze_ms'],'create_ms':o['fork_wall_ms'],'verify_ms':o['verify_wall_ms'],'native_read_ms':vs,'file':str(p.relative_to(P)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'source_setup_ms':o['source_prepare']['ms'],'source_create_ms':o['source_create']['ms']})
groups={}
for n in [1,4,16,64]:
 rows=[r for r in records if r['n']==n and r['role']=='measured'];assert len(rows)==4
 g={'count':len(rows),'files':[r['file'] for r in rows]}
 for key in ['ready_ms','snapshot_ms','create_ms','verify_ms']:
  values=[r[key] for r in rows];g[key]={'values':values,'mean':statistics.mean(values),'median':statistics.median(values),'min':min(values),'max':max(values)}
 g['mean_native_read_per_child_ms']=statistics.mean(x for r in rows for x in r['native_read_ms']);groups[str(n)]=g
resources=[]
for f in (D/'results').glob('*/resources.jsonl'):resources.extend(json.loads(x) for x in f.read_text().splitlines())
limits={'min_l1_free_GiB':min(x['free_bytes'] for x in resources)/2**30,'min_l1_available_memory_GiB':min(x['MemAvailable'] for x in resources)/2**20,'peak_hugepage_used_GiB':max(x['HugePages_Total']-x['HugePages_Free'] for x in resources)*2/1024,'min_free_unreserved_hugepage_GiB':min(x['HugePages_Free']-x['HugePages_Rsvd'] for x in resources)*2/1024}
formal=[r for r in records if r['role']=='measured'];result={'groups':groups,'records':records,'formal_calls':len(formal),'formal_children':sum(r['n'] for r in formal),'all_calls':len(records),'all_children':sum(r['n'] for r in records),'all_verified_page_markers':16384*sum(r['n'] for r in records),'resource_extrema':limits}
assert result['formal_calls']==16 and result['formal_children']==340 and result['all_calls']==23 and result['all_children']==425
(P/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
print('n mean median min max snapshot_mean create_mean verify_mean')
for n,g in groups.items():print(n,*[round(g['ready_ms'][k],3) for k in ['mean','median','min','max']],*[round(g[k]['mean'],3) for k in ['snapshot_ms','create_ms','verify_ms']])
print('counts',result['formal_calls'],result['formal_children'],result['all_calls'],result['all_children']);print('resources',limits)
