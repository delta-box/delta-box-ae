#!/usr/bin/env python3
"""Download, verify and assemble the DeltaBox v3 appliance image."""
import argparse, concurrent.futures, hashlib, json, shutil, subprocess, time, urllib.request
from pathlib import Path
from urllib.parse import quote

RECORD=24136571
def url(name): return f'https://zenodo.org/records/{RECORD}/files/{quote(name)}?download=1'
def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for data in iter(lambda:f.read(8*2**20),b''):h.update(data)
    return h.hexdigest()
def fetch(name):
    return urllib.request.urlopen(urllib.request.Request(url(name),headers={'User-Agent':'DeltaBox-AE/3'}),timeout=90)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output-dir',type=Path,default=Path.cwd())
    p.add_argument('--jobs',type=int,default=4)
    p.add_argument('--extract',action='store_true',help='Also decompress and verify the qcow2; requires zstd')
    a=p.parse_args();dest=a.output_dir.resolve();dest.mkdir(parents=True,exist_ok=True)
    if not 1<=a.jobs<=16:p.error('Choose 1–16 download workers')
    with fetch('APPLIANCE.json') as r:m=json.load(r)
    assert m['record_id']==RECORD and m['version']=='deltabox-atc2026-artifact-v3'
    for name in [m['image_name'],m['compressed_name'],*[x['name'] for x in m['parts']]]:
        assert Path(name).name==name and name.startswith('DeltaBox-ATC2026-v3-l1.qcow2')
    compressed=dest/m['compressed_name'];raw=dest/m['image_name']
    if compressed.exists():
        if sha(compressed)!=m['compressed_sha256']:raise RuntimeError('Existing compressed image has a different checksum; left unchanged')
    else:
        needed=2*m['compressed_size']+(m['uncompressed_size'] if a.extract else 0)
        if shutil.disk_usage(dest).free<needed:raise RuntimeError(f'Allow at least {needed/2**30:.1f} GiB free for download and assembly')
        parts=dest/'.deltabox-v3-download-parts';parts.mkdir(exist_ok=True)
        def download(row):
            path=parts/row['name']
            if path.exists() and path.stat().st_size==row['size'] and sha(path)==row['sha256']:
                print('Already verified:',path.name,flush=True);return
            tmp=path.with_suffix(path.suffix+'.partial')
            for attempt in range(3):
                try:
                    with fetch(row['name']) as r,tmp.open('wb') as f:shutil.copyfileobj(r,f,2**20)
                    assert tmp.stat().st_size==row['size'] and sha(tmp)==row['sha256'],'Part checksum mismatch'
                    tmp.replace(path);print('Verified:',path.name,flush=True);return
                except Exception:
                    if attempt==2:raise
                    time.sleep(2*(attempt+1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=a.jobs) as pool:
            list(pool.map(download,m['parts']))
        tmp=compressed.with_suffix(compressed.suffix+'.partial');h=hashlib.sha256()
        with tmp.open('wb') as out:
            for row in m['parts']:
                with (parts/row['name']).open('rb') as f:
                    for block in iter(lambda:f.read(2**20),b''):out.write(block);h.update(block)
        assert tmp.stat().st_size==m['compressed_size'] and h.hexdigest()==m['compressed_sha256']
        tmp.replace(compressed)
        for row in m['parts']:(parts/row['name']).unlink()
        try:parts.rmdir()
        except OSError:pass
    (dest/'APPLIANCE.json').write_text(json.dumps(m,indent=2)+'\n')
    if a.extract:
        if raw.exists():
            if sha(raw)!=m['uncompressed_sha256']:raise RuntimeError('Existing qcow2 differs; left unchanged')
        else:
            if not shutil.which('zstd'):raise RuntimeError('Install zstd, then rerun with --extract; downloaded image is retained')
            tmp=raw.with_suffix('.qcow2.partial')
            subprocess.run(['zstd','-d','--long=31','-f','--keep',str(compressed),'-o',str(tmp)],check=True)
            assert tmp.stat().st_size==m['uncompressed_size'] and sha(tmp)==m['uncompressed_sha256']
            tmp.replace(raw)
        print('Verified appliance:',raw)
    else:print('Verified compressed image:',compressed)

if __name__=='__main__':main()
