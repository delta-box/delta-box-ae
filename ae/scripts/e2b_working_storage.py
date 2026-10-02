"""Candidate: one hosted E2B fanout's writable files on owned noswap RAM.

Called only inside the protected service-placement transaction, with all three
registered daemons stopped and /e2b empty. Original directories are never
copied wholesale or recursively removed. Known V3 base dependencies alone are
copied, because the registered Local backend opens even reads O_RDWR.
"""
import hashlib
import json
import datetime
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import uuid

GIB = 1024**3
HOSTED = Path('/mnt/disk2/dyp/ae-hosted-20260922/e2b')
SANDBOX = Path('/home/dyp/infra/.local-finalbench-62-e2b-lw12/sandbox')
BASE = '62e2b12b-0000-4000-8000-000000000062'
TEMPLATE = 'ae-e2b-base-20260922'
# Reserve 21 bytes for each E2B identifier and one NUL in Linux sun_path[108].
UFFD_SOCKET_NAME = 'uffd-' + 'x'*21 + '-' + 'x'*21 + '.sock'
PATH_KEYS = {
    'LOCAL_BUILD_CACHE_STORAGE_BASE_PATH':HOSTED/'build-cache',
    'LOCAL_TEMPLATE_STORAGE_BASE_PATH':HOSTED/'storage/templates',
    'SANDBOX_CACHE_DIR':HOSTED/'sandbox-cache',
    'SNAPSHOT_CACHE_DIR':HOSTED/'snapshot-cache',
    'TEMPLATE_CACHE_DIR':HOSTED/'template-cache',
    'ORCHESTRATOR_BASE_PATH':HOSTED/'orchestrator',
    'SANDBOX_DIR':SANDBOX,
}


def require(value, message):
    if not value:
        raise RuntimeError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(4*1024**2), b''):
            h.update(block)
    return h.hexdigest()


def identity(path):
    path = Path(path)
    info = path.lstat()
    require(not stat.S_ISLNK(info.st_mode), 'Storage path is a symlink')
    return dict(device=info.st_dev,inode=info.st_ino,bytes=info.st_size,
                blocks_bytes=info.st_blocks*512,mtime_ns=info.st_mtime_ns)


def same_directory(path, original):
    actual=identity(path)
    return all(actual[key]==original[key] for key in ('device','inode'))


def header_v3(raw):
    require(len(raw)>=64 and (len(raw)-64)%40 == 0, 'Truncated V3 dependency header')
    version, block, size, generation, build, base = struct.unpack('<QQQQ16s16s',raw[:64])
    require(version & 65535 in (1,2,3), 'Only evidenced V3 template headers are supported')
    refs={uuid.UUID(bytes=build),uuid.UUID(bytes=base)}
    covered=0
    for pos in range(64,len(raw),40):
        offset,length,ref,storage_offset=struct.unpack('<QQ16sQ',raw[pos:pos+40])
        require(length>0 and offset+length<=size, 'Invalid V3 dependency mapping')
        value=uuid.UUID(bytes=ref)
        if value.int:
            refs.add(value);covered+=length
    refs.discard(uuid.UUID(int=0))
    return dict(version=version,block_size=block,logical_size=size,generation=generation,
                build_id=str(uuid.UUID(bytes=build)),base_build_id=str(uuid.UUID(bytes=base)),
                referenced_build_ids=sorted(map(str,refs)),mapped_nonzero_bytes=covered)


