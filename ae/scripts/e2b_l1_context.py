"""Owned, disk-backed L1 for the explicit E2B paper reconstruction.

API: with owned_l1(L1Config(manifest, workspace, public_key, ssh_identity)) as vm:
         vm.verify()
         # Caller runs only the profile's fixed E2B job through vm.ssh_port.
The caller must already be a child of run_pinned_measurement.py with NUMA1/
CPU28-31 leases, and its systemd unit must have MemorySwapMax=0. This module
never takes those locks or changes frequency/global swap settings.

The root-owned manifest has schema_version=1, kind='e2b-paper-l1-assets',
disk_size_gib=80 or128, expected_l1_kernel, base_image{path,sha256}, tools mapping
qemu/qemu_img/genisoimage to {path,sha256}, shares [{path,tag,files:[{path,sha256}]}],
and ssh_port(55000..59999). Share paths must be distinct directories below
WORK/shares; file paths are relative and their complete tree is hash checked.
Base image/tools/public key are regular, non-group/world-writable files; the base is root-owned and read-only.
Tools and manifests are root-owned. The private SSH identity is an existing host file used only by ssh -i;
its bytes are never read, hashed, copied, logged or embedded in cloud-init.

All new artifacts remain in a uniquely created workspace, including failures.
4vCPU/16GiB and the chosen80/128GiB disk are declared reconstruction candidates,
NOT recovered formal eight-input L1 settings. No arbitrary command is accepted.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time
import uuid

REPO = Path('/home/atc-ae/delta-box-ae')
WORK = REPO / 'ae/work/e2b-paper-reproduction'
CPUS = {28, 29, 30, 31}
GIB = 1024 ** 3
READINESS = """python3 - <<'AE_L1_PROBE'
import json,os,pathlib,stat
m={x.split(':')[0]:int(x.split()[1]) for x in pathlib.Path('/proc/meminfo').read_text().splitlines()}
p=pathlib.Path('/dev/kvm')
print(json.dumps(dict(kernel=os.uname().release,cpus=os.cpu_count(),memory_kib=m['MemTotal'],swap_kib=m['SwapTotal'],kvm=p.exists() and stat.S_ISCHR(p.stat().st_mode))))
AE_L1_PROBE"""
SHUTDOWN = 'sudo -n sync && sudo -n systemctl poweroff'


@dataclass(frozen=True)
class L1Config:
    manifest: Path
    workspace: Path
    public_key: Path
    ssh_identity: Path
    readiness_timeout: float = 300
    stop_grace: float = 20


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def trusted(path, *, directory=False, root=True):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Expected an absolute, non-traversing path')
    for p in (path, *path.parents):
        if p.is_symlink():
            raise ValueError('Symlink path is not trusted')
    info = path.stat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('Unexpected asset type')
    if (root and info.st_uid != 0) or info.st_mode & 0o022:
        raise ValueError('Asset must be root-owned and not externally writable')
    return path


def file_asset(value):
    if not isinstance(value, dict) or set(value) != {'path', 'sha256'}:
        raise ValueError('Invalid frozen asset')
    path = trusted(value['path'])
    if not re.fullmatch(r'[0-9a-f]{64}', str(value['sha256'])) or digest(path) != value['sha256']:
        raise ValueError('Frozen asset SHA mismatch')
    return path


def read_manifest(config):
    path = trusted(config.manifest)
    if not path.is_relative_to(WORK):
        raise ValueError('Asset manifest must be in the fixed profile work directory')
    value = json.loads(path.read_text())
    if set(value) != {'schema_version', 'kind', 'disk_size_gib', 'expected_l1_kernel',
                       'base_image', 'tools', 'shares', 'ssh_port'}:
        raise ValueError('Unexpected asset manifest fields')
    if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['kind'] != 'e2b-paper-l1-assets':
        raise ValueError('Unsupported asset manifest')
    if type(value['disk_size_gib']) is not int or value['disk_size_gib'] not in (80, 128):
        raise ValueError('Unreviewed virtual disk size')
    if (type(value['ssh_port']) is not int or not 55000 <= value['ssh_port'] <= 59999
            or not re.fullmatch(r'[0-9A-Za-z._+-]+', value['expected_l1_kernel'])):
        raise ValueError('Invalid fixed L1 kernel or port')
    if set(value['tools']) != {'qemu', 'qemu_img', 'genisoimage'}:
        raise ValueError('Incomplete tool identities')
    image = file_asset(value['base_image'])
    if image.stat().st_mode & 0o222:
        raise ValueError('Frozen base image must be read-only')
    tools = {k: file_asset(v) for k, v in value['tools'].items()}
    for path in tools.values():
        if not os.access(path, os.X_OK):
            raise ValueError('Frozen tool is not executable')
    shares, seen = [], set()
    if not isinstance(value['shares'], list):
        raise ValueError('Invalid shares')
    for share in value['shares']:
        if set(share) != {'path', 'tag', 'files'}:
            raise ValueError('Invalid share identity')
        p = trusted(share['path'], directory=True)
        if not p.is_relative_to(WORK / 'shares') or p == WORK / 'shares':
            raise ValueError('Share is outside the narrow fixed share root')
        if ',' in str(p) or not re.fullmatch(r'ae_[a-z0-9_]{1,24}', share['tag']) or share['tag'] in seen:
            raise ValueError('Invalid or duplicate share tag')
        if any(p == prior or p.is_relative_to(prior) or prior.is_relative_to(p) for prior, _ in shares):
            raise ValueError('Shared directories overlap')
        seen.add(share['tag'])
        expected = {}
        for item in share['files']:
            rel = Path(item['path'])
            if set(item) != {'path', 'sha256'} or rel.is_absolute() or '..' in rel.parts or str(rel) in expected:
                raise ValueError('Invalid share member')
            expected[str(rel)] = item['sha256']
        observed = {}
        for f in p.rglob('*'):
            if f.is_symlink():
                raise ValueError('Shared tree cannot contain symlinks')
            trusted(f, directory=f.is_dir())
            if f.is_file():
                observed[str(f.relative_to(p))] = digest(f)
        if observed != expected:
            raise ValueError('Share tree differs from frozen manifest')
        shares.append((p, share['tag']))
    keypath = trusted(config.public_key, root=False)
    key = keypath.read_text().strip()
    fields = key.split()
    if (len(fields) < 2 or fields[0] not in ('ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256')
            or '\n' in key or '\r' in key):
        raise ValueError('Expected a host public SSH key, never a private key')
    try:
        blob = base64.b64decode(fields[1], validate=True)
        length = int.from_bytes(blob[:4], 'big')
        if blob[4:4+length].decode() != fields[0]:
            raise ValueError('Public key algorithm mismatch')
    except (ValueError, UnicodeError) as error:
        raise ValueError('Malformed public SSH key') from error
    identity = trusted(config.ssh_identity, root=False)
    if identity.stat().st_mode & 0o077:
        raise ValueError('Host SSH identity permissions must be private')
    return value, image, tools, shares, key


def asset_stats(config, image, tools, shares):
    paths = [config.manifest, config.public_key, image, *tools.values()]
    for directory, _ in shares:
        paths += [directory, *directory.rglob('*')]
    return {str(p): [p.stat().st_dev, p.stat().st_ino, p.stat().st_size,
                      p.stat().st_mtime_ns, p.stat().st_ctime_ns] for p in paths}


def proc_identity(pid):
    base = Path('/proc') / str(pid)
    raw = (base / 'stat').read_text()
    fields = raw[raw.rfind(')') + 2:].split()
    group = next(x.split(':', 2)[2] for x in (base / 'cgroup').read_text().splitlines() if x.startswith('0::'))
    return {'pid': pid, 'ppid': int(fields[1]), 'starttime': int(fields[19]),
            'cgroup': group, 'exe': os.readlink(base / 'exe')}


def ancestors():
    result = {}
    pid = os.getppid()
    for _ in range(64):
        if pid <= 1 or pid in result:
            break
        identity = proc_identity(pid)
        result[pid] = identity
        pid = identity['ppid']
    return result


def held_ancestor_leases():
    lineage = ancestors()
    locks = Path('/proc/locks').read_text().splitlines()
    owners = []
    for name in ['deltabox-numa-1.lock', *['deltabox-policy%d.lock' % c for c in sorted(CPUS)]]:
        p = trusted(Path('/run/lock') / name)
        s = p.stat()
        inode = (os.major(s.st_dev), os.minor(s.st_dev), s.st_ino)
        found = []
        for line in locks:
            fields = line.split()
            if len(fields) < 8 or fields[1:4] != ['FLOCK', 'ADVISORY', 'WRITE']:
                continue
            dev = fields[5].split(':')
            if len(dev) == 3 and (int(dev[0], 16), int(dev[1], 16), int(dev[2])) == inode:
                found.append(int(fields[4]))
        if len(found) != 1 or found[0] not in lineage:
            raise ValueError('Required lease is not held by a live ancestor: ' + name)
        owners.append(found[0])
    if len(set(owners)) != 1:
        raise ValueError('Resource leases have different owners')
    pid = owners[0]
    argv = (Path('/proc') / str(pid) / 'cmdline').read_bytes().split(b'\0')
    if str(REPO / 'ae/scripts/run_pinned_measurement.py').encode() not in argv:
        raise ValueError('Resource lease owner is not the pinned measurement controller')
    return lineage[pid]


def cgroup_noswap():
    group = proc_identity(os.getpid())['cgroup']
    if '..' in Path(group).parts:
        raise ValueError('Invalid cgroup')
    root = Path('/sys/fs/cgroup')
    path = root / group.lstrip('/')
    found = []
    for p in (path, *path.parents):
        if not p.is_relative_to(root):
            break
        limit = p / 'memory.swap.max'
        if limit.exists() and limit.read_text().strip() == '0':
            found.append(str(limit))
    if not found:
        raise ValueError('Caller must already have effective MemorySwapMax=0')
    return {'cgroup': group, 'zero_swap_limits': found}


def resources():
    if os.geteuid() != 0 or set(os.sched_getaffinity(0)) != CPUS:
        raise ValueError('L1 requires root under existing CPU28-31 placement')
    owner = held_ancestor_leases()
    result = subprocess.run(['/usr/bin/numactl', '--show'], text=True, capture_output=True, check=True)
    binding = {}
    for line in result.stdout.splitlines():
        if ':' in line:
            k, v = line.split(':', 1)
            binding[k.strip()] = v.strip()
    if binding.get('policy') != 'bind' or binding.get('membind') != '1':
        raise ValueError('Caller must inherit actual NUMA1 memory policy')
    for cpu in sorted(CPUS):
        p = Path('/sys/devices/system/cpu/cpu%d/cpufreq' % cpu)
        maximum = (p / 'cpuinfo_max_freq').read_text().strip()
        if any((p / key).read_text().strip() != val for key, val in
               [('scaling_governor', 'performance'), ('scaling_min_freq', maximum), ('scaling_max_freq', maximum)]):
            raise ValueError('Expected leased maximum P-state policy')
    return {'lease_owner': owner, 'numactl': binding, **cgroup_noswap()}


def capacity(workspace, disk_gib, *, allocated=0):
    if workspace.stat().st_dev != Path('/mnt/disk1').stat().st_dev:
        raise ValueError('Owned L1 work must be backed by the reviewed disk1 device')
    v = os.statvfs(workspace)
    available = v.f_bavail * v.f_frsize
    required = max(0, disk_gib * GIB - allocated) + 10 * GIB
    if available < required:
        raise ValueError('Insufficient full-growth disk capacity plus 10GiB reserve')
    return {'available_bytes': available, 'required_bytes': required, 'disk_size_gib': disk_gib}


def memory_admission():
    vals = {}
    for line in Path('/sys/devices/system/node/node1/meminfo').read_text().splitlines():
        pieces = line.split()
        vals[pieces[2].rstrip(':')] = int(pieces[3]) * 1024
    available = vals['MemFree'] + max(0, vals['Active(file)'] + vals['Inactive(file)']
              - vals['Dirty'] - vals['Writeback'] - vals['Mapped'])
    if available < 20 * GIB:
        raise ValueError('NUMA1 needs 16GiB guest plus 4GiB host headroom')
    return {'available_estimate_bytes': available, 'required_bytes': 20 * GIB}


def owned_listener(pid, port):
    wanted = '%04X' % port
    inodes = set()
    for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
        f = line.split()
        if f[1] == '0100007F:' + wanted and f[3] == '0A':
            inodes.add(f[9])
    if not inodes:
        return False
    links = set()
    for f in (Path('/proc') / str(pid) / 'fd').iterdir():
        try:
            links.add(os.readlink(f))
        except FileNotFoundError:
            pass
    return all('socket:[%s]' % x in links for x in inodes)


class L1:
    def __init__(self, config, value, tools, folder, process, identity, pidfd, state, *, preparation=False):
        self.config, self.value, self.tools, self.folder = config, value, tools, folder
        self.process, self.identity, self.pidfd, self.state = process, identity, pidfd, state
        self._preparation = preparation
        self._verified_poweroff = None
        self.ssh_port = value['ssh_port']
        self.manifest_path = folder / 'lifecycle.json'

    def save(self):
        temp = self.folder / 'lifecycle.json.tmp'
        temp.write_text(json.dumps(self.state, indent=2) + '\n')
        temp.replace(self.manifest_path)

    def same_process(self):
        if (self.identity['ppid'] != os.getpid()
                or self.identity['exe'] != str(self.tools['qemu'])
                or self.identity['cgroup'] != self.state['resources']['cgroup']):
            return False
        if self.process.poll() is not None:
            return False
        try:
            return proc_identity(self.process.pid) == self.identity
        except (FileNotFoundError, ProcessLookupError):
            return False

    def verify(self):
        if not self.same_process():
            raise RuntimeError('Owned QEMU identity changed or exited')
        frozen = self.state['frozen_asset_stats']
        for name, before in frozen.items():
            info = Path(name).stat()
            if [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns] != before:
                raise RuntimeError('Frozen asset changed while L1 was active')
        current = resources()
        if current['cgroup'] != self.identity['cgroup'] or proc_identity(self.process.pid)['ppid'] != os.getpid():
            raise RuntimeError('Owned QEMU left caller cgroup or parent')
        for task in (Path('/proc') / str(self.process.pid) / 'task').iterdir():
            if set(os.sched_getaffinity(int(task.name))) != CPUS:
                raise RuntimeError('QEMU thread affinity differs')
        status = (Path('/proc') / str(self.process.pid) / 'status').read_text()
        if not re.search(r'^VmSwap:\s+0\s+kB$', status, re.M):
            raise RuntimeError('Owned QEMU has swap or missing VmSwap evidence')
        maps = (Path('/proc') / str(self.process.pid) / 'numa_maps').read_text()
        anon = [line for line in maps.splitlines() if re.search(r'\banon=\d+', line)]
        pages = [int(n) for line in anon for node, n in re.findall(r'\bN(\d+)=(\d+)', line) if node != '1']
        if any(pages):
            raise RuntimeError('QEMU anonymous pages escaped NUMA1')
        if not any(int(n) for line in anon for n in re.findall(r'\bN1=(\d+)', line)):
            raise RuntimeError('Missing positive NUMA1 anonymous-page evidence')
        if not owned_listener(self.process.pid, self.ssh_port):
            raise RuntimeError('SSH listener is not owned by this QEMU')
        overlay = self.folder / 'l1.qcow2'
        self.state['last_verified'] = {'unix': time.time(), 'resources': current,
              'capacity': capacity(self.folder, self.value['disk_size_gib'], allocated=overlay.stat().st_blocks * 512),
              'vm_swap_kib': 0, 'numa_anonymous_maps': anon}
        self.save()

    def _ssh_argv(self, command):
        return ['/usr/bin/ssh', '-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                    '-o', 'StrictHostKeyChecking=accept-new', '-o', 'UserKnownHostsFile=' + str(self.folder / 'known_hosts'),
                    '-o', 'ConnectTimeout=2', '-o', 'ConnectionAttempts=1', '-o', 'ControlMaster=no',
                    '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
                    '-i', str(self.config.ssh_identity), '-p', str(self.ssh_port), 'ubuntu@127.0.0.1', command]

    def wait_ready(self, *, bootstrap=False):
        deadline = time.monotonic() + self.config.readiness_timeout
        last = 'SSH not ready'
        while time.monotonic() < deadline:
            if not self.same_process():
                raise RuntimeError('QEMU exited before readiness')
            if not owned_listener(self.process.pid, self.ssh_port):
                time.sleep(.25)
                continue
            argv = self._ssh_argv(READINESS)
            try:
                cp = subprocess.run(argv, text=True, capture_output=True, timeout=min(5, max(.1, deadline-time.monotonic())))
            except subprocess.TimeoutExpired:
                last = 'SSH readiness timeout'
            else:
                if cp.returncode == 0:
                    guest = json.loads(cp.stdout)
                    if (guest.get('kernel') != self.value['expected_l1_kernel'] or guest.get('cpus') != 4
                            or not 15 * 1024**2 <= guest.get('memory_kib', 0) <= 16 * 1024**2
                            or type(guest.get('cpus')) is not int or type(guest.get('swap_kib')) is not int
                            or guest.get('swap_kib') != 0 or (guest.get('kvm') is not True and not bootstrap)):
                        raise RuntimeError('Guest kernel/CPU/memory/KVM/swap differs from frozen profile')
                    self.state['guest'] = guest
                    self.verify()
                    self.state['status'] = 'bootstrap-ready' if bootstrap else 'ready'
                    self.save()
                    return
                last = 'SSH readiness exit ' + str(cp.returncode)
            time.sleep(.5)
        raise TimeoutError(last)

    def _shutdown_observation(self, *, exit_wait=None):
        """Return running/exited without equating a disappearing proc entry to drift.

        Popen.poll/wait reap only this object's child. A missing proc entry gets
        one bounded wait for that exact child; it is never permission to signal
        an unverified live PID. A positively observed identity mismatch remains
        an immediate error, even if the process may later exit.
        """
        if (self.identity['ppid'] != os.getpid()
                or self.identity['exe'] != str(self.tools['qemu'])
                or self.identity['cgroup'] != self.state['resources']['cgroup']):
            raise RuntimeError('Owned QEMU identity is invalid; no signal sent')
        code = self.process.poll()
        if code is not None:
            self.process.wait()
            return 'exited', code
        try:
            observed = proc_identity(self.process.pid)
        except (FileNotFoundError, ProcessLookupError):
            code = self.process.poll()
            if code is None:
                try:
                    code = self.process.wait(timeout=min(self.config.stop_grace,
                                              self.config.stop_grace if exit_wait is None else max(.001, exit_wait)))
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError('Owned QEMU identity unavailable while still live; no signal sent') from error
            self.process.wait()
            return 'exited', code
        if observed != self.identity:
            raise RuntimeError('Owned QEMU PID/starttime/cgroup identity changed; no signal sent')
        code = self.process.poll()
        if code is not None:
            self.process.wait()
            return 'exited', code
        return 'running', None

    def poweroff_prepared(self):
        """Cleanly stop preparation only; SSH disconnect alone proves nothing.

        The fixed command syncs and requests guest poweroff. Success requires
        this exact owned child to exit with code zero before the bounded
        deadline. A private proof is produced only here; setting a state field
        cannot bypass final process/resource validation in the context.
        """
        if not self._preparation or self._verified_poweroff is not None:
            raise RuntimeError('Clean prepared-image poweroff is preparation-only and one-shot')
        self.wait_ready()
        self.verify()
        started = time.time()
        deadline = time.monotonic() + self.config.stop_grace
        proof = {'requested_unix': started, 'identity': dict(self.identity),
                 'command': SHUTDOWN, 'identity_verified_before_request': True}
        self.state['poweroff_attempt'] = proof
        self.save()
        try:
            response = subprocess.run(self._ssh_argv(SHUTDOWN), text=True, capture_output=True,
                                      timeout=min(10, self.config.stop_grace))
        except subprocess.TimeoutExpired:
            proof['ssh_timed_out'] = True
        else:
            proof['ssh_returncode'] = response.returncode
            if response.returncode not in (0, 255):
                raise RuntimeError('Prepared guest poweroff command failed: '+str(response.returncode))
        while True:
            phase, code = self._shutdown_observation(exit_wait=deadline-time.monotonic())
            if phase == 'exited':
                if code != 0:
                    raise RuntimeError('Prepared QEMU did not exit cleanly: '+str(code))
                proof.update(qemu_returncode=code, reaped=True, completed_unix=time.time())
                self._verified_poweroff = proof
                self.state['poweroff_verified'] = dict(proof)
                self.state['status'] = 'powered-off'
                self.save()
                return
            proof['last_alive_identity_check_unix'] = time.time()
            if time.monotonic() >= deadline:
                raise TimeoutError('Prepared guest did not power off before its deadline')
            time.sleep(.25)

    def verify_poweroff(self):
        if self._verified_poweroff is None or not self._preparation:
            raise RuntimeError('Missing method-established prepared poweroff proof')
        if self._verified_poweroff['identity'] != self.identity or self.process.poll() != 0:
            raise RuntimeError('Prepared poweroff proof does not match clean owned child')
        self.process.wait(timeout=0)
        current = resources()
        if current['cgroup'] != self.state['resources']['cgroup']:
            raise RuntimeError('Caller resource cgroup changed after prepared poweroff')
        self.state['post_poweroff_resources'] = current

    def close(self):
        phase, code = self._shutdown_observation()
        if phase == 'exited':
            self.state['qemu_returncode'] = code
            return
        try:
            signal.pidfd_send_signal(self.pidfd, signal.SIGTERM)
        except ProcessLookupError:
            phase, code = self._shutdown_observation()
            if phase != 'exited':
                raise RuntimeError('Owned QEMU vanished during stop without a reaped exit')
            self.state['qemu_returncode'] = code
            return
        try:
            self.process.wait(timeout=self.config.stop_grace)
        except subprocess.TimeoutExpired:
            phase, code = self._shutdown_observation()
            if phase == 'running':
                try:
                    signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)
                except ProcessLookupError:
                    phase, code = self._shutdown_observation()
                    if phase != 'exited':
                        raise RuntimeError('Owned QEMU vanished during forced stop without a reaped exit')
                self.process.wait(timeout=10)
        self.state['qemu_returncode'] = self.process.returncode


def owned_l1(config):
    """Run a frozen prepared image; strict KVM readiness precedes the yield."""
    return _owned_l1(config, preparation=False)


def prepare_l1(config):
    """Prepare the dated cloud-image candidate without weakening measurement.

    Initial bootstrap may lack /dev/kvm, while kernel/CPU/RAM/swap/host checks
    remain mandatory. The trusted caller performs fixed preparation over SSH.
    Before normal exit this context requires strict KVM readiness. Call
    vm.poweroff_prepared() after fixed preparation for a clean image; it verifies
    strict readiness, requests guest sync/poweroff and requires owned exit0.
    Without that proof cleanup is a forced stop, not a clean-image claim. It retains
    the stopped overlay; freezing/flattening it is an external preparation step.
    This API never accepts or executes a caller-supplied shell command.
    """
    return _owned_l1(config, preparation=True)


def cleanup_spawn(process, pidfd, state, config):
    """Cleanup only the direct, unreaped child if initial /proc proof failed.

    waitid(WNOWAIT) proves kernel parenthood without reaping the PID. With
    SIGCHLD=SIG_DFL enforced at launch, that PID cannot be recycled between
    this check and signal delivery; a successfully opened pidfd pins it too.
    Used only before an identity snapshot exists, never after identity drift.
    """
    if process.poll() is not None:
        process.wait()
        state['qemu_returncode'] = process.returncode
        return
    def send(sig):
        info = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if info is not None:
            process.wait()
            return False
        if pidfd is None:
            os.kill(process.pid, sig)
        else:
            signal.pidfd_send_signal(pidfd, sig)
        return True
    if send(signal.SIGTERM):
        try:
            process.wait(timeout=config.stop_grace)
        except subprocess.TimeoutExpired:
            if send(signal.SIGKILL):
                process.wait(timeout=10)
    state['qemu_returncode'] = process.returncode
    state['startup_cleanup'] = 'kernel-confirmed direct unreaped child'


@contextmanager
def _owned_l1(config, *, preparation):
    if not (0 < config.readiness_timeout <= 600 and 0 < config.stop_grace <= 60):
        raise ValueError('Unbounded lifecycle timeout')
    value, image, tools, shares, key = read_manifest(config)
    frozen_sha = digest(config.manifest)
    before = resources()
    workspace = trusted(config.workspace, directory=True)
    if not workspace.is_relative_to(WORK) or workspace == WORK or ',' in str(workspace):
        raise ValueError('Workspace must be a dedicated descendant of the fixed profile work')
    admission = capacity(workspace, value['disk_size_gib'])
    ram = memory_admission()
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise RuntimeError('PID-fd ownership-safe signaling is required')
    if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
        raise RuntimeError('Owned L1 requires default SIGCHLD disposition to retain child ownership')
    folder = workspace / ('l1-' + uuid.uuid4().hex)
    folder.mkdir(mode=0o700)
    state = {'schema_version': 1, 'status': 'preparing', 'started_unix': time.time(),
             'asset_manifest': {'path': str(config.manifest), 'sha256': digest(config.manifest)},
             'resources': before, 'disk_admission': admission, 'memory_admission': ram,
             'reconstruction': {'vcpus': 4, 'memory_gib': 16, 'disk_size_gib': value['disk_size_gib'],
               'formal_outer_resources_unknown': True, 'candidate_not_exact_formal_configuration': True},
             'purpose': 'preparation' if preparation else 'measurement',
             'capacity_accounting': 'The frozen base already consumes statvfs space; reserve full new overlay growth plus 10GiB. No concurrently growing prepared base is permitted.',
             'files_retained_on_failure': True, 'public_key_sha256': digest(config.public_key)}
    process = None
    launching = False
    pending_signals = []
    vm = None
    pidfd = None
    log = None
    old_signals = {}
    primary_error = None
    def interrupted(signum, frame):
        if launching:
            pending_signals.append(signum)
            return
        raise KeyboardInterrupt('Owned L1 interrupted by signal ' + str(signum))
    try:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            old_signals[signum] = signal.getsignal(signum)
            signal.signal(signum, interrupted)
        (folder / 'lifecycle.json').write_text(json.dumps(state, indent=2)+'\n')
        user = '#cloud-config\nusers:\n  - name: ubuntu\n    sudo: ALL=(ALL) NOPASSWD:ALL\n    shell: /bin/bash\n    ssh_authorized_keys:\n      - ' + json.dumps(key) + '\nssh_pwauth: false\ndisable_root: true\n'
        (folder / 'user-data').write_text(user)
        (folder / 'meta-data').write_text('instance-id: '+folder.name+'\nlocal-hostname: ae-e2b-l1\n')
        for name in ('user-data', 'meta-data'):
            (folder/name).chmod(0o600)
        info = subprocess.run([str(tools['qemu_img']), 'info', '--output=json', str(image)],
                              check=True, capture_output=True, text=True, timeout=30)
        base = json.loads(info.stdout)
        if (base.get('format') != 'qcow2' or base.get('backing-filename') or base.get('full-backing-filename')
                or type(base.get('virtual-size')) is not int or base['virtual-size'] > value['disk_size_gib'] * GIB):
            raise ValueError('Prepared base must be standalone qcow2 within the reviewed disk size')
        state['base_image_info'] = base
        subprocess.run([str(tools['qemu_img']), 'create', '-f', 'qcow2', '-F', 'qcow2',
                        '-b', str(image), str(folder/'l1.qcow2'), str(value['disk_size_gib'])+'G'],
                       check=True, capture_output=True, timeout=120)
        subprocess.run([str(tools['genisoimage']), '-output', str(folder/'seed.iso'), '-volid', 'cidata',
                        '-joliet', '-rock', str(folder/'user-data'), str(folder/'meta-data')],
                       check=True, capture_output=True, timeout=60)
        # Revalidate frozen assets after preparation, before executing QEMU.
        if digest(config.manifest) != frozen_sha or read_manifest(config) != (value, image, tools, shares, key):
            raise ValueError('Frozen asset manifest changed during preparation')
        resources()
        argv = [str(tools['qemu']), '-enable-kvm', '-machine', 'q35,accel=kvm', '-cpu', 'host',
                '-smp', '4', '-m', '16G', '-drive', 'file='+str(folder/'l1.qcow2')+',if=virtio,format=qcow2,cache=none',
                '-drive', 'file='+str(folder/'seed.iso')+',if=virtio,format=raw,readonly=on',
                '-netdev', 'user,id=n0,hostfwd=tcp:127.0.0.1:'+str(value['ssh_port'])+'-:22',
                '-device', 'virtio-net-pci,netdev=n0', '-nographic', '-serial', 'mon:stdio', '-display', 'none']
        for path, tag in shares:
            argv += ['-virtfs', 'local,path='+str(path)+',mount_tag='+tag+',security_model=none,readonly=on,id='+tag]
        state['frozen_asset_stats'] = asset_stats(config, image, tools, shares)
        state['qemu_argv'] = argv
        log = (folder/'qemu.log').open('xb')
        launching = True
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        pidfd = os.pidfd_open(process.pid)
        ident = proc_identity(process.pid)
        vm = L1(config, value, tools, folder, process, ident, pidfd, state, preparation=preparation)
        if (process.poll() is not None or ident['ppid'] != os.getpid()
                or ident['exe'] != str(tools['qemu']) or ident['cgroup'] != before['cgroup']):
            raise RuntimeError('New QEMU identity is not the expected direct owned child')
        launching = False
        if pending_signals:
            raise KeyboardInterrupt('Owned L1 interrupted during launch')
        state['ownership'] = ident
        vm.save()
        vm.wait_ready(bootstrap=preparation)
        yield vm
        if preparation and vm._verified_poweroff is not None:
            vm.verify_poweroff()
        else:
            if preparation:
                vm.wait_ready()
            vm.verify()
        state['status'] = 'completed'
    except BaseException as error:
        primary_error = error
        state['status'] = 'failed'
        state['error'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        for signum in old_signals:
            signal.signal(signum, signal.SIG_IGN)
        try:
            if vm is not None:
                vm.close()
            elif process is not None:
                cleanup_spawn(process, pidfd, state, config)
        except BaseException as error:
            state['status'] = 'failed'
            state['cleanup_error'] = type(error).__name__ + ': ' + str(error)
            if primary_error is None:
                raise
        finally:
            state['finished_unix'] = time.time()
            (folder/'lifecycle.json').write_text(json.dumps(state, indent=2)+'\n')
            if pidfd is not None:
                os.close(pidfd)
            if log is not None:
                log.close()
            for signum, handler in old_signals.items():
                signal.signal(signum, handler)
