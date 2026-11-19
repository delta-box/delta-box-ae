import hashlib,json,statistics
from pathlib import Path

def rows(p):return [json.loads(x) for x in Path(p).read_text().splitlines() if x.strip()]
def audit(p,item):
    p=Path(p);summary=json.loads((p/'summary.json').read_text());ck=rows(p/'checkpoints.jsonl');rs=rows(p/'restores.jsonl') if (p/'restores.jsonl').exists() else []
    assert summary['status']=='ok' and json.loads((p/'cleanup.json').read_text())['ok']
    n=json.loads((p/'manifest.json').read_text())['max_steps'];assert len(ck)==n+1
    assert [x['node_id'] for x in ck]==list(range(n+1))
    assert [x['selected_node_id'] for x in rs]==item['restore_nodes'][:len(rs)]
    if n==29:assert len(rs)==len(item['restore_nodes'])
    req=rows(p/'requests.jsonl');assert len(req)==n
    steps=rows(p/'steps.jsonl');assert len(steps)==n
    for c in ck:
        start,end=c['checkpoint_start_ns'],c['checkpoint_end_ns'];assert end>start
        assert abs(c['checkpoint_wall_ms']-(end-start)/1e6)<1e-8
        assert c['fs']['rc']==c['criu_dump']['rc']==0
        assert c['aligned_ck']==(c['node_id'] in item['checkpoint_nodes'])
        if c['inference']:
            llm=c['inference'];assert llm['start_ns']<=c['mock_received']['t_ns']<=start
            assert abs(c['unhidden_checkpoint_ms']-max(0,end-llm['end_ns'])/1e6)<1e-8
            assert c['gate_end_ns']>=max(end,llm['end_ns'])
            assert c['fully_hidden']==(end<=llm['end_ns'])
            consumed=json.loads((p/'requests'/f"{c['seq']}.consumed.json").read_text())
            assert consumed['request_sha256']==llm['request_sha256']==req[c['seq']]['sha256']
            body=req[c['seq']]['body'];digest=hashlib.sha256(json.dumps(json.loads(body),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
            assert digest==consumed['request_sha256']
            # Pilot observed a clock-domain mismatch after worker restoration.
            # Compare ordering only in the external controller's clock domain.
            assert steps[c['seq']]['start_ns']>=c['gate_end_ns']
            assert steps[c['seq']]['end_ns']>steps[c['seq']]['start_ns']
        else:assert c['seq']==n
    for r in rs:
        assert r['filesystem_matches'] and r['fs']['rc']==r['criu']['rc']==0
        assert abs(r['restore_api_wall_ms']-(r['restore_end_ns']-r['restore_start_ns'])/1e6)<1e-8
        assert abs(r['restore_api_wall_ms']-sum(r[k] for k in ['kill_ms','lazy_settle_ms','fs_interval_ms','criu_interval_ms','state_rpc_ms']))<1e-6
    return dict(instance=item['instance'],group=item['group'],steps=n,ck=len(ck),aligned_ck=sum(c['aligned_ck'] for c in ck),rs=len(rs),with_inference=n,
                tail_mean_ms=statistics.mean(c['unhidden_checkpoint_ms'] for c in ck if c['inference']),
                summary_sha256=hashlib.sha256((p/'summary.json').read_bytes()).hexdigest(),path=str(p))