def dependency_closure(templates, base=BASE):
    templates=Path(templates)
    pending, seen, rows=[base],set(),[]
    while pending:
        name=pending.pop()
        if name in seen:
            continue
        require(str(uuid.UUID(name))==name, 'Unsafe template build identifier')
        seen.add(name)
        require(len(seen)<=32, 'Unbounded template dependency closure')
        folder=templates/name
        require(folder.is_dir() and not folder.is_symlink(), 'Template dependency is missing/unsafe')
        headers=[]
        for kind in ('memfile','rootfs.ext4'):
            path=folder/(kind+'.header')
            require(path.is_file() and not path.is_symlink(), 'Unsafe dependency header')
            require(path.stat().st_size<=16*1024**2,'Unbounded dependency header')
            raw=path.read_bytes()
            row=header_v3(raw)
            require(row['build_id']==name, 'Template directory/header build identity differs')
            headers.append(dict(row,kind=kind,sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw)))
            pending.extend(set(row['referenced_build_ids'])-seen)
        files=[]
        for path in sorted(folder.iterdir()):
            require(path.is_file() and not path.is_symlink(), 'Unknown directory/symlink in required template build')
            files.append(dict(path=str(path),**identity(path)))
        require({'metadata.json','snapfile','memfile','memfile.header','rootfs.ext4','rootfs.ext4.header'}<=set(Path(f['path']).name for f in files),
                'Required template base files are missing')
        rows.append(dict(build_id=name,headers=headers,files=files))
    return dict(base_build=base,builds=rows,build_ids=sorted(seen),
                allocated_bytes=sum(f['blocks_bytes'] for r in rows for f in r['files']))


