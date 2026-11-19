import gzip,hashlib,json,pathlib,sys,time
P=pathlib.Path(__file__).resolve().parent
if sys.argv[1]=='start':
 paths=[pathlib.Path('/data/log/CubeMaster/cubemaster-req.log'),pathlib.Path('/data/log/Cubelet/Cubelet-req.log'),pathlib.Path('/data/log/CubeShim/cube-shim-req.log'),pathlib.Path('/data/log/CubeVmm/vmm.log')]
 paths += list(pathlib.Path('/data/log/CubeAPI').glob('*.log'))
 state=[{'path':str(f),'inode':f.stat().st_ino,'offset':f.stat().st_size} for f in paths if f.exists()]
 (P/'log-offsets.json').write_text(json.dumps(state,indent=2))
else:
 out=P/'raw-logs';out.mkdir(exist_ok=True);manifest=[]
 for r in json.loads((P/'log-offsets.json').read_text()):
  f=pathlib.Path(r['path']);assert f.stat().st_ino==r['inode'] and f.stat().st_size>=r['offset'],'log rotated: '+str(f)
  dest=out/(f.parent.name+'-'+f.name+'.gz')
  with f.open('rb') as src,gzip.open(dest,'wb') as dst:
   src.seek(r['offset'])
   while True:
    data=src.read(1024*1024)
    if not data:break
    dst.write(data)
  manifest.append({**r,'saved':dest.name,'sha256':hashlib.sha256(dest.read_bytes()).hexdigest()})
 (P/'log-manifest.json').write_text(json.dumps(manifest,indent=2))
 print('service log tails archived',flush=True)
