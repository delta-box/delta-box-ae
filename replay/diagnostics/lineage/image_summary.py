"""Read the pinned CRIU pagemap protobuf wire format without third-party imports."""
import struct
from pathlib import Path

MAGIC=bytes.fromhex('1943565425400856')

def varint(data,at):
    value=0
    for shift in range(0,70,7):
        if at>=len(data):raise ValueError('truncated protobuf varint')
        byte=data[at];at+=1;value|=(byte&127)<<shift
        if not byte&128:return value,at
    raise ValueError('oversized protobuf varint')

def fields(data):
    result={};at=0
    while at<len(data):
        key,at=varint(data,at)
        if key&7 or key>>3 in result:raise ValueError('unexpected/duplicate protobuf field')
        value,at=varint(data,at);result[key>>3]=value
    return result

def summarize(path):
    data=Path(path).read_bytes()
    if data[:8]!=MAGIC:raise ValueError('unexpected CRIU pagemap magic')
    at=8;records=[]
    while at<len(data):
        if at+4>len(data):raise ValueError('truncated image record length')
        size=struct.unpack_from('<I',data,at)[0];at+=4
        if at+size>len(data):raise ValueError('truncated image record')
        records.append(fields(data[at:at+size]));at+=size
    if not records or set(records[0])!={1}:raise ValueError('bad pagemap header')
    counts={'pages_id':records[0][1],'total_pages':0,'parent_pages':0,'present_pages':0,'lazy_eligible_pages':0,'parent_lazy_pages':0,'entries':len(records)-1}
    for rec in records[1:]:
        if not {1,2}.issubset(rec) or set(rec)-{1,2,3,4,5}:raise ValueError('bad pagemap entry')
        pages=rec.get(5,rec[2]);flags=rec.get(4,1 if rec.get(3) else 4)
        if flags&1 and flags&2:counts['parent_lazy_pages']+=pages
        counts['total_pages']+=pages
        for name,bit in [('parent_pages',1),('present_pages',4),('lazy_eligible_pages',2)]:
            if flags&bit:counts[name]+=pages
    return counts
