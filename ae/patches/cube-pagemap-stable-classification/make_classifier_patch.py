from pathlib import Path
import argparse,hashlib,json,struct
ORIGINAL_SHA='6e024b582a44167f93e868c9c26b3057dbafb19d5680dd08575a7f35146ca42c'
VADDR=0x94e40e
OLD=bytes.fromhex('48c1e103488d7c24088b6c240489ee31d2e86c07d0ff48837c2408000f85a1000000ba0800000089ef488d742438e84fa7d8ff4889c54885c00f85eb000000f644243910488b7c24207580eb83')
# Reload the SAME cached pagemap entry; bit61 distinguishes private CoW from
# backing-file pages without looking up a PFN after it may have moved.
# Keep the original present/swapped branches, PFN0 rejection, file opens,
# pagemap I/O, allocation, cleanup and ABI. No other binary bytes change.
NEW_CODE=bytes.fromhex('488b4424184a8b0ce0480fbae13d488b7c242073b6ebb9')
NEW=NEW_CODE+b'\x90'*(len(OLD)-len(NEW_CODE))
def offset_for(data,address,size):
 if data[:6]!=b'\x7fELF\x02\x01':raise ValueError('expected ELF64 little-endian')
 if struct.unpack_from('<H',data,18)[0]!=62:raise ValueError('expected x86-64')
 phoff=struct.unpack_from('<Q',data,32)[0];ents,n=struct.unpack_from('<HH',data,54);found=[]
 for i in range(n):
  typ,flags,off,va,_,fs,ms,align=struct.unpack_from('<IIQQQQQQ',data,phoff+i*ents)
  if typ==1 and flags&1 and va<=address and address+size<=va+fs:found.append(off+address-va)
 if len(found)!=1:raise ValueError('patch does not uniquely map to executable file segment')
 return found[0]
def build(data):
 if hashlib.sha256(data).hexdigest()!=ORIGINAL_SHA:raise ValueError('unrecognized input SHA; refusing patch')
 off=offset_for(data,VADDR,len(OLD))
 if data[off:off+len(OLD)]!=OLD:raise ValueError('original instruction block mismatch')
 out=data[:off]+NEW+data[off+len(OLD):]
 assert out[:off]==data[:off] and out[off+len(OLD):]==data[off+len(OLD):]
 return out,{'original_sha256':ORIGINAL_SHA,'candidate_sha256':hashlib.sha256(out).hexdigest(),'virtual_address':hex(VADDR),'file_offset':off,'replacement_extent':len(OLD),'old_hex':OLD.hex(),'new_hex':NEW.hex(),'outside_extent_byte_identical':True,'scope':'Private MAP_PRIVATE Cube guest RAM; present non-file pages and swapped pages selected. Original PFN0/permission, input read and size checks retained. No per-PFN kpageflags lookup. Zero-page copies may conservatively increase; shared-file pages excluded as before.'}
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('original',type=Path);p.add_argument('candidate',type=Path);p.add_argument('receipt',type=Path);a=p.parse_args()
 out,r=build(a.original.read_bytes())
 if a.candidate.exists() or a.receipt.exists():raise ValueError('fresh candidate and receipt required')
 a.candidate.write_bytes(out);a.candidate.chmod(0o755);a.receipt.write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))
