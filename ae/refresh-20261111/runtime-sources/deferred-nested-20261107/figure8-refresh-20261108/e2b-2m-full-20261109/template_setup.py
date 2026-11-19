from pathlib import Path
import json,subprocess,sys,time,hashlib,shutil,struct,uuid
P=Path(__file__).resolve().parent;H=Path('/mnt/disk2/dyp/ae-hosted-20260922/e2b');T=json.loads((P/'template.json').read_text());STATE=P/'template-before.json'
def save(n,o):(P/n).write_text(json.dumps(o,indent=2)+'\n')
def sql(q):
 r=subprocess.run(['docker','exec','-i','ae-e2b-postgres','psql','-U','postgres','-d','postgres','-X','-At','-v','ON_ERROR_STOP=1'],input=q,text=True,capture_output=True,timeout=60)
 if r.returncode:raise RuntimeError(r.stderr)
 return r.stdout.strip()
def hp():return {str(p):int(p.read_text()) for p in Path('/sys/devices/system/node').glob('node*/hugepages/hugepages-2048kB/nr_hugepages')}
def original():return sql("select build_id from env_build_assignments where env_id='ae-e2b-agent-4c8g-20261106' and tag='default';")
def enter():
 assert not STATE.exists();before=hp();assert len(before)==4 and all(v==0 for v in before.values());assert original()==T['rootfs_source']
 assert not subprocess.run(['pgrep','-x','firecracker'],capture_output=True).stdout
 assert sql("select count(*) from envs where id='"+T['template']+"';")=='0'
 assert sql("select count(*) from env_builds where id='"+T['build_id']+"';")=='0'
 verifier=Path('/mnt/disk2/dyp/deltabox-runtime/ae/work/deferred-nested-20261107/results/e2b-freeze-repair/verify_template_closure.py')
 shutil.copy2(verifier,P/'verify_template_closure.py')
 with (P/'template-integrity.log').open('w') as out:
  subprocess.run(['python3',str(verifier),'--storage',str(H/'storage'),'--build',T['build_id'],'--output',str(P/'template-closure.json')],check=True,stdout=out,stderr=subprocess.STDOUT,timeout=300)
 closure=json.loads((P/'template-closure.json').read_text());assert closure['headers']['memfile']['block_size']==2**21
 b=H/'storage/templates'/T['build_id'];(P/'template-artifacts').mkdir(exist_ok=True)
 for n in ['metadata.json','memfile.header','rootfs.ext4.header','snapfile']:shutil.copy2(b/n,P/'template-artifacts'/n)
 save('template-before.json',{'hugepages':before,'original_default_build':original(),'epoch':time.time()})
 save('reservation-preparation.json',{'before_meminfo':Path('/proc/meminfo').read_text(),'epoch':time.time(),'scope':'L1 only; before all measurements'})
 subprocess.run(['sync'],check=True)
 Path('/proc/sys/vm/drop_caches').write_text('3')
 for node in ['node2','node3']:
  compact=Path('/sys/devices/system/node')/node/'compact'
  if compact.exists():compact.write_text('1')
 targets={path:(8192 if Path(path).parent.parent.parent.name in ('node2','node3') else 0) for path in before}
 for path,n in targets.items():Path(path).write_text(str(n))
 during=hp();save('hugepages-during.json',{'values':during,'meminfo':Path('/proc/meminfo').read_text()});assert during==targets and sum(during.values())==16384
 template=T['template'];build=T['build_id'];source=T['rootfs_source']
 sql(f"""BEGIN;
INSERT INTO envs SELECT (jsonb_populate_record(NULL::envs,row_to_json(e)::jsonb || jsonb_build_object('id','{template}','created_at',now(),'updated_at',now(),'spawn_count',0,'last_spawned_at',NULL))).* FROM envs e WHERE id='ae-e2b-agent-4c8g-20261106';
INSERT INTO env_builds SELECT (jsonb_populate_record(NULL::env_builds,row_to_json(b)::jsonb || jsonb_build_object('id','{build}','env_id','{template}','created_at',now(),'updated_at',now(),'finished_at',now()))).* FROM env_builds b WHERE id='{source}';
INSERT INTO env_build_assignments (env_id,build_id,tag) VALUES ('{template}','{build}','default');COMMIT;""")
 assert sql("select build_id from env_build_assignments where env_id='"+template+"' and tag='default';")==build
 save('template-during.json',{'template':T,'hugepages':during,'original_default_build':original(),'epoch':time.time()})
 print('Verified 2MiB template registered; L1 hugepages reserved 32 GiB.',flush=True)
def leave():
 if not STATE.exists():return
 before=json.loads(STATE.read_text());assert not subprocess.run(['pgrep','-x','firecracker'],capture_output=True).stdout,'VMM remains, cannot release hugepages'
 template=T['template'];build=T['build_id']
 sql(f"BEGIN; DELETE FROM env_build_assignments WHERE env_id='{template}'; DELETE FROM active_template_builds WHERE build_id='{build}'; DELETE FROM env_aliases WHERE env_id='{template}'; DELETE FROM env_builds WHERE id='{build}'; DELETE FROM envs WHERE id='{template}'; COMMIT;")
 key='template:info:{'+template+'}:default';deleted=subprocess.check_output(['docker','exec','ae-e2b-redis','redis-cli','DEL',key],text=True).strip()
 for p,n in before['hugepages'].items():Path(p).write_text(str(n))
 after=hp();result={'epoch':time.time(),'hugepages':after,'hugepages_match':after==before['hugepages'],'original_default_build':original(),'cache_deleted':int(deleted),'temporary_env_count':sql("select count(*) from envs where id='"+template+"';")}
 save('template-restored.json',result);assert result['hugepages_match'] and original()==before['original_default_build'] and result['temporary_env_count']=='0'
 STATE.rename(P/'template-before.restored.json');print('Original template binding preserved; temporary registration and hugepages restored.',flush=True)
if __name__=='__main__':{'enter':enter,'leave':leave}[sys.argv[1]]()
