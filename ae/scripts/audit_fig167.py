#!/usr/bin/env python3
"""Flag >50% changes, including incremental overhead hidden by a ratio near 1."""
import argparse,hashlib,json,math
from pathlib import Path

FIELDS=('backend','operation','group','arm','metric')

def identity(figure,row):
    return '/'.join([figure,*[str(row.get(k,'')) for k in FIELDS]])

def changes(fresh,reference):
    rows=[]
    for name in ('figure-01','figure-06','figure-07'):
        old={identity(name,r):r for r in reference['experiments'][name]['metrics']}
        seen=set()
        for now in fresh['experiments'][name]['metrics']:
            key=identity(name,now)
            if key not in old:continue
            if key in seen:raise ValueError('Choose one explicit population for '+key)
            seen.add(key);ref=old[key]
            v,b=now['value'],ref['value']
            if v is None or b is None:continue
            if not all(isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) for x in (v,b)):
                raise ValueError('Invalid comparison value')
            pairs=[(key,v,b)]
            if name=='figure-07' and now['metric']=='ratio':pairs.append((key+'-minus-one',v-1,b-1))
            for label,value,base in pairs:
                delta=None if base==0 else (value-base)/abs(base)
                rows.append(dict(id=label,current=value,reference=base,relative_change=delta,
                    exceeds_50_percent=abs(delta)>.5 if delta is not None else value!=0,
                    current_n=now.get('n'),reference_n=ref.get('n'),unit=now['unit'],
                    reference_evidence_kind=ref.get('evidence_kind'),
                    comparison_note=('Historical published adjustments are not directly measured phase observations.'
                        if ref.get('evidence_kind')=='published_adjustment' else None)))
    return rows

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--fresh',type=Path,required=True);ap.add_argument('--reference',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    a=ap.parse_args();rows=changes(json.loads(a.fresh.read_text()),json.loads(a.reference.read_text()))
    def record(p):return dict(path=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest())
    out=dict(threshold=.5,rows=rows,flagged=[r['id'] for r in rows if r['exceeds_50_percent']],
             inputs=[record(a.fresh),record(a.reference)],
             interpretation='Flags require documented investigation, not automatic rejection of valid changed protocols or adjustment of measured values.')
    a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(out,indent=2)+'\n')
    print(f'{len(rows)} comparisons, {len(out["flagged"])} require investigation')
    return 0

if __name__=='__main__':raise SystemExit(main())
