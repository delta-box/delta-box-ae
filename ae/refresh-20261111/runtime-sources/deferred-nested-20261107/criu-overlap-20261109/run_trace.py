#!/usr/bin/env python3
"""Live concurrent checkpoint and recorded-RTT request; no synthetic subtraction."""
import argparse,concurrent.futures as cf,fcntl,json,os,shutil,signal,sys,time,urllib.request
from pathlib import Path
import probe_base as b

def main(args):
    item=next(x for x in json.loads((b.HERE/'inputs.json').read_text()) if x['instance']==args.instance)
    assert 1<=args.max_steps<=item['old_max_steps']==29
    d=b.prepare(args);out=args.out
    os.environ['OVERLAP_CACHE']=str(out/'request-cache.json')
    os.environ['OVERLAP_MOCK_EVENTS']=str(out/'mock-request-events.jsonl')
    worker=out/'driver/criu_worker.py';text=worker.read_text();anchor='if __name__ == "__main__":'
    assert text.count(anchor)==1
    worker.write_text(text.replace(anchor,'from overlap_worker import install\ninstall(globals())\n\n'+anchor))
    shutil.copy2(b.HERE/'overlap_worker.py',worker.parent/'overlap_worker.py')
    mock_source=out/'payload/mock_llm_server.py';text=mock_source.read_text();assert text.count(anchor)==1
    mock_source.write_text(text.replace(anchor,'from mock_trace_hook import install\ninstall(MockHandler)\n\n'+anchor))
    shutil.copy2(b.HERE/'mock_trace_hook.py',mock_source.parent/'mock_trace_hook.py')
    meta=json.loads((out/'manifest.json').read_text());meta.update(max_steps=args.max_steps,
        timing='continuous checkpoint; external recorded-RTT request starts before checkpoint; no action until both complete',
        request_validation='prepared/consumed canonical JSON SHA256 must match',
        measurement_sources=[{'path':str(p),'sha256':b.filehash(p)} for p in [worker,worker.parent/'overlap_worker.py',mock_source,mock_source.parent/'mock_trace_hook.py']])
    b.write(out/'manifest.json',meta)
    work=out/'work';work.mkdir();logs=out/'logs';logs.mkdir();repo_base=work/'repo_base';repo_base.mkdir()
    live=repo_base/f'swe-bench_{args.instance}';mailbox=work/'mailbox'
    assert d.rsync_copy(d.SOURCE_REPOS/f'swe-bench_{args.instance}',live,delete=True)['rc']==0
    mock=proc=pid=None;checkpoints=[];restores=[];steps=[];errors=[]
    pool=cf.ThreadPoolExecutor(max_workers=2)
    def rpc(op,**kw):
        value=d.worker_rpc(mailbox,{'op':op,**kw},timeout_s=300)
        if not value.get('ok'):raise RuntimeError(str(value))
        return value
    def emit(kind,row):
        with (out/(kind+'.jsonl')).open('a') as f:f.write(json.dumps(row)+'\n');f.flush()
    def infer(request,response_path):
        start=b.now();req=urllib.request.Request(request['url'],data=request['body'].encode(),headers={'Content-Type':'application/json','Authorization':'Bearer dummy'})
        with urllib.request.urlopen(req,timeout=300) as response:
            result={'status':response.status,'body':response.read().decode()}
        end=b.now();b.write(response_path,result)
        return {'start_ns':start,'end_ns':end,'cache_written_ns':b.now(),'wall_ms':(end-start)/1e6,'request_sha256':request['sha256']}
    def mock_marks():
        p=out/'mock-request-events.jsonl'
        if not p.exists():return []
        return [json.loads(x) for x in p.read_text().split('\n')[:-1] if x.strip()]
    try:
        port=d.free_tcp_port();mock=d.start_mock(args.instance,port,logs/'mock.log',out/'mock-audit.json')
        proc=d.start_worker(args.instance,repo_base,mailbox,port,logs/'worker.log');pid=proc.pid
        initial=proc._finalbench_init;tree,state=initial['tree'],initial['state'];node_id=0;active_seq=None
        b.write(out/'owned-processes.json',{'mock_pid':mock.pid,'worker_pid':pid})
        for seq in range(args.max_steps+1):
            if (b.HERE/'STOP').exists():raise RuntimeError('cohort STOP requested')
            h0=b.now();before=b.treehash(live);hash_ms=(b.now()-h0)/1e6
            selected=request=None;prepare_ms=0.0
            if seq<args.max_steps:
                selection=rpc('select',tree=tree)
                if not selection.get('finished'):
                    selected=selection['selected_node_id'];t=b.now()
                    prepared=rpc('overlap_prepare',tree=tree,selected_node_id=selected);prepare_ms=(b.now()-t)/1e6
                    request=prepared['request'];assert request is not None,'No request captured'
                    h0=b.now();assert b.treehash(live)==before,'Preparing prompt changed filesystem';hash_ms+=(b.now()-h0)/1e6
            snap=work/'snapshots'/f'seq{seq}';img=work/'images'/f'seq{seq}'
            parent=checkpoints[active_seq] if active_seq is not None else None;marks={}
            def checkpoint():
                marks['start_ns']=b.now()
                result=d.checkpoint_state(mailbox=mailbox,worker_pid=pid,live_repo=live,fs_snap=snap,
                    link_dest=Path(parent['fs_snapshot']) if parent else None,image_dir=img,
                    prev_dir=Path(parent['criu_images']) if parent else None,log_file='dump.log')
                marks['end_ns']=b.now();assert result[0]['rc']==result[1]['rc']==0,result
                return result
            inference=None;received=None
            if request:
                response_path=out/'requests'/f'{seq}.response.json';consumed=out/'requests'/f'{seq}.consumed.json'
                b.write(out/'request-cache.json',{'request_sha256':request['sha256'],'response':str(response_path),'consumed':str(consumed)})
                emit('requests',{'checkpoint_seq':seq,'next_selected_node':selected,**request})
                starts_before=sum(m['kind']=='start' for m in mock_marks());future=pool.submit(infer,request,response_path)
                deadline=time.monotonic()+10
                while received is None:
                    starts=[m for m in mock_marks() if m['kind']=='start']
                    if len(starts)>starts_before:received=starts[starts_before];break
                    if future.done():future.result()
                    if time.monotonic()>deadline:raise RuntimeError('mock request-receipt mark timeout')
                    time.sleep(.0005)
                ck_future=pool.submit(checkpoint);inference=future.result();gate0=b.now();fs,cr,span=ck_future.result();gate1=b.now()
                assert received['t_ns']<=marks['start_ns']
            else:
                gate0=b.now();fs,cr,span=checkpoint();gate1=b.now()
            row={'seq':seq,'node_id':node_id,'parent_checkpoint_seq':active_seq,'state':state,
                 'fs_snapshot':str(snap),'criu_images':str(img),'filesystem':before,'fs':fs,'criu_dump':cr,
                 'parallel_span_ms':span,'checkpoint_start_ns':marks['start_ns'],'checkpoint_end_ns':marks['end_ns'],
                 'checkpoint_wall_ms':(marks['end_ns']-marks['start_ns'])/1e6,
                 'prepare_next_request_ms':prepare_ms,'filesystem_audit_ms':hash_ms,'inference':inference,
                 'mock_received':received,'gate_start_ns':gate0,'gate_end_ns':gate1,'gate_wait_ms':(gate1-gate0)/1e6,
                 'unhidden_checkpoint_ms':max(0,marks['end_ns']-inference['end_ns'])/1e6 if inference else None,
                 'response_to_gate_end_ms':(gate1-inference['end_ns'])/1e6 if inference else None,
                 'overlap_ms':max(0,min(marks['end_ns'],inference['end_ns'])-max(marks['start_ns'],inference['start_ns']))/1e6 if inference else 0,
                 'fully_hidden':marks['end_ns']<=inference['end_ns'] if inference else False,
                 'next_selected_node':selected,'aligned_ck':node_id in item['checkpoint_nodes'],
                 'no_inference_reason':None if request else 'terminal_or_step_limit'}
            checkpoints.append(row);emit('checkpoints',row);active_seq=seq
            print(json.dumps({'seq':seq,'node':node_id,'ck_ms':row['checkpoint_wall_ms'],'remaining_ms':row['unhidden_checkpoint_ms']}),flush=True)
            if selected is None:break
            if selected!=node_id:
                target=next(c for c in reversed(checkpoints) if c['node_id']==selected)
                with open('/tmp/dyp-cold.lock','w') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX)
                    ev_ms,ev_n=d._cold_evict([work/'snapshots',work/'images',live,Path(os.environ['MOATLESS_VENV']),out/'payload/moatless-det-src'])
                    t0=b.now();d.kill_discarded_worker(proc,pid);proc=None;pid=None;t1=b.now()
                    if d.LAZY_DAEMONS:d.settle_lazy_pages()
                    t2=b.now();fsr=d.rsync_copy(Path(target['fs_snapshot']),live,delete=True);t3=b.now()
                    crr=d.criu_restore(Path(target['criu_images']),log_file=f'restore-before-{seq+1}.log');t4=b.now()
                    assert fsr['rc']==crr['rc']==0,(fsr,crr)
                    restored=rpc('state')['state'];pid=restored['pid'];t5=b.now()
                assert restored['checkpoint_seq']==target['state']['checkpoint_seq'] and restored['node_id']==target['state']['node_id'],restored
                h0=b.now();assert b.treehash(live)==target['filesystem'],'Restored filesystem mismatch';verify_ms=(b.now()-h0)/1e6
                rr={'before_seq':seq+1,'selected_node_id':selected,'target_seq':target['seq'],'restore_start_ns':t0,'restore_end_ns':t5,
                    'restore_api_wall_ms':(t5-t0)/1e6,'fs':fsr,'criu':crr,'state':restored,'filesystem_matches':True,
                    'kill_ms':(t1-t0)/1e6,'lazy_settle_ms':(t2-t1)/1e6,'fs_interval_ms':(t3-t2)/1e6,
                    'criu_interval_ms':(t4-t3)/1e6,'state_rpc_ms':(t5-t4)/1e6,'evict_ms':ev_ms,'evict_files':ev_n,'filesystem_audit_ms':verify_ms}
                restores.append(rr);emit('restores',rr);active_seq=target['seq']
            if request:assert not consumed.exists(),'Prepared response consumed before step'
            step_start=b.now();step=rpc('step',seq=seq+1,tree=tree,selected_node_id=selected);step_end=b.now()
            if request:
                assert consumed.exists(),'Step did not consume prepared response'
                assert json.loads(consumed.read_text())['request_sha256']==request['sha256']
            tree,state,node_id=step['tree'],step['state'],step['node_id']
            record={k:v for k,v in step.items() if k!='tree'};record.update(start_ns=step_start,end_ns=step_end,response_consumed_observed_ns=b.now())
            steps.append(record);emit('steps',record)
        assert len(steps)==args.max_steps and [c['node_id'] for c in checkpoints]==list(range(args.max_steps+1))
        observed=[r['selected_node_id'] for r in restores]
        assert observed==item['restore_nodes'][:len(observed)]
        if args.max_steps==29:assert observed==item['restore_nodes']
        b.write(out/'final-tree.json',tree)
    except BaseException as exc:
        errors.append(repr(exc));b.write(out/'failure.json',{'error':repr(exc)});raise
    finally:
        primary=sys.exc_info()[0] is not None
        pool.shutdown(wait=True)
        if pid:
            try:d.kill_discarded_worker(proc,pid)
            except Exception as exc:errors.append('worker cleanup: '+repr(exc))
        if mock:
            try:d.flush_audit(f'http://127.0.0.1:{port}',out/'mock-audit.json')
            except Exception as exc:errors.append('mock audit: '+repr(exc))
            finally:d.stop_proc(mock)
        for daemon in d.LAZY_DAEMONS:
            if daemon.poll() is None:
                daemon.terminate()
                try:daemon.wait(timeout=10)
                except Exception:daemon.kill();daemon.wait()
        for path in work.rglob('*.log'):
            dest=out/'checkpoint-logs'/path.relative_to(work);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dest)
        for path in [work,out/'payload/repos',out/'nltk_data']:
            if path.is_dir():shutil.rmtree(path)
        b.write(out/'cleanup.json',{'ok':not errors,'errors':errors,'worker_pid':pid,'mock_pid':mock.pid if mock else None,'work_removed':not work.exists()})
        if errors and not primary:raise RuntimeError(errors)
    audit=json.loads((out/'mock-audit.json').read_text());stats=audit['stats']
    if args.max_steps==29:
        exp=item['expected_final_mock'];assert stats['cursor']==exp['cursor'] and stats['total']==exp['total'],(stats,exp)
    assert stats.get('n_protocol_errors',0)==0,stats
    b.write(out/'summary.json',{'status':'ok','instance':args.instance,'group':item['group'],
            'n_checkpoints':len(checkpoints),'n_restores':len(restores),'n_steps':len(steps),
            'prepared_request_matches':True,'all_restore_checks':True,'mock_stats':stats,
            'with_inference':sum(c['inference'] is not None for c in checkpoints),
            'aligned_ck':sum(c['aligned_ck'] for c in checkpoints),
            'aligned_with_inference':sum(c['aligned_ck'] and c['inference'] is not None for c in checkpoints),
            'fully_hidden':sum(c['fully_hidden'] for c in checkpoints),'terminal_unmasked':sum(c['inference'] is None for c in checkpoints)})

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--instance',required=True);parser.add_argument('--out',type=Path,required=True);parser.add_argument('--max-steps',type=int,default=29)
    args=parser.parse_args()
    def stop(*_):raise KeyboardInterrupt('owned experiment stopped')
    signal.signal(signal.SIGTERM,stop)
    main(args)
