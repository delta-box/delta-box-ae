"""User-authorized bounded validation; interrupted pilot remains incomplete."""
from pathlib import Path
import hashlib,json

def validate(prefix, current_lock):
    events = prefix['events']
    assert prefix['completed_restores'] == len(events) >= 26
    assert len({r['before_seq'] for r in events}) == len(events)
    proofs = []
    for row in events:
        assert row['restored_state']['ok']
        assert row['selected_node_id'] == row['restored_state']['node_id']
        if row['merge']['n_diffs']:
            proof = row['merge']['validation']
            assert proof['ok'] and proof['sources_unchanged']
            assert proof['sha256'] == proof['reference_sha256']
            assert proof['depth'] == row['merge']['n_diffs']
            proofs.append(proof)
    assert prefix['validated_nonempty'] == len(proofs) >= 25
    assert prefix['max_depth'] == max(x['depth'] for x in proofs) >= 13
    allowed_changes = {'PLAN.md', 'run_batch_l1.py', 'run_l1.py'}
    for name, digest in prefix['measured_source_lock'].items():
        if name not in allowed_changes:
            assert current_lock[name] == digest, ('changed validated source', name)
    return {'accepted_completed_restores':len(events),'validated_nonempty_merges':len(proofs),
            'max_depth':max(x['depth'] for x in proofs),'full_pilot_complete':False,
            'reason':'User requested ending repetitive full-image hash validation; completed prefix retained; entire pilot excluded from performance means.'}

def accept(root):
    root=Path(root);path=root/'validation-handoff-20261109/accepted-prefix.json'
    raw=path.read_bytes();current=json.loads((root/'source-lock.json').read_text())
    assert hashlib.sha256(raw).hexdigest()==current[str(path.relative_to(root))]
    result=validate(json.loads(raw),current)
    result['prefix_sha256']=hashlib.sha256(raw).hexdigest()
    (root/'validation-acceptance.json').write_text(json.dumps(result,indent=2))
    return result
