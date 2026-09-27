"""Explicit file transport for the owned nested E2B reconstruction.

Transfers happen outside the retained Go checkpoint/restore timers. No writable
host share, remote shell retry, snapshot retry or missing-result success exists.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import stat
import subprocess
import uuid

REPO = Path('/home/atc-ae/delta-box-ae')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def local_file(path, root, *, existing):
    path, root = Path(path), Path(root)
    if (not path.is_absolute() or '..' in path.parts or root.resolve() != root
            or not path.is_relative_to(root) or path == root
            or not path.resolve().is_relative_to(root)):
        raise ValueError('Transfer path escapes this input output directory')
    for p in (path, *path.parents):
        if p.is_symlink():
            raise ValueError('Transfer path contains a symlink')
    if existing:
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise ValueError('Transfer source must be an independent regular file')
    elif path.exists():
        raise ValueError('Refusing to overwrite a downloaded result')
    return path


class Transport:
    def __init__(self, path):
        from ae.scripts.e2b_l1_context import trusted, proc_identity, ancestors, owned_listener
        self.proc_identity, self.ancestors, self.owned_listener = proc_identity, ancestors, owned_listener
        path = trusted(path)
        self.manifest = json.loads(path.read_text())
        d = self.manifest
        if d.get('kind') != 'e2b-paper-owned-transport-v1':
            raise ValueError('Not an owned paper reconstruction transport')
        self.output = Path(d['host_output_root'])
        if (self.output == REPO / 'ae/results/selected'
                or not self.output.is_relative_to(REPO / 'ae/results/selected')
                or self.output.resolve() != self.output):
            raise ValueError('Transport output must be a selected run descendant')
        self.lifecycle = trusted(d['lifecycle'])
        self.identity = d['qemu_identity']
        self.port = d['ssh_port']
        if type(self.port) is not int or not 55000 <= self.port <= 59999:
            raise ValueError('Invalid owned SSH endpoint')
        self.key = trusted(d['ssh_identity'], root=False)
        self.known_hosts = trusted(d['known_hosts'])
        if self.key.stat().st_mode & 0o077:
            raise ValueError('SSH identity permissions are not private')
        root = PurePosixPath(d['guest_root'])
        if (root.parent != PurePosixPath('/var/tmp') or not root.name.startswith('e2b-paper-')
                or len(root.name) != len('e2b-paper-') + 32):
            raise ValueError('Invalid owned guest root')
        uuid.UUID(hex=root.name[len('e2b-paper-'):])
        self.guest_root = str(root)
        self.storage = self.guest_root + '/storage'
        self.runtime = '/opt/e2b-paper/runtime'
        self.guard()

    def guard(self):
        ident = self.proc_identity(self.identity['pid'])
        life = json.loads(self.lifecycle.read_text())
        if (ident != self.identity or life.get('ownership') != ident
                or life.get('status') not in ('ready', 'running')
                or ident['ppid'] not in self.ancestors()
                or ident['cgroup'] != self.proc_identity(os.getpid())['cgroup']
                or not self.owned_listener(ident['pid'], self.port)):
            raise RuntimeError('The live L1 is not owned by this measurement ancestry')

    def ssh_options(self):
        return ['-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(self.known_hosts),
                '-o', 'ConnectTimeout=5', '-o', 'ConnectionAttempts=1',
                '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
                '-o', 'ControlMaster=no', '-i', str(self.key)]

    def remote(self, command, *, timeout=60, check=True):
        self.guard()
        return subprocess.run(['/usr/bin/ssh', *self.ssh_options(), '-p', str(self.port),
            'ubuntu@127.0.0.1', command], capture_output=True, text=True, timeout=timeout, check=check)

    def copy(self, source, destination, *, upload, timeout=900):
        self.guard()
        local_file(source if upload else destination, self.output, existing=upload)
        remote = destination if upload else source
        p = PurePosixPath(remote)
        if not p.is_relative_to(PurePosixPath(self.guest_root)) or '..' in p.parts:
            raise ValueError('Transfer escapes owned guest work')
        peer = 'ubuntu@127.0.0.1:' + shlex.quote(str(p))
        cmd = ['/usr/bin/scp', '-q', *self.ssh_options(), '-P', str(self.port)]
        cmd += [str(source), peer] if upload else [peer, str(destination)]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)

    def sidecars_ready(self, worker_port, index_port):
        import re
        expected = (self.manifest.get('worker_mock_port'), self.manifest.get('index_port'))
        if (worker_port, index_port) != expected or any(type(p) is not int or not 1024 <= p <= 65535 for p in expected):
            raise ValueError('Sidecar ports differ from the owned input contract')
        evidence = []
        for port, route in zip(expected, ('/admin/healthz','/healthz')):
            command = 'curl --fail --silent --show-error --connect-timeout 3 --max-time 10 http://10.0.2.2:' + str(port) + route
            cp = self.remote(command, timeout=15)
            obj = json.loads(cp.stdout)
            if obj.get('ok') is not True:
                raise RuntimeError('Sidecar health response is not successful')
            evidence.append({'port': port, 'address': '10.0.2.2', 'response': obj})
        path = self.output / 'l1-sidecar-reachability.json'
        with path.open('x') as stream:
            json.dump({'scope': 'L1 to controller through QEMU user-network gateway; L2 checked separately', 'sidecars': evidence}, stream, indent=2)
        return evidence

    def capacity_guard(self, from_build, pending_upload_bytes, evidence, *, download_count=0):
        """Read-only admission outside all Go timers; never dispatch on failure.

        Reserve two full memory+rootfs copies (pause working output and saved
        snapshot), all retained input data files as a parent-read-cache upper
        bound, one rootfs of bytes per declared response download, worst-case
        V3 headers and metadata, uploads not yet copied,
        and 10GiB free inside L1. Host admission retains the already reviewed
        full remaining qcow growth plus 10GiB rule.
        """
        from ae.scripts.e2b_l1_context import capacity, trusted
        proof = {'status': 'checking', 'from_build': from_build,
                 'pending_upload_bytes': pending_upload_bytes, 'download_count': download_count,
                 'scope': 'read-only before physical resume; outside C/R timers'}
        try:
            self.guard()
            if str(uuid.UUID(from_build)) != from_build:
                raise ValueError('Noncanonical capacity build UUID')
            if type(pending_upload_bytes) is not int or pending_upload_bytes < 0:
                raise ValueError('Invalid pending upload capacity')
            if type(download_count) is not int or download_count < 0:
                raise ValueError('Invalid response download count')
            life = json.loads(trusted(self.lifecycle).read_text())
            asset = life['asset_manifest']
            raw = trusted(asset['path']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != asset['sha256']:
                raise ValueError('Owned L1 asset manifest changed')
            value = json.loads(raw)
            disk_gib = value.get('disk_size_gib')
            if (type(disk_gib) is not int or disk_gib not in (80, 128, 156)
                    or life.get('reconstruction', {}).get('disk_size_gib') != disk_gib):
                raise ValueError('Owned L1 disk capacity identity differs')
            overlay = trusted(self.lifecycle.parent/'l1.qcow2')
            allocated = overlay.stat().st_blocks*512
            fs = os.statvfs(overlay.parent)
            proof['host_observed'] = dict(available_bytes=fs.f_bavail*fs.f_frsize,
                required_bytes=max(0,disk_gib*1024**3-allocated)+10*1024**3,
                allocated_overlay_bytes=allocated,disk_size_gib=disk_gib)
            proof['host'] = capacity(overlay.parent,disk_gib,allocated=allocated)
            fresh = json.loads(trusted(self.manifest['fresh_base_manifest']).read_text())
            if (fresh.get('guest_storage') != self.storage or fresh.get('fresh_base_build_id')
                    != self.manifest.get('fresh_base_build_id')):
                raise ValueError('Fresh capacity proof differs from owned storage/base')
            headers = fresh['snapshot_closure']['builds'][fresh['fresh_base_build_id']]['headers']
            expected = {h['file']: h['logical_bytes'] for h in headers}
            if (set(expected) != {'memfile.header','rootfs.ext4.header'}
                    or len(headers) != 2 or expected['memfile.header'] != 2048*1024**2
                    or any(type(v) is not int or v <= 0 or v % 4096 or v > disk_gib*1024**3 for v in expected.values())):
                raise ValueError('Fresh capacity header dimensions are invalid')
            source = '''import json,os,pathlib,stat,struct,uuid
storage=STORAGE
build=BUILD
expected=EXPECTED
pending=PENDING
download_count=DOWNLOAD_COUNT
root=pathlib.Path(storage)
assert root.resolve()==root and root.is_dir(), 'storage is not canonical'
dev=root.stat().st_dev
proof={'storage':storage,'from_build':build,'logical_bytes':{},'headers':[]}
for name in ('memfile.header','rootfs.ext4.header'):
 p=root/'templates'/build/name
 assert p.resolve()==p and not any(x.is_symlink() for x in (p,*p.parents)), 'header path is a symlink'
 fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW)
 try:
  st=os.fstat(fd)
  assert stat.S_ISREG(st.st_mode) and st.st_nlink==1 and st.st_dev==dev, 'header must be an independent file on storage filesystem'
  data=os.read(fd,64)
  assert len(data)==64, 'truncated V3 header'
  version,block,size,generation,ident,base=struct.unpack('<QQQQ16s16s',data)
  assert version==3 and block==4096 and str(uuid.UUID(bytes=ident))==build, 'V3 header identity/version differs'
  assert size==expected[name] and size>0 and size%block==0, 'logical header dimensions changed'
  again=os.fstat(fd)
  assert (st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)==(again.st_ino,again.st_size,again.st_mtime_ns,again.st_ctime_ns), 'header changed during capacity read'
  proof['logical_bytes'][name]=size
  proof['headers'].append({'path':str(p),'bytes':st.st_size,'inode':st.st_ino})
 finally:os.close(fd)
templates=root/'templates'
assert templates.resolve()==templates and templates.is_dir(), 'templates is not canonical'
files=('metadata.json','snapfile','memfile','memfile.header','rootfs.ext4','rootfs.ext4.header')
children=sorted(templates.iterdir())
cache_rows=[]
for folder in children:
 assert folder.resolve()==folder and not folder.is_symlink() and folder.is_dir(), 'cache build directory is not canonical'
 assert str(uuid.UUID(folder.name))==folder.name, 'cache build UUID is not canonical'
 assert {p.name for p in folder.iterdir()}==set(files), 'cache build file set differs'
 for name in ('memfile','rootfs.ext4'):
  p=folder/name;st=p.lstat()
  assert stat.S_ISREG(st.st_mode) and st.st_nlink==1 and st.st_dev==dev, 'cache data must be an independent file on storage filesystem'
  cache_rows.append(dict(path=str(p),bytes=st.st_size,inode=st.st_ino,mtime_ns=st.st_mtime_ns,ctime_ns=st.st_ctime_ns))
for row in cache_rows:
 st=pathlib.Path(row['path']).lstat()
 assert (st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns)==(row['inode'],row['bytes'],row['mtime_ns'],row['ctime_ns']), 'cache data changed during capacity read'
assert sorted(templates.iterdir())==children, 'cache build set changed during capacity read'
cache_bytes=sum(row['bytes'] for row in cache_rows)
proof['parent_cache_data_files']=cache_rows
proof['parent_cache_bound_bytes']=cache_bytes
fs=os.statvfs(root)
logical=sum(proof['logical_bytes'].values())
metadata=max(256*1024**2,2*sum((v//4096)*40+64 for v in proof['logical_bytes'].values())+32*1024**2)
download_bytes=download_count*expected['rootfs.ext4.header']
required=2*logical+cache_bytes+download_bytes+metadata+pending+10*1024**3
available=fs.f_bavail*fs.f_frsize
proof.update(status='verified' if available>=required else 'insufficient',available_bytes=available,
 required_bytes=required,pending_upload_bytes=pending,download_count=download_count,download_bound_bytes=download_bytes,full_copy_count=2,metadata_reserve_bytes=metadata,
 free_reserve_bytes=10*1024**3,formula='2*(memory+rootfs)+all-input-data-files+download-count*rootfs+max(256MiB,2*V3-worst-headers+32MiB)+pending-upload+10GiB')
print(json.dumps(proof,sort_keys=True))
'''.replace('STORAGE',repr(self.storage)).replace('BUILD',repr(from_build)).replace('EXPECTED',repr(expected)).replace('PENDING',str(pending_upload_bytes)).replace('DOWNLOAD_COUNT',str(download_count))
            cp = self.remote('sudo -n python3 -B -c '+shlex.quote(source),timeout=30)
            guest = json.loads(cp.stdout)
            proof['guest'] = guest
            logical = sum(expected.values())
            metadata = max(256*1024**2,2*sum((v//4096)*40+64 for v in expected.values())+32*1024**2)
            cache_rows = guest.get('parent_cache_data_files')
            if not isinstance(cache_rows,list) or not cache_rows:
                raise ValueError('Invalid parent cache capacity proof')
            data_paths=set()
            for row in cache_rows:
                p=PurePosixPath(row.get('path',''))
                if (p.parent.parent != PurePosixPath(self.storage)/'templates'
                        or p.name not in ('memfile','rootfs.ext4') or str(uuid.UUID(p.parent.name)) != p.parent.name
                        or p in data_paths or type(row.get('bytes')) is not int or row['bytes']<0):
                    raise ValueError('Invalid parent cache data-file bound')
                data_paths.add(p)
            cache_bytes=sum(row['bytes'] for row in cache_rows)
            if guest.get('parent_cache_bound_bytes') != cache_bytes:
                raise ValueError('Invalid parent cache total')
            required = 2*logical+cache_bytes+download_count*expected['rootfs.ext4.header']+metadata+pending_upload_bytes+10*1024**3
            if (guest.get('storage') != self.storage or guest.get('from_build') != from_build
                    or guest.get('logical_bytes') != expected
                    or guest.get('download_count') != download_count
                    or guest.get('download_bound_bytes') != download_count*expected['rootfs.ext4.header']
                    or type(guest.get('available_bytes')) is not int or guest['available_bytes'] < required
                    or guest.get('required_bytes') != required or guest.get('status') != 'verified'):
                raise ValueError('Insufficient or invalid owned L1 step capacity')
            proof['status'] = 'verified'
            return proof
        except BaseException as error:
            proof.update(status='failed',error=type(error).__name__+': '+str(error))
            raise
        finally:
            with Path(evidence).open('x') as stream:
                json.dump(proof,stream,indent=2);stream.write('\n')
            Path(evidence).chmod(0o600)

    def step(self, driver, *, from_build, to_build, storage, command, timings_path,
             uploads, downloads, timeout=1200.0):
        if storage != self.storage:
            raise ValueError('Step storage differs from this owned input')
        for build in (from_build, to_build):
            if str(uuid.UUID(build)) != build:
                raise ValueError('Noncanonical build UUID')
        if not 0 < timeout <= 2400:
            raise ValueError('Unbounded step timeout')
        timing = local_file(timings_path, self.output, existing=False)
        for p, _ in uploads:
            local_file(p, self.output, existing=True)
        for _, p in downloads:
            local_file(p, self.output, existing=False)
        transfer = self.guest_root + '/transfers/' + uuid.uuid4().hex
        evidence = timing.parent / (timing.stem + '.transport')
        evidence.mkdir(mode=0o700)
        self.capacity_guard(from_build,sum(Path(p).stat().st_size for p,_ in uploads),
                            evidence/'capacity-before-upload.json', download_count=len(downloads))
        self.remote('mkdir -m 700 -p ' + shlex.quote(transfer))
        uploaded, download_map = [], []
        records = {'kind': 'ssh-files-outside-inner-timers', 'uploads': [], 'downloads': [],
                   'guest_transfer': transfer, 'from_build': from_build, 'to_build': to_build}
        for i, (p, target) in enumerate(uploads):
            p = Path(p)
            before = (p.stat().st_size, digest(p))
            remote = transfer + '/upload-' + str(i)
            self.copy(p, remote, upload=True)
            after = (p.stat().st_size, digest(p))
            if before != after:
                raise ValueError('Upload source changed during transfer')
            observed = self.remote('sha256sum ' + shlex.quote(remote)).stdout.split()[0]
            if observed != before[1]:
                raise ValueError('Guest upload SHA mismatch')
            records['uploads'].append({'path': str(p), 'guest_path': remote, 'bytes': before[0], 'sha256': before[1]})
            uploaded.append((Path(remote), target))
        for i, (source, p) in enumerate(downloads):
            download_map.append((source, Path(transfer + '/download-' + str(i))))
        try:
            self.capacity_guard(from_build,0,evidence/'capacity-before-resume.json',download_count=len(downloads))
        except BaseException as error:
            records.update(ok=False,physical_resume_dispatched=False,capacity_error=type(error).__name__+': '+str(error))
            (evidence/'receipt.json').write_text(json.dumps(records,indent=2)+'\n')
            raise
        remote_timing = Path(transfer + '/timing.json')
        remote_command = driver.e2b_resume_build_cmd(from_build=from_build, to_build=to_build,
            storage=storage, command=command, finalbench_json=remote_timing,
            uploads=uploaded, downloads=download_map)
        (evidence/'command.txt').write_text(remote_command + '\n')
        try:
            cp = self.remote(remote_command, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as error:
            for name,value in (('stdout.log',error.stdout),('stderr.log',error.stderr)):
                value=value or ''
                (evidence/name).write_text(value.decode(errors='replace') if isinstance(value,bytes) else value)
            records.update(timeout=timeout,partial_output=True,ok=False)
            (evidence/'receipt.json').write_text(json.dumps(records,indent=2)+'\n')
            raise
        (evidence/'stdout.log').write_text(cp.stdout)
        (evidence/'stderr.log').write_text(cp.stderr)
        records['returncode'] = cp.returncode
        errors = []
        for remote, local in [(remote_timing, timing), *[(r, Path(p)) for (_, r), (_, p) in zip(download_map, downloads)]]:
            local.parent.mkdir(parents=True, exist_ok=True)
            part = local.with_name(local.name + '.partial-' + uuid.uuid4().hex)
            try:
                expected = self.remote('sha256sum ' + shlex.quote(str(remote))).stdout.split()[0]
                self.copy(str(remote), part, upload=False)
                if digest(part) != expected:
                    raise ValueError('Downloaded file SHA mismatch')
                # link() is atomic no-replace; the private part is retained on failure.
                os.link(part, local)
                part.unlink()
                records['downloads'].append({'guest_path': str(remote), 'path': str(local), 'bytes': local.stat().st_size, 'sha256': expected})
            except Exception as error:
                errors.append(type(error).__name__ + ': ' + str(error))
        records['transfer_errors'] = errors
        (evidence/'receipt.json').write_text(json.dumps(records, indent=2) + '\n')
        data = json.loads(timing.read_text()) if timing.exists() else None
        out = dict(data or {})
        out.update(ok=cp.returncode == 0 and isinstance(data, dict) and data.get('ok') is True and not errors,
                   host_rc=cp.returncode, timing_present=isinstance(data, dict),
                   stdout_tail=cp.stdout[-2000:], stderr_tail=cp.stderr[-4000:],
                   transport_receipt=str(evidence/'receipt.json'))
        if not out['ok']:
            out.setdefault('error', 'Nested process or independently hashed result transfer failed')
        return out


def install(driver):
    """Install only in the explicit paper driver; ordinary profiles stay intact."""
    path = os.environ.get('AE_E2B_PAPER_TRANSPORT')
    if not path:
        raise ValueError('Explicit owned paper transport is required')
    transport = Transport(Path(path))
    driver.E2B = Path(transport.runtime)
    os.environ['E2B_RESUME_BINARY'] = transport.runtime + '/bin/resume-build'
    os.environ['E2B_SANDBOX_DIR'] = transport.guest_root + '/sandboxes'
    driver.SIDE_CAR_IP_FOR_SANDBOX = '10.0.2.2'
    old_prefix = driver.e2b_env_prefix
    env = {'HOST_ENVD_PATH': transport.runtime + '/envd/envd',
           'HOST_BUSYBOX_DIR': transport.runtime + '/busybox', 'BUSYBOX_VERSION': '1.36.1',
           'FIRECRACKER_VERSIONS_DIR': transport.runtime + '/fc-versions',
           'HOST_KERNELS_DIR': transport.runtime + '/kernels'}
    driver.e2b_env_prefix = lambda: old_prefix() + 'unset LAUNCH_DARKLY_API_KEY; ' + ''.join('export ' + k + '=' + shlex.quote(v) + '; ' for k, v in env.items())
    driver.e2b_step = lambda **kwargs: transport.step(driver, **kwargs)
    def forbidden_create(**kwargs):
        raise ValueError('Paper suite must build and verify one fresh base for this input before timing')
    driver.create_base_build = forbidden_create
    return transport
