#!/usr/bin/env python3
"""Check PID-namespace-init blocked SIGCONT/sigwait handshake through stock CRIU."""
import argparse, ctypes, json, os, signal, subprocess, sys, time
from pathlib import Path


def worker(out,fd):
    os.setsid()
    signal.pthread_sigmask(signal.SIG_BLOCK,{signal.SIGCONT})
    os.write(fd,b'ready\n');os.close(fd)
    received=signal.sigwait({signal.SIGCONT})
    (out/'continued.json').write_text(json.dumps({'pid':os.getpid(),'received':received,'blocked':[int(s) for s in signal.pthread_sigmask(signal.SIG_BLOCK,set())]})+'\n')
    while True:signal.pause()


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--criu',type=Path);p.add_argument('--worker-fd',type=int)
    a=p.parse_args();a.out=a.out.resolve()
    if a.worker_fd is not None:return worker(a.out,a.worker_fd)
    a.out.mkdir(parents=True,exist_ok=False)
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(36,1,0,0,0):raise OSError('subreaper')
    record={'passed':False,'steps':[]};owned={}
    def own(pid):owned[pid]=int((Path('/proc')/str(pid)/'stat').read_text().rpartition(')')[2].split()[19]);return pid
    def stop(pid):
        os.kill(pid,signal.SIGSTOP);wp,status=os.waitpid(pid,os.WUNTRACED)
        assert wp==pid and os.WIFSTOPPED(status)
    def kill(pid):
        if pid not in owned:return
        try:
            current=int((Path('/proc')/str(pid)/'stat').read_text().rpartition(')')[2].split()[19]);assert current==owned[pid]
            os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
        except (FileNotFoundError,ProcessLookupError):pass
        owned.pop(pid)
    def invoke(cmd):
        r=subprocess.run(list(map(str,cmd)),capture_output=True,text=True,timeout=45)
        record['steps'].append({'command':list(map(str,cmd)),'rc':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
        if r.returncode:raise RuntimeError('CRIU failed')
    def await_reply():
        end=time.monotonic()+5
        while not (a.out/'continued.json').exists():
            if time.monotonic()>end:raise TimeoutError('sigwait did not accept SIGCONT')
            time.sleep(.005)
        reply=json.loads((a.out/'continued.json').read_text());assert reply['pid']==1 and reply['received']==int(signal.SIGCONT);return reply
    try:
        r,w=os.pipe();os.set_inheritable(w,True)
        # x86_64 clone with a fork-style NULL child stack; exec immediately.
        pid=libc.syscall(56,0x20000000|signal.SIGCHLD,0,0,0,0)
        if pid<0:raise OSError(ctypes.get_errno(),'clone CLONE_NEWPID')
        if pid==0:
            os.close(r);dev=os.open('/dev/null',os.O_RDWR)
            for fd in (0,1,2):os.dup2(dev,fd)
            os.execv(sys.executable,[sys.executable,str(Path(__file__).resolve()),'--out',str(a.out),'--worker-fd',str(w)])
        own(pid);os.close(w)
        with os.fdopen(r,'rb') as pipe:assert pipe.read()==b'ready\n'
        stop(pid);record['steps'].append({'ready_pipe_eof_then_stopped':pid})
        images=a.out/'images';images.mkdir()
        flags=['--shell-job','--tcp-close','--file-locks','--ext-unix-sk']
        invoke([a.criu,'dump','-t',pid,'-D',images,'--leave-stopped',*flags,'-o','dump.log','-v4'])
        # Same blocked wait works on the live original, then the image independently.
        os.kill(pid,signal.SIGCONT);record['warm_reply']=await_reply();kill(pid);(a.out/'continued.json').unlink()
        pidfile=a.out/'restore.pid'
        invoke([a.criu,'restore','-d','-D',images,'--leave-stopped','--pidfile',pidfile,*flags,'-o','restore.log','-v4'])
        pid=own(int(pidfile.read_text()));os.kill(pid,signal.SIGCONT)
        record['cold_reply']=await_reply();kill(pid);record['passed']=True
    except Exception as e:
        record['error']=repr(e);raise
    finally:
        for pid in list(owned):kill(pid)
        (a.out/'result.json').write_text(json.dumps(record,indent=2)+'\n');print(json.dumps(record))

if __name__=='__main__':main()