def paths_from_process(pid):
    # All unrelated environment values, including credentials, stay in memory.
    env=dict(entry.split(b'=',1) for entry in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0') if b'=' in entry)
    observed={k:Path(env.get(k.encode(),b'').decode()) for k in PATH_KEYS}
    require(observed==PATH_KEYS, 'Registered E2B writable-path identity differs')
    require(env.get(b'STORAGE_PROVIDER',b'').lower()==b'local', 'E2B storage is not the registered local provider')
    # This registered deployment leaves SharedChunkCacheDir empty and derives
    # DefaultCacheDir/TemplatesDir from ORCHESTRATOR_BASE_PATH. Fail if changed.
    require(not env.get(b'SHARED_CHUNK_CACHE_PATH',b''), 'Additional shared chunk cache must be reviewed')
    for key,default in (('DEFAULT_CACHE_DIR',HOSTED/'orchestrator/build'),('TEMPLATES_DIR',HOSTED/'orchestrator/build-templates')):
        require(not env.get(key.encode(),b'') or Path(env[key.encode()].decode())==default,
                'Unreviewed E2B additional writable path')
    return observed


def process_tmpdir(pid):
    # Read only the one path needed for filesystem verification; never serialize
    # the complete process environment or pass it through a command line.
    for entry in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0'):
        if entry.startswith(b'TMPDIR='):
            return Path(entry.split(b'=',1)[1].decode())
    return None


def visible_mount(pid, path):
    raw=subprocess.check_output(['nsenter','-t',str(pid),'-m','findmnt','-J','-T',str(path),
        '-o','TARGET,SOURCE,FSTYPE,OPTIONS,MAJ:MIN,ID'],text=True)
    device=Path(f'/proc/{pid}/root{path}').stat().st_dev
    observed=f'{os.major(device)}:{os.minor(device)}'
    visible=[m for m in json.loads(raw)['filesystems'] if m['maj:min']==observed]
    require(len(visible)==1,'Cannot identify actual E2B working-data mount')
    return dict(path=str(path),device=observed,mount=visible[0])


class WorkingStorage:
    def __init__(self, config, out, node, cpus, source_sha256, *, root, units, vm_root):
        require(os.geteuid()==0 and type(node) is int and node in (0,1,2,3),
                'E2B RAM storage requires root and a supported NUMA0/1/2/3 lane')
        require(re.fullmatch(r'[0-9a-f]{64}',str(source_sha256)), 'RAM storage source identity missing')
        require(config.get('e2b',{}).get('template')==TEMPLATE, 'Unreviewed E2B fanout template')
        self.out,self.root,self.node,self.cpus=Path(out),Path(root),node,cpus
        self.units,self.vm_root=tuple(units),Path(vm_root)
        self.source_sha256=source_sha256
        self.size=config.get('e2b',{}).get('fanout_memory_size_gib',24)
        require(type(self.size) is int and self.size>0, 'Invalid E2B fanout RAM ceiling')
        self.mounts=[];self.original={};self.closure=None;self.paths=None;self.directories={}
        self.ram=self.root/'ae/work'/('et-'+uuid.uuid4().hex[:10])
        self.alias=self.out/'original-templates'
        self.out_identity=None;self.vm_identity=None;self.original_tmpdir=None
        require(self.out.is_absolute() and self.root.is_absolute() and '..' not in self.out.parts
                and self.out.is_relative_to(self.root), 'Unsafe E2B storage receipt location')
        require(len(str(self.ram/'tmp'/UFFD_SOCKET_NAME).encode('utf-8'))<=107,
                'E2B RAM TMPDIR is too long for the UFFD Unix socket (107-byte limit)')

    def open_receipt_directory(self):
        if self.out_identity is None:
            require(self.out.resolve(strict=False)==self.out and not self.out.exists(),
                    'Existing or symlinked E2B storage receipt location')
            self.out.mkdir(parents=True,mode=0o700)
            self.out.chmod(0o700)
            self.out_identity=identity(self.out)
        require(same_directory(self.out,self.out_identity), 'E2B storage receipt directory identity changed')

    def save(self, name, value):
        require(re.fullmatch(r'[a-z-]+\.json',name),'Unsafe storage receipt filename')
        self.open_receipt_directory()
        payload=dict(value,schema='deltabox.e2b-working-storage.v1',
            source_sha256=self.source_sha256,job=str(self.out.parent.parent),node=self.node,cpus=self.cpus,
            captured_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
        fd=os.open(self.out/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'w') as stream:
            stream.write(json.dumps(payload,sort_keys=True,indent=2)+'\n')

    def assert_stopped(self):
        require(os.readlink('/proc/self/ns/mnt')==os.readlink('/proc/1/ns/mnt'),
                'Shared E2B storage requires the host mount namespace')
        require(self.vm_identity is not None and same_directory(self.vm_root,self.vm_identity),
                'E2B VM root identity changed; no shared storage writes allowed')
        for unit in self.units:
            raw=subprocess.check_output(['systemctl','show',unit,'-p','MainPID','-p','ActiveState'],text=True)
            state=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
            require(state.get('MainPID')=='0' and state.get('ActiveState') in ('inactive','failed'),
                    'E2B daemon is not stopped; no shared storage writes allowed')
            group=Path('/sys/fs/cgroup/system.slice')/unit
            if group.exists():
                require('populated 0' in (group/'cgroup.events').read_text()
                        and not any(p.read_text().strip() for p in group.rglob('cgroup.procs')),
                        'Stopped E2B daemon cgroup remains populated')
        require('populated 0' in (self.vm_root/'cgroup.events').read_text()
                and not any(p.read_text().strip() for p in self.vm_root.rglob('cgroup.procs')),
                'E2B VM root remains populated; storage retained')

    def admit(self, before):
        require(self.ram.parent.is_dir() and self.ram.parent.resolve()==self.ram.parent,
                'E2B RAM workspace parent is missing or symlinked')
        require(os.readlink('/proc/self/ns/mnt')==os.readlink('/proc/1/ns/mnt'),
                'Shared E2B storage requires the host mount namespace')
        daemon=before['units']['ae-e2b-orchestrator.service']['process']
        self.paths=paths_from_process(daemon['pid'])
        self.original_tmpdir=process_tmpdir(daemon['pid'])
        self.original_mounts={key:visible_mount(daemon['pid'],path) for key,path in self.paths.items()}
        self.vm_identity=identity(self.vm_root)
        require(self.vm_identity['inode']==before['vm_root']['inode'], 'E2B VM root changed before RAM admission')
        for path in self.paths.values():
            require(path.is_dir() and not path.is_symlink(), 'E2B writable target is missing/unsafe')
            self.original[str(path)]=identity(path)
        self.closure=dependency_closure(self.paths['LOCAL_TEMPLATE_STORAGE_BASE_PATH'])
        mem=next(h['logical_size'] for r in self.closure['builds'] if r['build_id']==BASE for h in r['headers'] if h['kind']=='memfile')
        self.next_snapshot_bytes=mem
        require(self.size*GIB>=self.closure['allocated_bytes']+mem+2*GIB,
                'E2B RAM ceiling cannot stage required dependencies and one full next memory snapshot plus reserve')
        self.save('before.json',dict(source_sha256=self.source_sha256,node=self.node,cpus=self.cpus,
            original_paths=self.original,dependency_closure=self.closure,
            original_mounts=self.original_mounts,
            original_tmpdir=str(self.original_tmpdir) if self.original_tmpdir is not None else None,
            vm_root_identity=self.vm_identity,
            ephemeral_paths=[str(self.ram),str(self.alias)],
            volume_ceiling_bytes=self.size*GIB,next_snapshot_geometry_bytes=mem,
            capacity_scope='Known startup/next-allocation minimum only; child logical memory is not resident usage and later OOM/ENOSPC is not excluded'))

    def prepare_stopped(self):
        self.assert_stopped()
        require(self.paths is not None, 'RAM storage was not admitted')
        for text,original in self.original.items():
            require(same_directory(Path(text),original),'Original E2B directory changed before RAM setup')
        from ae.scripts.run_memory_job import mount_private,bind_private,mount_info
        from ae.vendor.finalbench.fc_diff_dm.fc_capacity import check_capacity
        self.open_receipt_directory()
        require(self.ram.parent.is_dir() and self.ram.parent.resolve()==self.ram.parent,
                'E2B RAM workspace parent changed before setup')
        for directory in (self.ram,self.alias):
            directory.mkdir()
            self.directories[str(directory)]=identity(directory)
        bind_private(self.paths['LOCAL_TEMPLATE_STORAGE_BASE_PATH'],self.alias,self.mounts)
        subprocess.run(['mount','-o','remount,bind,ro',str(self.alias)],check=True)
        require('ro' in mount_info(self.alias)['options'].split(','), 'Original template alias is not read-only')
        mount_private(['mount','-t','tmpfs','-o',f'size={self.size}G,mode=0700,mpol=bind:{self.node},noswap',
                       'tmpfs',str(self.ram)],self.ram,self.mounts)
        info=mount_info(self.ram)
        require(info['fstype']=='tmpfs' and 'noswap' in info['options'].split(',')
                and f'mpol=bind:{self.node}' in info['options'].split(','),'Actual E2B volume is not node-bound noswap RAM')
        check_capacity(self.ram,'e2b-before-staging',self.closure['allocated_bytes']+self.next_snapshot_bytes,
                       self.out/'capacity.jsonl',node=self.node)
        for key,target in self.paths.items():
            staged=self.ram/key.lower();staged.mkdir()
            if key=='LOCAL_TEMPLATE_STORAGE_BASE_PATH':
                for build in self.closure['builds']:
                    dest=staged/build['build_id'];dest.mkdir()
                    for file in build['files']:
                        old=Path(file['path']);src=self.alias/build['build_id']/old.name
                        require(identity(src)=={k:file[k] for k in identity(src)},'Template input identity changed before copy')
                        subprocess.run(['cp','--sparse=always','--reflink=never','--preserve=mode',str(src),str(dest/old.name)],check=True)
                        sha=digest(src)
                        require(digest(dest/old.name)==sha and identity(src)=={k:file[k] for k in identity(src)},
                                'Template copy differs or source changed during staging')
                        file['sha256']=sha
            bind_private(staged,target,self.mounts)
        (self.ram/'tmp').mkdir()
        check_capacity(self.ram,'e2b-before-service-start',self.next_snapshot_bytes,
                       self.out/'capacity.jsonl',node=self.node)
        self.save('prepared.json',dict(source_sha256=self.source_sha256,node=self.node,cpus=self.cpus,
            mounts=self.mounts,dependency_closure=self.closure,tmpdir=str(self.ram/'tmp'),
            capacity_checks=[json.loads(line) for line in (self.out/'capacity.jsonl').read_text().splitlines()]))

    def verify_active(self, orchestrator):
        pid=orchestrator['process']['pid'];start=orchestrator['process']['start_ticks']
        paths=paths_from_process(pid)
        require(process_tmpdir(pid)==self.ram/'tmp', 'E2B daemon TMPDIR differs from its owned RAM directory')
        paths=dict(paths,TMPDIR=self.ram/'tmp')
        result=[]
        for key,path in paths.items():
            observed=visible_mount(pid,path);mount=observed['mount'];opts=mount['options'].split(',')
            require(mount['fstype']=='tmpfs' and 'noswap' in opts and f'mpol=bind:{self.node}' in opts,
                    'E2B daemon working data is not on actual noswap RAM')
            result.append(dict(observed,role=key))
        observed=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        require(int(observed[19])==start,'E2B daemon identity changed during storage proof')
        self.save('verified.json',dict(schema='deltabox.e2b-working-storage.v1',source_sha256=self.source_sha256,
            job=str(self.out.parent.parent),node=self.node,cpus=self.cpus,service_pid=pid,service_start_ticks=start,
            actual_paths=result,dependency_closure=self.closure,volume_ceiling_bytes=self.size*GIB,
            scope='Registered writable caches, rootfs cow, snapshot store and runtime files on RAM; original base assets copied exactly before measurement'))

    def restore_stopped(self):
        self.assert_stopped()  # Never cross this gate after a failed stop.
        from ae.scripts.run_memory_job import unmount_owned
        try:
            for record in reversed(self.mounts):
                if not record.get('unmounted'):
                    unmount_owned(record)  # Stop immediately on any failed layer.
            for text,original in self.original.items():
                require(same_directory(Path(text),original), 'Original E2B directory identity did not restore')
            for text,record in self.directories.items():
                path=Path(text);actual=identity(path)
                require(all(actual[key]==record[key] for key in ('device','inode')), 'Owned E2B RAM directory identity changed')
                path.rmdir()  # Never recurse into an old or mounted directory.
            require(all(not p.exists() and not p.is_symlink() for p in (self.ram,self.alias)),
                    'Unexpected E2B RAM/alias directory retained; no original-service restart allowed')
            for build in self.closure['builds']:
                for file in build['files']:
                    require(identity(file['path'])=={k:file[k] for k in identity(file['path'])},
                            'Original E2B template file identity changed')
                    if 'sha256' in file:
                        require(digest(file['path'])==file['sha256'],'Original E2B template bytes changed')
            self.save('restored.json',dict(source_sha256=self.source_sha256,original_paths=self.original,
                      mounts=self.mounts,status='ok',ephemeral_paths=[str(self.ram),str(self.alias)],
                      ephemeral_paths_gone=all(not p.exists() and not p.is_symlink() for p in (self.ram,self.alias))))
        except BaseException as error:
            self.save('retained.json',dict(source_sha256=self.source_sha256,mounts=self.mounts,
                      error_type=type(error).__name__,status='recovery-required'))
            raise

    def verify_restored(self, orchestrator):
        pid=orchestrator['process']['pid'];start=orchestrator['process']['start_ticks']
        paths=paths_from_process(pid)
        require(process_tmpdir(pid)==self.original_tmpdir, 'Original E2B daemon TMPDIR did not restore')
        actual={key:visible_mount(pid,path) for key,path in paths.items()}
        for key,path in paths.items():
            info=Path(f'/proc/{pid}/root{path}').stat()
            original=self.original[str(path)]
            actual[key]['observed_directory']={'device':info.st_dev,'inode':info.st_ino}
            require(info.st_dev==original['device'] and info.st_ino==original['inode'],
                    'Restarted original E2B daemon sees a different working directory')
            require(all(actual[key]['mount'][field]==self.original_mounts[key]['mount'][field]
                        for field in ('source','fstype','maj:min')),
                    'Restarted original E2B working-data filesystem differs')
        observed=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        require(int(observed[19])==start,'Restored E2B daemon identity changed during storage proof')
        self.save('restored-active.json',dict(status='ok',service_pid=pid,service_start_ticks=start,
            original_paths=self.original,actual_paths=actual,
            original_tmpdir=str(self.original_tmpdir) if self.original_tmpdir is not None else None))
