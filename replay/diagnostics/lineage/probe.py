#!/usr/bin/env python3
"""Owned PID-namespace fork/exact-parent/cold-restore correctness probe, not a benchmark."""
import argparse, ctypes, hashlib, json, os, signal, shutil, subprocess, time
from image_summary import summarize
from pathlib import Path

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out',type=Path,required=True)
    ap.add_argument('--criu',type=Path,required=True)
    ap.add_argument('--stock-criu','--restore-criu',dest='stock_criu',type=Path,required=True)
    ap.add_argument('--worker',type=Path,required=True)
    ap.add_argument('--lazy',action='store_true',help='Restore via userfaultfd and check parent-page eligibility; needs patched parent reader')
    a=ap.parse_args();a.out=a.out.resolve();a.out.mkdir(parents=True,exist_ok=False)
    if ctypes.CDLL(None,use_errno=True).prctl(36,1,0,0,0):raise OSError('subreaper')
    result={'passed':False,'lazy_restore':a.lazy,'steps':[],'kernel':os.uname().release,'affinity':sorted(os.sched_getaffinity(0)), 'criu_sha256':hashlib.sha256(a.criu.read_bytes()).hexdigest(),
            'stock_criu_sha256':hashlib.sha256(a.stock_criu.read_bytes()).hexdigest(),
            'restore_binary':str(a.stock_criu),
            'worker_sha256':hashlib.sha256(a.worker.read_bytes()).hexdigest(),
            'probe_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'summary_parser_sha256':hashlib.sha256(Path(__file__).with_name('image_summary.py').read_bytes()).hexdigest()}
    env=dict(os.environ,DELTABOX_CRIU_CAPABILITIES='1')
    result['capabilities']=subprocess.check_output([str(a.criu),'--version'],env=env,text=True)
    assert 'DeltaBox capabilities: exact-parent-v1' in result['capabilities']
    owned={}; daemons=[]; seq=0
    def ident(pid):return int((Path('/proc')/str(pid)/'stat').read_text().rpartition(')')[2].split()[19])
    def own(pid):owned[pid]=ident(pid);return pid
    def kill(pid):
        if pid in owned:
            try:
                assert ident(pid)==owned[pid];os.kill(pid,signal.SIGKILL)
                try:os.waitpid(pid,0)
                except ChildProcessError:
                    until=time.monotonic()+5
                    while (Path('/proc')/str(pid)).exists() and time.monotonic()<until:time.sleep(.01)
            except (ProcessLookupError,FileNotFoundError):pass
            owned.pop(pid)
    def wait(path):
        until=time.monotonic()+20
        while not path.exists():
            if time.monotonic()>until:raise TimeoutError(str(path))
            time.sleep(.005)
        return json.loads(path.read_text())
    def cmd(kind,x=0,y=0):
        nonlocal seq
        seq+=1;(a.out/'command.tmp').write_text(f'{seq} {kind} {x} {y}\n');(a.out/'command.tmp').replace(a.out/'command')
        r=wait(a.out/f'response-{seq}.json');result['steps'].append({'command':kind,'x':x,'y':y,'response':r});return r
    def memory_digest(record):
        fd=os.open(f"/proc/{record['host_pid']}/mem",os.O_RDONLY);h=hashlib.sha256()
        try:
            for offset in range(0,record['bytes'],1024*1024):
                size=min(1024*1024,record['bytes']-offset)
                data=os.pread(fd,size,record['address']+offset)
                if len(data)!=size:raise ValueError('short independent memory read')
                h.update(data)
        finally:os.close(fd)
        return h.hexdigest()
    def fork():
        nonlocal seq
        seq+=1;(a.out/'command.tmp').write_text(f'{seq} f 0 0\n');(a.out/'command.tmp').replace(a.out/'command')
        s=wait(a.out/f'snapshot-{seq}.json');own(s['host_pid']);os.kill(s['host_pid'],signal.SIGSTOP)
        wait(a.out/f'response-{seq}.json');assert s['namespace_pid']==1;s['starttime']=ident(s['host_pid']);s['independent_sha256']=memory_digest(s);result['steps'].append({'snapshot':s});return s
    def invoke(command,exact=False,expect_failure=False):
        env=os.environ.copy();env['DELTABOX_CRIU_EXACT_PARENT']='1' if exact else '0'
        p=subprocess.run(list(map(str,command)),capture_output=True,text=True,timeout=45,env=env)
        result['steps'].append({'exec':list(map(str,command)),'exact':exact,'rc':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
        if expect_failure:
            if not p.returncode:raise RuntimeError('invalid parent unexpectedly accepted')
        elif p.returncode:raise RuntimeError('CRIU failed; see step logs')
    flags=['--shell-job','--file-locks','--tcp-close','--ext-unix-sk']
    def dump(s,name,parent=None,exact=True,expect_failure=False):
        path=a.out/name;path.mkdir()
        command=[a.criu if exact else a.stock_criu,'dump','-t',s['host_pid'],'-D',path,'--leave-stopped',*flags,'-v4','-o','dump.log']
        if parent:command+=['--prev-images-dir',os.path.relpath(parent,path)]
        invoke(command,exact,expect_failure)
        if expect_failure:
            result['steps'].append({'rejected_invalid_parent':name});return path
        info={'name':name,'pages_bytes':sum(p.stat().st_size for p in path.glob('pages-*.img'))}
        info['pagemap']={p.name:summarize(p) for p in path.glob('pagemap-*.img')}
        if a.lazy and exact and parent:
            assert sum(v['parent_lazy_pages'] for v in info['pagemap'].values()) > 0, 'parent pages lost lazy eligibility'
        info['exact_stats']=[line.strip() for line in (path/'dump.log').read_text().splitlines() if 'Exact parent pages:' in line]
        info['pid_reuse']=[line.strip() for line in (path/'dump.log').read_text().splitlines() if 'Pid reuse' in line]
        result['steps'].append({'dump':info});return path
    def restore(path,expected):
        (a.out/'command').unlink(missing_ok=True)
        pidfile=a.out/f'restore-{seq}.pid'
        lazy_flags=[]
        if a.lazy:
            rfd,wfd=os.pipe()
            daemon=subprocess.Popen([str(a.stock_criu),'lazy-pages','-D',str(path),
                '--status-fd',str(wfd),'-o',f'lazy-{seq}.log','-v4'],pass_fds=(wfd,))
            daemons.append(daemon);os.close(wfd)
            import select
            try:
                if not select.select([rfd],[],[],10)[0] or os.read(rfd,1)!=b'\0':
                    raise RuntimeError('lazy daemon did not become ready')
            finally:os.close(rfd)
            lazy_flags=['--lazy-pages']
        invoke([a.stock_criu,'restore','-d','-D',path,*flags,*lazy_flags,'--pidfile',pidfile,'--leave-stopped','-o',f'restore-{seq}.log','-v4'])
        pid=own(int(pidfile.read_text()));os.kill(pid,signal.SIGCONT)
        until=time.monotonic()+5
        while (Path('/proc')/str(pid)/'wchan').read_text().strip() != '__do_sys_pause':
            if time.monotonic()>until:raise TimeoutError('restored worker did not reach pause')
            time.sleep(.005)
        os.kill(pid,signal.SIGUSR1)
        r=cmd('r');assert r['hash']==expected['hash'],(r,expected);assert r['samples']==expected['samples'];r['independent_sha256']=memory_digest(r);assert r['independent_sha256']==expected['independent_sha256'];result['steps'].append({'cold_restore_verified':path.name,'response':r});return pid
    log=(a.out/'worker.log').open('w');p=None
    try:
        p=subprocess.Popen([str(a.worker),str(a.out)],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True);active=own(p.pid);initial=wait(a.out/'ready-0.json')
        A=fork();dirA=dump(A,'A');kill(A['host_pid'])
        cmd('w',0,11);cmd('w',3,33);B=fork()
        (Path('/proc')/str(B['host_pid'])/'clear_refs').write_text('4')
        result['steps'].append({'adversarial_current_softdirty_clear':B['host_pid']})
        stockB=dump(B,'B-stock',dirA,False);dirB=dump(B,'B-exact',dirA)
        for damage in ('truncated-pages','missing-pagemap','bad-pagemap-magic'):
            bad=a.out/('parent-'+damage);shutil.copytree(dirA,bad)
            if damage=='truncated-pages':
                pages=next(bad.glob('pages-*.img'))
                with pages.open('r+b') as f:f.truncate(pages.stat().st_size-4096)
            elif damage=='missing-pagemap':(bad/'pagemap-1.img').unlink()
            else:
                with (bad/'pagemap-1.img').open('r+b') as f:f.write(b'BAD!')
            dump(B,'reject-'+damage,bad,expect_failure=True)
        kill(B['host_pid'])
        # Parent branch changes after B cannot contaminate the frozen A/B images.
        cmd('w',17,171);kill(active);p.wait(timeout=5)
        active=restore(dirB,B);kill(active)
        active=restore(dirA,A)
        # A divergent branch, including a VMA address reuse and a write-back.
        cmd('w',1,22);cmd('w',0,99);cmd('w',0,90);cmd('z',17,71)
        C=fork();dirC=dump(C,'C-exact',dirB);kill(C['host_pid']);kill(active)
        active=restore(dirC,C)
        cmd('w',3,44);D=fork();dirD=dump(D,'D-exact',dirC);kill(D['host_pid']);kill(active)
        active=restore(dirD,D);kill(active)
        result['passed']=True
    except Exception as e:
        result['error']=repr(e);raise
    finally:
        for pid in list(owned):kill(pid)
        for daemon in daemons:
            if daemon.poll() is None:daemon.terminate()
            try:daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:daemon.kill();daemon.wait(timeout=5)
        log.close();(a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({'passed':result['passed'],'out':str(a.out),'error':result.get('error')}))
if __name__=='__main__':main()
