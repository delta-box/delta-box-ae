from pathlib import Path
import ctypes,json,sys,struct,subprocess,mmap,random,hashlib,importlib.util,argparse
parser=argparse.ArgumentParser();parser.add_argument('--candidate',type=Path,required=True);parser.add_argument('--receipt',type=Path,required=True);parser.add_argument('--work',type=Path,required=True);parser.add_argument('--captured-snapshots',type=Path);args=parser.parse_args()
A=args.work.resolve();A.mkdir(parents=True,exist_ok=True);candidate=args.candidate.resolve();assert candidate.is_file()
sp=importlib.util.spec_from_file_location('patch',Path(__file__).parent/'make_classifier_patch.py');p=importlib.util.module_from_spec(sp);sp.loader.exec_module(p)
data=candidate.read_bytes();rec=json.loads(args.receipt.read_text());assert hashlib.sha256(data).hexdigest()==rec['candidate_sha256']
off=p.offset_for(data,0x94e3d9,0x94e45b-0x94e3d9);assert data[p.offset_for(data,p.VADDR,len(p.NEW)):][:len(p.NEW)]==p.NEW
asm=f'''.text
.globl classify
.type classify,@function
classify:
 push %rbp
 push %rbx
 push %r12
 push %r13
 push %r14
 push %r15
 sub $0x48,%rsp
 mov %rdi,0x18(%rsp)
 mov %rdx,0x20(%rsp)
 movabs $0x007fffffffffffff,%rax
 mov %rax,0x40(%rsp)
 mov %rsi,%r14
 mov %rsi,%r13
 neg %r13
 xor %r12,%r12
 mov %rdx,%rdi
 jmp .Lbody+0x10
.Lbody:
 .incbin "{candidate}",{off},{0x94e45b-0x94e3d9}
 xor %eax,%eax
 jmp .Lend
 .fill {0x94e4ba-0x94e3d9}-(.-.Lbody),1,0x90
 mov $-1,%eax
.Lend:
 add $0x48,%rsp
 pop %r15
 pop %r14
 pop %r13
 pop %r12
 pop %rbx
 pop %rbp
 ret
.size classify,.-classify
.section .note.GNU-stack,"",@progbits
'''
(A/'classifier-harness.S').write_text(asm);subprocess.run(['gcc','-shared','-fPIC','-o',str(A/'classifier-harness.so'),str(A/'classifier-harness.S')],check=True)
lib=ctypes.CDLL(str(A/'classifier-harness.so'));f=lib.classify;f.argtypes=[ctypes.POINTER(ctypes.c_uint64),ctypes.c_size_t,ctypes.POINTER(ctypes.c_uint8)];f.restype=ctypes.c_int
PRESENT=1<<63;SWAP=1<<62;FILE=1<<61;MASK=(1<<55)-1
def run(entries):
 n=len(entries);a=(ctypes.c_uint64*n)(*entries);b=(ctypes.c_uint8*n)();rc=f(a,n,b);return rc,bytes(b)
def reference(x):
 if x&SWAP:return 1
 if not x&PRESENT:return 0
 if not x&MASK:return -1
 return int(not x&FILE)
tests=[]
for name,entries in [('empty',[]),('not-present',[0,FILE,1]),('anonymous-cow',[PRESENT|1,PRESENT|2|(1<<55)]),('file-readonly',[PRESENT|FILE|1]),('swapped',[SWAP|3,SWAP|FILE|3]),('pfn-permission-denied',[PRESENT]),('file-pfn-permission-denied',[PRESENT|FILE])]:
 rc,b=run(entries);refs=[reference(x) for x in entries];want=-1 if -1 in refs else 0;assert rc==want,(name,rc)
 if rc==0:assert b==bytes(refs),(name,b,refs)
 tests.append(name)
rng=random.Random(4192);entries=[rng.getrandbits(64) for _ in range(32768)];rc,b=run(entries);assert rc==0 and b==bytes(reference(x) for x in entries);tests.append('32768-random-pagemap-values')
# Candidate rejects unrelated, corrupt and already-patched inputs, never guessing.
for b0 in [b'bad-ELF',data,bytes([data[0]^1])+data[1:]]:
 try:p.build(b0)
 except ValueError:pass
 else:raise AssertionError('wrong input accepted')
tests.append('wrong-binary-and-repeat-patch-rejected')
results=[]
for cfile in args.captured_snapshots.glob('paused-*/comparison.json') if args.captured_snapshots else []:
 S=args.captured_snapshots
 c=json.loads(cfile.read_text());sid=cfile.parent.name.removeprefix('paused-');sel=S/('selection-'+sid);raw=(sel/'pagemap-classifier-input.bin').read_bytes();entries=struct.unpack('<131072Q',raw);rc,bitmap=run(entries);assert rc==0 and bitmap==bytes(reference(x) for x in entries)
 original=(sel/'selected-pages.bin').read_bytes();missing=c['different_pages'];assert all(original[i]==0 and bitmap[i]==1 for i in missing)
 with (cfile.parent/'source-ram.bin').open('rb') as sf,(cfile.parent/'snapshot-memory.bin').open('rb') as bf:
  with mmap.mmap(sf.fileno(),0,access=mmap.ACCESS_READ) as source,mmap.mmap(bf.fileno(),0,access=mmap.ACCESS_READ) as base:
   unresolved=[i for i in range(len(bitmap)) if not bitmap[i] and source[i*4096:(i+1)*4096]!=base[i*4096:(i+1)*4096]]
 assert not unresolved,unresolved[:10]
 results.append({'source':sid,'captured_input_sha256':hashlib.sha256(raw).hexdigest(),'actual_original_selected':sum(original),'candidate_selected':sum(bitmap),'actual_original_missing_pages':len(missing),'all_original_missing_pages_selected':True,'remaining_mismatches_after_candidate_page_overlay':len(unresolved),'boundary':'offline replay against captured source and old snapshot, not live VM qualification'})
out={'status':'passed','tests':tests,'actual_instruction_bytes_executed':True,'binary_patch':rec,'captured_data_regressions':results}
(A/'candidate-tests.json').write_text(json.dumps(out,indent=2)+'\n');print(json.dumps(out))
