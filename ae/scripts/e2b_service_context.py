"""Protected fixed-NUMA placement for the existing hosted E2B fanout service.

Only the registered local daemon/container/cgroup identities are changed.
Daemon restarts give them a fresh, reversible NUMA policy. SDK credentials stay
in process memory; manifests contain identities and placement, never environment.
The caller must hold the results and NUMA leases until restoration completes.
"""
from contextlib import contextmanager
import datetime
import hashlib
import errno
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
import traceback
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
CGROUP = Path('/sys/fs/cgroup')
VM_ROOT = CGROUP / 'e2b'
PROC = Path('/proc')
SYSTEMD_RUNTIME = Path('/run/systemd/system')
SYSTEMD_CONTROL = Path('/run/systemd/system.control')
UNITS = ('ae-e2b-api.service', 'ae-e2b-client-proxy.service', 'ae-e2b-orchestrator.service')
CONTAINERS = ('ae-e2b-postgres', 'ae-e2b-clickhouse', 'ae-e2b-redis')
PROPERTIES = ('AllowedCPUs', 'AllowedMemoryNodes', 'MemorySwapMax', 'CPUAffinity', 'NUMAPolicy', 'NUMAMask')
RESOURCE_PROPERTIES = PROPERTIES[:3]
CG_FILES = ('cpuset.cpus', 'cpuset.cpus.effective', 'cpuset.mems', 'cpuset.mems.effective',
            'memory.swap.max', 'memory.swap.current', 'cgroup.subtree_control', 'cgroup.events')
GUARD = ROOT / 'ae/work/E2B_SERVICE_RECOVERY_REQUIRED.json'
DROP_NAME = 'zzzz-deltabox-numa03-e2b.conf'
API_LOG = Path('/mnt/disk2/dyp/ae-hosted-20260922/e2b/logs/api.log')
LANE_CPUS = {0: '0-3', 1: '28-31', 2: '48-51', 3: '72-75'}


def run(*args):
    subprocess.run(list(map(str, args)), check=True, stdout=subprocess.DEVNULL)


def output(*args):
    return subprocess.check_output(list(map(str, args)), text=True).strip()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


def error_record(error):
    if error is None:
        return None
    message = str(error)
    key = os.environ.get('E2B_API_KEY')
    if key:
        message = message.replace(key, '<redacted>')
    return {'type': type(error).__name__, 'message': message}


def assert_backend_ready():
    if GUARD.exists():
        raise RuntimeError('Earlier E2B service recovery is required: ' + str(GUARD))


def cpuset(value):
    result = set()
    for item in str(value).replace(' ', ',').split(','):
        if not item:
            continue
        if not re.fullmatch(r'\d+(?:-\d+)?', item):
            raise ValueError('Invalid CPU/node mask')
        a, _, b = item.partition('-')
        if int(b or a) < int(a):
            raise ValueError('Invalid CPU/node range')
        result.update(range(int(a), int(b or a) + 1))
    return result


def ancestor_pids():
    result, pid = set(), os.getpid()
    while pid > 0 and pid not in result:
        result.add(pid)
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        pid = int(fields[1])
    return result


def require_results_lease():
    path = ROOT / 'ae/work/.results.lock'
    info = path.stat()
    wanted = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    parents = ancestor_pids()
    for line in Path('/proc/locks').read_text().splitlines():
        fields = line.split()
        if len(fields) < 8 or fields[1:4] != ['FLOCK', 'ADVISORY', 'WRITE']:
            continue
        dev = fields[5].split(':')
        if len(dev) == 3 and (int(dev[0], 16), int(dev[1], 16), int(dev[2])) == wanted and int(fields[4]) in parents:
            return int(fields[4])
    raise ValueError('E2B service placement requires an ancestor results EX lease')


def require_admission(config, node, cpus):
    if os.geteuid() != 0 or type(node) is not int or node not in LANE_CPUS:
        raise ValueError('E2B service placement requires root and NUMA0/1/2/3')
    settings = config.get('e2b', {})
    if settings.get('api_url', '').rstrip('/') not in ('http://127.0.0.1:3100', 'http://localhost:3100') or settings.get('sandbox_url', '').rstrip('/') not in ('http://127.0.0.1:3102', 'http://localhost:3102'):
        raise ValueError('E2B service placement requires the registered local API')
    if cpuset(cpus) != cpuset(LANE_CPUS[node]):
        raise ValueError('E2B service placement requires the fixed CPUs for its NUMA lane')
    assert_backend_ready()
    owner = require_pinned_parent(node, cpus)
    return {'numa_lease_owner': owner, 'results_lease_owner': require_results_lease()}


def require_pinned_parent(node, cpus):
    from ae.scripts.cube_control_context import require_pinned_parent as existing_admission
    return existing_admission(node, cpus)


def file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(4 * 1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def process(pid, *, before_numa_maps=None):
    if before_numa_maps is not None:
        return observed_process(pid, before_numa_maps)
    base = Path('/proc') / str(pid)
    fields = (base / 'stat').read_text().rsplit(')', 1)[1].split()
    status = dict(line.split(':', 1) for line in (base / 'status').read_text().splitlines() if ':' in line)
    policies, resident = {}, {}
    for line in (base / 'numa_maps').read_text().splitlines():
        values = line.split()
        if len(values) > 1:
            policies[values[1]] = policies.get(values[1], 0) + 1
        for value in values[2:]:
            match = re.fullmatch(r'N(\d+)=(\d+)', value)
            if match:
                resident[match[1]] = resident.get(match[1], 0) + int(match[2])
    return {'pid': pid, 'start_ticks': int(fields[19]), 'exe': str((base / 'exe').resolve()),
            'cpus': status['Cpus_allowed_list'].strip(), 'mems': status['Mems_allowed_list'].strip(),
            'cgroup': (base / 'cgroup').read_text().strip(), 'numa_policies': policies,
            'resident_pages_by_node': resident}


class VMProcessReadGone(RuntimeError):
    """ESRCH at one exact VM /proc read; never sufficient as exit proof."""
    def __init__(self, row, path):
        super().__init__('ESRCH reading VM proc file: ' + str(path))
        self.row, self.path, self.observed_at = dict(row), str(path), time.time()
        self.operation = Path(path).name


class NumaMapsProcessGone(VMProcessReadGone):
    """Retained name for the original, prevalidated numa_maps read."""


@contextmanager
def vm_proc_read(path, row):
    # Only VM observer reads enter here. Root/cgroup, service-main and observer
    # identity reads remain strict, as do permission/I/O and semantic failures.
    try:
        yield
    except OSError as error:
        if error.errno != errno.ESRCH:
            raise
        raise VMProcessReadGone(row, path) from error


class CgroupChildDisappeared(RuntimeError):
    """Only an exact child.lstat ENOENT after validating known process fields."""
    def __init__(self, task, owner_pid, owner_start, child, identity, root_identity, stage):
        self.observation = {
            'operation': 'child.lstat', 'errno': errno.ENOENT, 'stage': stage,
            'error_observed_at': time.time(), 'target': dict(task),
            'owner_pid': owner_pid, 'owner_start_ticks': owner_start,
            'child': {'path': str(child), 'device': identity.st_dev, 'inode': identity.st_ino},
            'root': {'path': str(VM_ROOT), 'device': root_identity[0], 'inode': root_identity[1]},
        }
        super().__init__('E2B VM child disappeared during ' + stage + ': ' + str(child))


def proc_start(base):
    return int((base / 'stat').read_text().rsplit(')', 1)[1].split()[19])


def observed_process(pid, validate):
    base = PROC / str(pid)
    row = {'pid': pid}
    with vm_proc_read(base / 'stat', row):
        started = proc_start(base)
    row['start_ticks'] = started
    with vm_proc_read(base / 'status', row):
        status = dict(line.split(':', 1) for line in (base / 'status').read_text().splitlines() if ':' in line)
    row.update(tgid=int(status['Tgid']), cpus=status['Cpus_allowed_list'].strip(),
               mems=status['Mems_allowed_list'].strip(), numa_policies={}, resident_pages_by_node={})
    validate(row, metadata_only=True)
    with vm_proc_read(base / 'cgroup', row):
        row['cgroup'] = (base / 'cgroup').read_text().strip()
    # Validate known placement/ownership before a later read can disappear.
    validate(row, stage='pre-numa_maps')
    with vm_proc_read(base / 'exe', row):
        row['exe'] = str((base / 'exe').resolve())
    with vm_proc_read(base / 'stat', row):
        if proc_start(base) != started:
            raise RuntimeError('E2B VM PID was reused before numa_maps')
    path = base / 'numa_maps'
    try:
        text = path.read_text()
    except OSError as error:
        if error.errno != errno.ESRCH:
            raise
        raise NumaMapsProcessGone(row, path) from error
    for line in text.splitlines():
        values = line.split()
        if len(values) > 1:
            policy = values[1]
            row['numa_policies'][policy] = row['numa_policies'].get(policy, 0) + 1
        for value in values[2:]:
            match = re.fullmatch(r'N(\d+)=(\d+)', value)
            if match:
                node = match[1]
                row['resident_pages_by_node'][node] = row['resident_pages_by_node'].get(node, 0) + int(match[2])
    validate(row, stage='post-numa_maps')
    with vm_proc_read(base / 'stat', row):
        if proc_start(base) != started:
            raise RuntimeError('E2B VM PID was reused during observation')
    return row


def cgroup(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError('Unexpected E2B cgroup ownership')
    row = {'path': str(path), 'inode': info.st_ino}
    for name in CG_FILES:
        row[name] = (path / name).read_text().strip()
    return row


def cgroup_processes(path):
    pids = set()
    for file in path.rglob('cgroup.procs'):
        try:
            pids.update(map(int, file.read_text().split()))
        except FileNotFoundError:
            pass
    return sorted(pids)


def live_snapshot_processes(pids):
    """A child exiting between enumeration and /proc reads is an observation gap."""
    rows = []
    for pid in pids:
        try:
            rows.append(process(pid))
        except OSError as error:
            if error.errno not in (errno.ENOENT, errno.ESRCH):
                raise
            print('[snapshot-transient] ' + json.dumps({'pid': pid,
                'reason': 'process-exited-during-read', 'errno': error.errno}), flush=True)
    return rows


def threads(pid, *, before_numa_maps=None):
    if before_numa_maps is not None:
        with vm_proc_read(PROC / str(pid) / 'task', {'pid': pid}):
            return [process(int(p.name), before_numa_maps=before_numa_maps) for p in (PROC / str(pid) / 'task').iterdir()]
    return live_snapshot_processes([int(p.name) for p in (Path('/proc') / str(pid) / 'task').iterdir()])


def unit(name):
    keys = ('Id', 'ActiveState', 'SubState', 'MainPID', 'ControlGroup', 'FragmentPath', 'DropInPaths', *PROPERTIES)
    text = output('systemctl', 'show', name, *sum([['-p', k] for k in keys], []))
    row = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
    expected = '/system.slice/' + name
    if row.get('Id') != name or row.get('ActiveState') != 'active' or row.get('SubState') != 'running' or row.get('ControlGroup') != expected:
        raise RuntimeError('Unexpected E2B service identity: ' + name)
    pid = int(row['MainPID'])
    if pid <= 0:
        raise RuntimeError('E2B service has no main process: ' + name)
    row['process'] = process(pid)
    row['binary_sha256'] = file_digest(Path(row['process']['exe']))
    if row['process']['cgroup'] != '0::' + expected:
        raise RuntimeError('E2B daemon escaped its registered unit')
    row['cgroup'] = cgroup(CGROUP / expected.lstrip('/'))
    row['tasks'] = live_snapshot_processes(cgroup_processes(CGROUP / expected.lstrip('/')))
    row['threads'] = threads(pid)
    row['dropins'] = {}
    for filename in [row['FragmentPath'], *row['DropInPaths'].split()]:
        path = Path(filename)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('E2B unit configuration is not root trusted')
        row['dropins'][filename] = hashlib.sha256(path.read_bytes()).hexdigest()
    if proc_start(Path('/proc') / str(pid)) != row['process']['start_ticks']:
        raise RuntimeError('E2B daemon changed during snapshot: ' + name)
    return row


def container(name):
    value = json.loads(output('docker', 'inspect', name))[0]
    if not value['State']['Running'] or value['Name'] != '/' + name:
        raise RuntimeError('Unexpected E2B container identity: ' + name)
    pid = int(value['State']['Pid'])
    row = {'id': value['Id'], 'pid': pid, 'started_at': value['State']['StartedAt'],
           'cpus': value['HostConfig']['CpusetCpus'], 'mems': value['HostConfig']['CpusetMems'],
           'process': process(pid)}
    expected = '/system.slice/docker-' + row['id'] + '.scope'
    if row['process']['cgroup'] != '0::' + expected:
        raise RuntimeError('Unexpected E2B container cgroup')
    row['cgroup'] = cgroup(CGROUP / expected.lstrip('/'))
    row['tasks'] = live_snapshot_processes(cgroup_processes(CGROUP / expected.lstrip('/')))
    row['threads'] = [thread for task in row['tasks'] for thread in threads(task['pid'])]
    if proc_start(Path('/proc') / str(pid)) != row['process']['start_ticks']:
        raise RuntimeError('E2B container changed during snapshot: ' + name)
    return row


def inventory(config):
    key = os.environ.get('E2B_API_KEY')
    if not key:
        raise ValueError('E2B_API_KEY is required in memory')
    request = urllib.request.Request(config['e2b']['api_url'].rstrip('/') + '/v2/sandboxes?state=running&limit=100', headers={'X-API-KEY': key})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            rows = json.load(response)
    except Exception as error:
        # HTTP response/error bodies can include secrets; do not retain them.
        raise RuntimeError('E2B inventory request failed: ' + type(error).__name__) from None
    if not isinstance(rows, list):
        raise RuntimeError('Invalid E2B inventory response')
    return [{'id': item['sandboxID'], 'run': item.get('metadata', {}).get('official_fork_run')} for item in rows]


def idle(config, timeout=30):
    end = time.monotonic() + timeout
    while True:
        records = inventory(config)
        group = cgroup(VM_ROOT)
        children = list(VM_ROOT.iterdir())
        child_groups = [p for p in children if p.is_dir()]
        if not records and 'populated 0' in group['cgroup.events'] and not cgroup_processes(VM_ROOT) and not child_groups:
            return group
        if time.monotonic() >= end:
            raise RuntimeError('E2B inventory or VM cgroup is not empty; recovery required')
        time.sleep(.2)


def identity_matches(before, actual):
    return (before['pid'], before['start_ticks'], before['exe']) == (actual['pid'], actual['start_ticks'], actual['exe'])


def same_instance(before, actual):
    # A registered Python ExecStart wrapper execs Go without changing its PID
    # or start tick. Its transaction ownership survives that expected exec.
    return (before['pid'], before['start_ticks']) == (actual['pid'], actual['start_ticks'])


def check_owned_units(identities):
    for name in UNITS:
        text = output('systemctl', 'show', name, '-p', 'MainPID', '-p', 'ActiveState')
        state = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        if state.get('MainPID') == '0' and state.get('ActiveState') in ('inactive', 'failed'):
            continue
        if not same_instance(identities[name], unit(name)['process']):
            raise RuntimeError('E2B daemon identity changed; refusing to stop unknown instance: ' + name)


def require_stopped_units():
    for name in UNITS:
        text = output('systemctl', 'show', name, '-p', 'MainPID', '-p', 'ActiveState')
        state = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        if state.get('MainPID') != '0' or state.get('ActiveState') not in ('inactive', 'failed'):
            raise RuntimeError('E2B daemon has not stopped: ' + name)
        path = CGROUP / 'system.slice' / name
        if path.exists():
            row = cgroup(path)
            if 'populated 0' not in row['cgroup.events'] or cgroup_processes(path):
                raise RuntimeError('Stopped E2B daemon cgroup remains populated: ' + name)


def stop_owned_units(identities):
    check_owned_units(identities)
    run('systemctl', 'stop', *UNITS)
    require_stopped_units()


def assert_pinned(row, node, cpus, *, swap=True):
    group = row['cgroup']
    if cpuset(group['cpuset.cpus.effective']) != cpuset(cpus) or cpuset(group['cpuset.mems.effective']) != {node}:
        raise RuntimeError('E2B effective cgroup placement differs from the lane')
    if swap and (group['memory.swap.max'] != '0' or group['memory.swap.current'] != '0'):
        raise RuntimeError('E2B service has not established effective no-swap')
    for task in row.get('tasks', []) + row.get('threads', []):
        if not cpuset(task['cpus']) or not cpuset(task['cpus']) <= cpuset(cpus) or cpuset(task['mems']) != {node}:
            raise RuntimeError('E2B task placement differs from the lane')
        if any(policy != 'default' and policy != 'bind:' + str(node) for policy in task['numa_policies']):
            raise RuntimeError('E2B actual memory policy differs from the lane')


def snapshot():
    return {'units': {u: unit(u) for u in UNITS}, 'containers': {n: container(n) for n in CONTAINERS}, 'vm_root': cgroup(VM_ROOT)}


def start_ticks(pid):
    return int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19])


class APINodeReadiness:
    """Read only fresh status emitted by this registered API restart.

    The registered API has no usable admin token (/nodes returns 401). Its
    internal status logs call the same Node.Status() used by placement. Bind
    the append-only log's inode/EOF before stop, then the new PID/start tick;
    old statuses, PID reuse, log rotation and truncation cannot satisfy this
    gate. No SDK create or timed operation is retried here.
    """
    def __init__(self, original):
        self.original = {'pid': original['pid'], 'start_ticks': original['start_ticks']}
        self.api = None
        self.captured_at = datetime.datetime.now(datetime.timezone.utc)
        self.startup = None
        self.latest = None
        self.partial = b''
        self.fd_identity(self.original)
        fd = os.open(API_LOG, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError('Registered API log is not a regular file')
            self.device, self.inode, self.offset = info.st_dev, info.st_ino, info.st_size
        finally:
            os.close(fd)
        self.initial_offset = self.offset
        self.fd_identity(self.original)

    def fd_identity(self, api):
        if start_ticks(api['pid']) != api['start_ticks']:
            raise RuntimeError('Registered API PID/start identity changed during readiness')
        fd_path = Path(f"/proc/{api['pid']}/fd/1")
        if os.readlink(fd_path) != str(API_LOG):
            raise RuntimeError('Registered API stdout differs from its known log')
        observed, file = fd_path.stat(), API_LOG.lstat()
        if not stat.S_ISREG(file.st_mode) or (observed.st_dev, observed.st_ino) != (file.st_dev, file.st_ino):
            raise RuntimeError('Registered API log identity differs from stdout')
        if hasattr(self, 'inode') and (file.st_dev, file.st_ino) != (self.device, self.inode):
            raise RuntimeError('Registered API log rotated during readiness')
        if start_ticks(api['pid']) != api['start_ticks']:
            raise RuntimeError('Registered API PID/start identity changed during readiness')

    def bind(self, api):
        value = {'pid': api['pid'], 'start_ticks': api['start_ticks']}
        if value == self.original or value['start_ticks'] < self.original['start_ticks']:
            raise RuntimeError('API readiness requires the new registered restart')
        self.fd_identity(value)
        if self.api is not None and self.api != value:
            raise RuntimeError('API restarted again during readiness')
        self.api = value

    def before(self):
        return {'path': str(API_LOG), 'device': self.device, 'inode': self.inode,
                'offset': self.initial_offset, 'captured_at': self.captured_at.isoformat(),
                'original_api': self.original}

    def next_restart(self):
        # Original daemons are now stopped. Bind a fresh EOF to the already
        # admitted inode before starting restoration; no dead PID is read.
        result = object.__new__(type(self))
        result.original = self.api or self.original
        result.api, result.startup, result.latest = None, None, None
        result.partial = b''
        result.captured_at = datetime.datetime.now(datetime.timezone.utc)
        fd = os.open(API_LOG, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (self.device, self.inode) or info.st_size < self.offset:
                raise RuntimeError('Registered API log changed before original-service restart')
            result.device, result.inode = info.st_dev, info.st_ino
            result.offset = result.initial_offset = info.st_size
        finally:
            os.close(fd)
        return result

    def parse(self, raw, offset):
        # Console JSON begins after the message; never save arbitrary log text.
        text = raw.decode('utf-8', errors='replace')
        match = re.fullmatch(r'(\S+)\s+(?:\x1b\[[0-9;]*m)*INFO(?:\x1b\[[0-9;]*m)*\s+(Starting API service\.\.\.|API internal status)\s+(\{.*\})', text)
        if not match:
            return
        try:
            # The Go console logger writes +0800; Python 3.10's
            # fromisoformat accepts +08:00 but rejects that compact offset.
            timestamp = re.sub(r'([+-]\d{2})(\d{2})$', r'\1:\2', match[1])
            at = datetime.datetime.fromisoformat(timestamp)
            row = json.loads(match[3])
        except (ValueError, TypeError):
            return
        if at.tzinfo is None or at < self.captured_at or at > datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=1):
            return
        if row.get('service') != 'orchestration-api' or row.get('internal') is not True or type(row.get('pid')) is not int or row['pid'] != self.api['pid']:
            return
        record = {'logged_at': at.isoformat(), 'offset': offset, 'bytes': len(raw),
                  'sha256': hashlib.sha256(raw).hexdigest()}
        if match[2] == 'Starting API service...':
            instance = row.get('service.instance.id')
            if not isinstance(instance, str) or not instance:
                return
            if self.startup is not None and self.startup['service_instance_id'] != instance:
                raise RuntimeError('API service instance changed during readiness')
            self.startup = dict(record, service_instance_id=instance)
        elif self.startup is not None and at >= datetime.datetime.fromisoformat(self.startup['logged_at']):
            # Retain the latest status, including a later transition away from
            # ready in the same read; an earlier ready line cannot mask it.
            nodes = row.get('nodes')
            ready = (type(row.get('nodes_count')) is int and row['nodes_count'] == 1
                     and isinstance(nodes, list) and len(nodes) == 1 and isinstance(nodes[0], dict)
                     and nodes[0].get('id') == 'local' and nodes[0].get('status') == 'ready'
                     and type(nodes[0].get('sandboxes')) is int and nodes[0]['sandboxes'] == 0)
            self.latest = dict(record, ready=ready,
                nodes_count=row['nodes_count'] if type(row.get('nodes_count')) is int else None,
                local_node={'id':'local','status':'ready','sandboxes':0} if ready else None)

    def poll(self, api):
        self.bind(api)
        fd = os.open(API_LOG, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (self.device, self.inode) or info.st_size < self.offset:
                raise RuntimeError('Registered API log rotated/truncated during readiness')
            os.lseek(fd, self.offset, os.SEEK_SET)
            raw = os.read(fd, 1024**2)
            cursor = self.offset - len(self.partial)
            self.offset += len(raw)
            lines = (self.partial + raw).split(b'\n')
            self.partial = lines.pop()
            if len(self.partial) > 64*1024:
                raise RuntimeError('Unbounded API readiness log line')
            for line in lines:
                self.parse(line, cursor)
                cursor += len(line) + 1
            caught_up = self.offset == os.fstat(fd).st_size and not self.partial
        finally:
            os.close(fd)
        self.fd_identity(self.api)
        return bool(caught_up and self.latest and self.latest['ready'])

    def evidence(self):
        return {**self.before(), 'api': self.api, 'startup': self.startup,
                'ready_status': self.latest, 'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'scope': 'Fresh registered API internal status; local node ready and idle before timed SDK work'}


def wait_actual(config, before, node, cpus, *, readiness, timeout=90):
    """Type=simple wrappers can briefly still be Python while they exec Go."""
    end = time.monotonic() + timeout
    while True:
        try:
            actual = snapshot()
            for name, row in actual['units'].items():
                if row['process']['exe'] != before['units'][name]['process']['exe'] or row['binary_sha256'] != before['units'][name]['binary_sha256']:
                    raise RuntimeError('E2B daemon wrapper has not execed its original binary')
                assert_pinned(row, node, cpus)
            for row in actual['containers'].values():
                assert_pinned(row, node, cpus, swap=False)
            assert_pinned({'cgroup': actual['vm_root']}, node, cpus)
            idle(config, timeout=1)
            if not readiness.poll(actual['units']['ae-e2b-api.service']['process']):
                raise RuntimeError('Registered API local node is not ready after restart')
            actual['api_node_readiness'] = readiness.evidence()
            return actual
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            if time.monotonic() >= end:
                raise
            time.sleep(.2)


def observer_placement(node, cpus, admission):
    from ae.scripts.e2b_observer_placement import ThreadPlacement, admit
    return ThreadPlacement(admit(ROOT, node, cpus, admission))


class VMProof:
    def __init__(self, node, cpus, *, observer=None):
        self.node, self.cpus = node, cpus
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.observer = observer
        self.rows, self.errors = {}, []
        self.discarded_cgroups = []
        self.discarded_processes = []
        self.worker = threading.Thread(target=self.watch, daemon=True)

    def _child_inputs(self, child, root_identity):
        # Kernfs may return ENODEV during removal; require independent deletion proof.
        # Keep this catch around cgroup reads, never around /proc or root reads.
        try:
            identity = child.lstat()
        except FileNotFoundError:
            self._discard_deleted_child(child, None, root_identity, 'lstat', errno.ENOENT)
            return None
        if not stat.S_ISDIR(identity.st_mode) or identity.st_uid != 0 or identity.st_mode & 0o022:
            raise RuntimeError('Unexpected E2B cgroup ownership')
        operation = 'cgroup-files'
        try:
            group = cgroup(child)
            if group['inode'] != identity.st_ino:
                raise RuntimeError('E2B VM cgroup identity changed during observation')
            # A later disappearance must not hide a violation already observed.
            assert_pinned({'cgroup': group}, self.node, self.cpus, swap=False)
            if group['memory.swap.current'] != '0':
                raise RuntimeError('E2B VM has existing swap despite the ancestor limit')
            operation = 'cgroup.procs'
            pids = cgroup_processes(child)
        except OSError as error:
            if error.errno not in (errno.ENODEV, errno.ENOENT):
                raise
            self._discard_deleted_child(child, identity, root_identity, operation, error.errno)
            return None
        try:
            current = child.lstat()
        except FileNotFoundError:
            self._discard_deleted_child(child, identity, root_identity, 'identity-recheck', errno.ENOENT)
            return None
        if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
            raise RuntimeError('E2B VM cgroup identity changed during observation')
        return group, pids, identity

    def _discard_deleted_child(self, child, identity, root_identity, operation, error_number):
        # kernfs deactivates control files before unlinking the directory. Give
        # that same observed leaf a bounded interval to disappear; never reread
        # it as successful evidence or waive full VM identity coverage.
        confirmation_started = time.monotonic()
        confirmation_limit = .05
        present_checks = 0
        while True:
            try:
                current = child.lstat()
            except FileNotFoundError:
                break
            if identity is not None and (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise RuntimeError('E2B VM cgroup was replaced during observation')
            remaining = confirmation_limit - (time.monotonic() - confirmation_started)
            if identity is None or remaining <= 0:
                raise RuntimeError('E2B VM cgroup read failed while the observed leaf remains present'
                    + f' (operation={operation}, errno={error_number}, path={child}, inode={current.st_ino})')
            present_checks += 1
            time.sleep(min(.002, remaining))
        current_root = VM_ROOT.lstat()
        if (current_root.st_dev, current_root.st_ino) != root_identity:
            raise RuntimeError('E2B VM root identity changed during observation')
        root = cgroup(VM_ROOT)
        if root['inode'] != root_identity[1]:
            raise RuntimeError('E2B VM root identity changed during observation')
        assert_pinned({'cgroup': root}, self.node, self.cpus)
        # Do not turn a same-name replacement into deletion evidence.
        try:
            child.lstat()
        except FileNotFoundError:
            pass
        else:
            raise RuntimeError('E2B VM cgroup reappeared during deletion verification')
        self.discarded_cgroups.append({'path': str(child), 'device': identity.st_dev if identity is not None else None,
            'inode': identity.st_ino if identity is not None else None, 'operation': operation,
            'errno': error_number, 'observed_at': time.time(),
            'deletion_confirmation': {'present_checks': present_checks, 'wait_s': time.monotonic() - confirmation_started, 'limit_s': confirmation_limit},
            'reason': 'child absent; incomplete observation discarded'})

    def _validate_process(self, task, owner_pid, owner_start, child, identity, root_identity, group, *, metadata_only=False, stage='pre-numa_maps'):
        if task['pid'] == owner_pid and task['start_ticks'] != owner_start:
            raise RuntimeError('E2B VM owner PID was reused before numa_maps validation')
        if task['tgid'] != owner_pid:
            raise RuntimeError('E2B VM thread belongs to a different process')
        assert_pinned({'cgroup': group, 'tasks': [task]}, self.node, self.cpus, swap=False)
        if metadata_only:
            return
        expected = '0::/e2b/' + child.name
        if task['cgroup'] != expected and not task['cgroup'].startswith(expected + '/'):
            raise RuntimeError('E2B VM process escaped its observed cgroup')
        with vm_proc_read(PROC / str(owner_pid) / 'stat', {'pid': owner_pid, 'start_ticks': owner_start}):
            if proc_start(PROC / str(owner_pid)) != owner_start:
                raise RuntimeError('E2B VM owner PID was reused during observation')
        # Check already-readable leader/TID identity before a child can disappear.
        with vm_proc_read(PROC / str(owner_pid) / 'task' / str(task['pid']) / 'stat', task):
            if proc_start(PROC / str(owner_pid) / 'task' / str(task['pid'])) != task['start_ticks']:
                raise RuntimeError('E2B VM thread identity changed during observation')
        if stage not in ('pre-numa_maps', 'post-numa_maps'):
            raise RuntimeError('Unexpected E2B VM process validation stage')
        try:
            root_now = VM_ROOT.lstat()
        except FileNotFoundError as error:
            raise RuntimeError('E2B VM root missing during ' + stage) from error
        if (root_now.st_dev, root_now.st_ino) != root_identity:
            raise RuntimeError('E2B VM root identity changed during process observation')
        # Catch only this child lstat; proc/root and other errno stay strict.
        try:
            child_now = child.lstat()
        except FileNotFoundError as error:
            raise CgroupChildDisappeared(task, owner_pid, owner_start, child,
                                         identity, root_identity, stage) from error
        if (child_now.st_dev, child_now.st_ino) != (identity.st_dev, identity.st_ino):
            raise RuntimeError('E2B VM cgroup identity changed during process observation')

    def _discard_exited_process(self, error, owner_pid, owner_start, child, identity, root_identity):
        task = error.row
        def absent_target():
            try:
                (PROC / str(task['pid'])).lstat()
            except FileNotFoundError:
                return
            # Present, reused, inaccessible, and zombie targets are all fatal.
            raise RuntimeError('E2B VM proc-read ESRCH target remains present or was reused')
        def owner_state():
            owner = PROC / str(owner_pid)
            try:
                owner.lstat()
            except FileNotFoundError:
                return 'absent'
            try:
                if proc_start(owner) != owner_start:
                    raise RuntimeError('E2B VM owner PID was reused after proc-read ESRCH')
                expected = '0::/e2b/' + child.name
                member = (owner / 'cgroup').read_text().strip()
                if member != expected and not member.startswith(expected + '/'):
                    raise RuntimeError('E2B VM owner escaped its observed cgroup')
                try:
                    (owner / 'task' / str(task['pid'])).lstat()
                except FileNotFoundError:
                    return 'same owner; target thread absent'
            except OSError as read_error:
                if read_error.errno not in (errno.ENOENT, errno.ESRCH):
                    raise
                try:
                    owner.lstat()
                except FileNotFoundError:
                    return 'absent'
                raise
            raise RuntimeError('E2B VM target remains in its owner task directory')
        absent_target()
        owner_observation = owner_state()
        root_now = VM_ROOT.lstat()
        if (root_now.st_dev, root_now.st_ino) != root_identity:
            raise RuntimeError('E2B VM root identity changed after proc-read ESRCH')
        root = cgroup(VM_ROOT)
        if root['inode'] != root_identity[1]:
            raise RuntimeError('E2B VM root identity changed after proc-read ESRCH')
        assert_pinned({'cgroup': root}, self.node, self.cpus)
        try:
            child_now = child.lstat()
        except FileNotFoundError:
            self._discard_deleted_child(child, identity, root_identity, 'numa_maps-exit-recheck', errno.ENOENT)
        else:
            if (child_now.st_dev, child_now.st_ino) != (identity.st_dev, identity.st_ino):
                raise RuntimeError('E2B VM child was replaced after proc-read ESRCH')
            group = cgroup(child)
            if group['inode'] != identity.st_ino:
                raise RuntimeError('E2B VM child identity changed after proc-read ESRCH')
            assert_pinned({'cgroup': group}, self.node, self.cpus, swap=False)
            if group['memory.swap.current'] != '0':
                raise RuntimeError('E2B VM has existing swap despite the ancestor limit')
        absent_target()
        owner_after = owner_state()
        if owner_after != owner_observation and owner_after != 'absent':
            raise RuntimeError('E2B VM owner changed during disappearance verification')
        self.discarded_processes.append({'operation': error.operation, 'path': error.path, 'errno': errno.ESRCH,
            'error_observed_at': error.observed_at, 'verified_at': time.time(),
            'target': task, 'owner_pid': owner_pid, 'owner_start_ticks': owner_start,
            'owner_after': owner_after, 'owner_before_recheck': owner_observation, 'child': {'path': str(child), 'device': identity.st_dev, 'inode': identity.st_ino},
            'root': {'path': str(VM_ROOT), 'device': root_identity[0], 'inode': root_identity[1]},
            'reason': 'bound target independently absent; incomplete child sample (all tasks and threads) discarded'})

    def sample(self):
        root = cgroup(VM_ROOT)
        assert_pinned({'cgroup': root}, self.node, self.cpus)
        root_info = VM_ROOT.lstat()
        if root_info.st_ino != root['inode']:
            raise RuntimeError('E2B VM root identity changed during observation')
        root_identity = (root_info.st_dev, root_info.st_ino)
        for child in VM_ROOT.iterdir():
            if not child.is_dir():
                continue
            if not re.fullmatch(r'sbx-[A-Za-z0-9]+-[A-Za-z0-9]+', child.name):
                raise RuntimeError('Unexpected E2B VM cgroup name')
            inputs = self._child_inputs(child, root_identity)
            if inputs is None:
                continue
            group, pids, identity = inputs
            row = {'cgroup': group, 'tasks': []}
            incomplete = False
            for pid in pids:
                started = None
                try:
                    with vm_proc_read(PROC / str(pid) / 'stat', {'pid': pid}):
                        started = start_ticks(pid)
                    validate = lambda task, **kwargs: self._validate_process(task, pid, started, child, identity, root_identity, group, **kwargs)
                    task = process(pid, before_numa_maps=validate)
                    if task['start_ticks'] != started:
                        raise RuntimeError('E2B VM PID was reused during observation')
                    if not task['cgroup'].startswith('0::/e2b/' + child.name):
                        raise RuntimeError('E2B VM process escaped its observed cgroup')
                    assert_pinned({'cgroup': group, 'tasks': [task]}, self.node, self.cpus, swap=False)
                    observed_threads = threads(pid, before_numa_maps=validate)
                    assert_pinned({'cgroup': group, 'threads': observed_threads}, self.node, self.cpus, swap=False)
                    with vm_proc_read(PROC / str(pid) / 'stat', task):
                        if start_ticks(pid) != started:
                            raise RuntimeError('E2B VM PID was reused during observation')
                    # Append only after all thread reads finish. A disappearing
                    # task must not leave a successful row with missing threads.
                    row['tasks'].append(task)
                    row.setdefault('threads', []).extend(observed_threads)
                except CgroupChildDisappeared as error:
                    self._discard_deleted_child(child, identity, root_identity,
                                                'process-validation-' + error.observation['stage'], errno.ENOENT)
                    self.discarded_cgroups[-1]['process_observation'] = error.observation
                    incomplete = True
                    break
                except VMProcessReadGone as error:
                    self._discard_exited_process(error, pid, started, child, identity, root_identity)
                    incomplete = True
                    break
                except FileNotFoundError:
                    continue
            if incomplete:
                continue
            # Children default to swap.max=max, but the verified /e2b parent
            # cap of zero is the effective upper bound for every descendant.
            assert_pinned(row, self.node, self.cpus, swap=False)
            if row['cgroup']['memory.swap.current'] != '0':
                raise RuntimeError('E2B VM has existing swap despite the ancestor limit')
            try:
                current = child.lstat()
            except FileNotFoundError:
                self._discard_deleted_child(child, identity, root_identity, 'completed-sample-recheck', errno.ENOENT)
                continue
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise RuntimeError('E2B VM cgroup identity changed during observation')
            row['effective_swap_limit_source'] = '/e2b/memory.swap.max=0'
            for task in row['tasks']:
                token = child.name + ':' + str(task['pid']) + ':' + str(task['start_ticks'])
                self.rows.setdefault(token, dict(row, sampled_at=time.time()))

    def start(self):
        self.worker.start()
        if not self.ready.wait(timeout=10):
            self.errors.append('Observer thread placement readiness timed out')
            raise RuntimeError('E2B observer placement was not ready')
        if self.errors:
            raise RuntimeError('E2B observer placement setup failed')
        self.observer.check_main()

    def watch(self):
        phase = 'setup'
        try:
            if self.observer is None:
                raise RuntimeError('E2B observer placement admission is missing')
            self.observer.setup()
            self.ready.set()
            while not self.stop.is_set():
                phase = 'observer-before-sample'
                self.observer.check()
                phase = 'vm-sample'
                self.sample()
                phase = 'observer-after-sample'
                self.observer.check()
                self.stop.wait(.025)
        except BaseException as error:
            self.errors.append(type(error).__name__ + ': ' + str(error))
            if self.observer is not None:
                self.observer.receipt['error'] = error_record(error)
                self.observer.receipt['error_context'] = {'phase': phase,
                    'errno': getattr(error, 'errno', None), 'filename': getattr(error, 'filename', None),
                    'frames': [{'file': f.filename, 'line': f.lineno, 'function': f.name}
                               for f in traceback.extract_tb(error.__traceback__)]}
        finally:
            self.ready.set()

    def stop_worker(self):
        self.stop.set()
        if self.worker.ident is not None:
            self.worker.join(timeout=10)
        if self.worker.is_alive() and 'VM sampling did not terminate' not in self.errors:
            self.errors.append('VM sampling did not terminate')
        if self.observer is not None:
            try:
                self.observer.check_main()
            except BaseException as error:
                self.errors.append(type(error).__name__ + ': ' + str(error))
        return self.worker.is_alive()

    def evidence(self):
        return {'samples': list(self.rows.values()), 'errors': self.errors,
                'discarded_cgroups': self.discarded_cgroups, 'discarded_processes': self.discarded_processes,
                'observer': self.observer.receipt if self.observer is not None else None,
                'scope': 'Observed SDK VM cgroups and all observed process threads; constraints also apply between samples'}

    def finish(self, path):
        self.stop_worker()
        save(path, self.evidence())
        if self.errors:
            raise RuntimeError('E2B VM placement verification failed')

    def verify_ids(self, fanout_path):
        rows = json.loads(fanout_path.read_text())
        ids = {r['id'] for row in rows for r in row.get('cleanup', []) if r.get('resource') in ('source', 'child') and r.get('id')}
        ids.update(c['sandbox_id'] for row in rows for c in row.get('children', []) if c.get('sandbox_id'))
        observed = {row['cgroup']['path'].rsplit('/', 1)[1].split('-')[1] for row in self.rows.values()}
        if not ids or not ids <= observed:
            raise RuntimeError('Missing placement proof for a measured E2B source/child VM')
        return {'expected_sandbox_ids': sorted(ids), 'observed_sandbox_ids': sorted(observed)}


def restore_root(before):
    current = cgroup(VM_ROOT)
    if current['inode'] != before['inode'] or 'populated 0' not in current['cgroup.events'] or cgroup_processes(VM_ROOT):
        raise RuntimeError('E2B VM root identity changed or remains populated')
    for name in ('cpuset.mems', 'cpuset.cpus', 'memory.swap.max'):
        (VM_ROOT / name).write_text(before[name] + '\n')
    old = set(before['cgroup.subtree_control'].split())
    new = set((VM_ROOT / 'cgroup.subtree_control').read_text().split())
    changes = ['-' + name for name in new - old] + ['+' + name for name in old - new]
    if changes:
        (VM_ROOT / 'cgroup.subtree_control').write_text(' '.join(changes) + '\n')
    after = cgroup(VM_ROOT)
    for name in ('cpuset.mems', 'cpuset.cpus', 'cpuset.mems.effective', 'cpuset.cpus.effective', 'memory.swap.max', 'cgroup.subtree_control'):
        equal = (set(after[name].split()) == set(before[name].split())
                 if name == 'cgroup.subtree_control' else after[name] == before[name])
        if not equal:
            raise RuntimeError('E2B VM root did not restore: ' + name)
        if after[name] != before[name]:
            print('[restore-equivalence] ' + json.dumps({'check': name,
                'before': before[name], 'after': after[name], 'accepted': True}), flush=True)


def restore_stopped_unit_cgroups(before):
    """An empty systemd property need not clear an existing kernel cpuset."""
    for name, original in before['units'].items():
        text = output('systemctl', 'show', name, '-p', 'MainPID', '-p', 'ActiveState')
        state = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        if state.get('MainPID') != '0' or state.get('ActiveState') not in ('inactive', 'failed'):
            raise RuntimeError('E2B daemon was not stopped for restoration')
        path = CGROUP / 'system.slice' / name
        if path.exists():
            current = cgroup(path)
            if 'populated 0' not in current['cgroup.events'] or cgroup_processes(path):
                raise RuntimeError('Stopped E2B daemon cgroup remains populated')
            for key in ('cpuset.mems', 'cpuset.cpus', 'memory.swap.max'):
                (path / key).write_text(original['cgroup'][key] + '\n')


def assert_restored(before, restored):
    for name in UNITS:
        a, b = restored['units'][name], before['units'][name]
        if any(a[key] != b[key] for key in PROPERTIES) or a['process']['exe'] != b['process']['exe'] or a['binary_sha256'] != b['binary_sha256'] or a['dropins'] != b['dropins'] or set(a['process']['numa_policies']) != set(b['process']['numa_policies']):
            raise RuntimeError('Original E2B daemon placement/policy did not restore: ' + name)
        for key in ('cpuset.cpus', 'cpuset.mems', 'cpuset.cpus.effective', 'cpuset.mems.effective', 'memory.swap.max'):
            if a['cgroup'][key] != b['cgroup'][key]:
                raise RuntimeError('Original E2B daemon actual cgroup did not restore: ' + name + ':' + key)
        for task in a['tasks'] + a['threads']:
            if cpuset(task['cpus']) != cpuset(b['process']['cpus']) or cpuset(task['mems']) != cpuset(b['process']['mems']) or set(task['numa_policies']) != set(b['process']['numa_policies']):
                raise RuntimeError('Original E2B daemon actual task placement did not restore')
    for name in CONTAINERS:
        a, b = restored['containers'][name], before['containers'][name]
        if a['id'] != b['id'] or not identity_matches(a['process'], b['process']) or a['process']['cpus'] != b['process']['cpus'] or a['process']['mems'] != b['process']['mems']:
            raise RuntimeError('Original E2B container placement did not restore: ' + name)
        for key in ('cpuset.cpus.effective', 'cpuset.mems.effective'):
            if a['cgroup'][key] != b['cgroup'][key]:
                raise RuntimeError('Original E2B container actual cgroup did not restore: ' + name)
        old_tasks = {(task['pid'], task['start_ticks']): task for task in b['tasks'] + b['threads']}
        for task in a['tasks'] + a['threads']:
            original = old_tasks.get((task['pid'], task['start_ticks']), b['process'])
            if cpuset(task['cpus']) != cpuset(original['cpus']) or cpuset(task['mems']) != cpuset(original['mems']) or set(task['numa_policies']) != set(original['numa_policies']):
                raise RuntimeError('Original E2B container actual task placement did not restore: ' + name)


def wait_restored(config, before, *, readiness, timeout=90):
    end = time.monotonic() + timeout
    while True:
        try:
            restored = snapshot()
            assert_restored(before, restored)
            idle(config, timeout=1)
            if readiness is None or not readiness.poll(restored['units']['ae-e2b-api.service']['process']):
                raise RuntimeError('Original registered API local node has not become ready')
            restored['api_node_readiness'] = readiness.evidence()
            return restored
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            if time.monotonic() >= end:
                raise
            time.sleep(.2)


@contextmanager
def service_placement(config, out, *, fanout_path, working_storage=False, source_sha256=None):
    measurement = config.get('measurement', {})
    node, cpus = measurement.get('numa_node'), measurement.get('cpus')
    admission = require_admission(config, node, cpus)
    observer = observer_placement(node, cpus, admission)
    drops = {u: SYSTEMD_RUNTIME / (u + '.d') / DROP_NAME for u in UNITS}
    if any(path.exists() or path.is_symlink() for path in drops.values()):
        raise RuntimeError('An earlier E2B placement dropin exists')
    before = snapshot()
    idle(config)
    readiness = APINodeReadiness(before['units']['ae-e2b-api.service']['process'])
    storage = None
    if working_storage:
        from ae.scripts.e2b_working_storage import WorkingStorage
        storage = WorkingStorage(config, out.parent/'e2b-storage', node, cpus, source_sha256,
                                 root=ROOT, units=UNITS, vm_root=VM_ROOT)
        storage.admit(before)
    diagnostic = None
    diagnostic_request = os.environ.get('AE_E2B_UFFD_AGGREGATE_REQUEST')
    if diagnostic_request is not None:
        if storage is None:
            raise ValueError('Owned UFFD diagnostics require the original working storage')
        from ae.scripts.e2b_uffd_diagnostic import Session
        diagnostic = Session(diagnostic_request, root=ROOT, out=out,
            source_sha256=source_sha256, node=node, cpus=cpus, before=before, admission=admission)
    save(out / 'before.json', dict(before, admission=admission, api_node_readiness=readiness.before()))
    saved_controls = {}
    for name in UNITS:
        for prop in RESOURCE_PROPERTIES:
            path = SYSTEMD_CONTROL / (name + '.d') / ('50-' + prop + '.conf')
            if path.exists():
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise RuntimeError('E2B runtime resource property is not root trusted')
            saved_controls[path] = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
    changed, active, measurement_started, original_error = False, None, False, None
    owned_units = {name: row['process'] for name, row in before['units'].items()}
    proof = VMProof(node, cpus, observer=observer)
    save(GUARD, {'reason': 'E2B placement transaction in progress', 'evidence': str(out)})
    if diagnostic is not None:
        try:
            diagnostic.bind_guard()
        except BaseException as error:
            diagnostic.note_pending('guard-admission', error)
            save(out / 'original-error.json', {'error': error_record(error), 'phase': 'preparation'})
            save(out / 'transaction-result.json', {'original_error': error_record(error),
                'restoration_errors': [], 'placement_retained': True, 'services_changed': False})
            raise
    def save_recovery_guard(value):
        if diagnostic is not None:
            try:
                diagnostic.check_guard()
            except Exception as error:
                diagnostic.note_pending('guard-retained', error)
                return
        save(GUARD, value)
    try:
        # Mark before the first mutation so a partial restart still restores.
        changed = True
        stop_owned_units(owned_units)
        if storage is not None:
            storage.prepare_stopped()
        for name, path in drops.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            text = '[Service]\nCPUAffinity=\nCPUAffinity=' + cpus + '\nNUMAPolicy=bind\nNUMAMask=\nNUMAMask=' + str(node) + '\n'
            if storage is not None and name == 'ae-e2b-orchestrator.service':
                text += 'Environment="TMPDIR=' + str(storage.ram/'tmp') + '"\n'
            if diagnostic is not None and name == 'ae-e2b-orchestrator.service':
                text += diagnostic.dropin()
            path.write_text(text)
        run('systemctl', 'daemon-reload')
        for name in UNITS:
            run('systemctl', 'set-property', '--runtime', name, 'AllowedCPUs=' + cpus, 'AllowedMemoryNodes=' + str(node), 'MemorySwapMax=0')
        for name in CONTAINERS:
            actual = container(name)
            if actual['id'] != before['containers'][name]['id'] or not identity_matches(actual['process'], before['containers'][name]['process']):
                raise RuntimeError('E2B metadata container identity changed')
            run('docker', 'update', '--cpuset-cpus', cpus, '--cpuset-mems', str(node), actual['id'])
        current_root = cgroup(VM_ROOT)
        if current_root['inode'] != before['vm_root']['inode'] or 'populated 0' not in current_root['cgroup.events'] or cgroup_processes(VM_ROOT):
            raise RuntimeError('E2B VM root changed before placement')
        (VM_ROOT / 'cpuset.mems').write_text(str(node) + '\n')
        (VM_ROOT / 'cpuset.cpus').write_text(cpus + '\n')
        (VM_ROOT / 'memory.swap.max').write_text('0\n')
        if 'cpuset' not in before['vm_root']['cgroup.subtree_control'].split():
            (VM_ROOT / 'cgroup.subtree_control').write_text('+cpuset\n')
        for name in reversed(UNITS):
            run('systemctl', 'start', name)
            owned_units[name] = unit(name)['process']
            save(out / 'transaction-unit-identities.json', owned_units)
        readiness.bind(owned_units['ae-e2b-api.service'])
        active = wait_actual(config, before, node, cpus, readiness=readiness)
        if storage is not None:
            storage.verify_active(active['units']['ae-e2b-orchestrator.service'])
        save(out / 'actual.json', active)
        if diagnostic is not None:
            diagnostic.publish(active, storage)
        proof.start()
        measurement_started = True
        yield {'manifest': str(out / 'actual.json'), 'node': node, 'cpus': cpus,
               'scope': 'Registered E2B daemon cgroups, metadata containers, and /e2b VM root; not host-wide placement',
               **({'working_storage_manifest':str(storage.out/'verified.json')} if storage is not None else {})}
        proof.finish(out / 'vm-proof.json')
        save(out / 'vm-id-coverage.json', proof.verify_ids(fanout_path))
    except BaseException as error:
        original_error = error
        save(out / 'original-error.json', {'error': error_record(error),
             'phase': 'measurement' if measurement_started else 'preparation'})
        raise
    finally:
        observer_alive = proof.stop_worker()
        save(out / 'vm-proof.json', proof.evidence())
        # Finish the original service/storage restoration even if an observer
        # failed to join, then retain a real recovery guard and both errors.
        errors = ([error_record(RuntimeError('VM observer remains alive after owned join'))]
                  if observer_alive else [])
        try:
            if measurement_started:
                idle(config)
            else:
                group = cgroup(VM_ROOT)
                if group['inode'] != before['vm_root']['inode'] or 'populated 0' not in group['cgroup.events'] or cgroup_processes(VM_ROOT):
                    raise RuntimeError('E2B VM root changed during preparation')
            check_owned_units(owned_units)
            for name in CONTAINERS:
                current = container(name)
                if current['id'] != before['containers'][name]['id'] or not identity_matches(current['process'], before['containers'][name]['process']):
                    raise RuntimeError('E2B metadata container identity changed during measurement')
        except Exception as error:
            failures = [error_record(error)]
            save_recovery_guard({'reason': 'E2B resources require recovery; placement retained', 'evidence': str(out), 'original_error': error_record(original_error), 'restoration_errors': failures})
            save(out / 'transaction-result.json', {'original_error': error_record(original_error), 'restoration_errors': failures, 'placement_retained': True})
            raise RuntimeError('E2B resources require recovery; placement retained') from (original_error or error)
        restoration_readiness = None
        if changed:
            try:
                stop_owned_units(owned_units)
            except Exception as error:
                failures = [error_record(error)]
                save_recovery_guard({'reason': 'E2B daemon stop failed; resources retained', 'evidence': str(out), 'original_error': error_record(original_error), 'restoration_errors': failures})
                save(out / 'transaction-result.json', {'original_error': error_record(original_error), 'restoration_errors': failures, 'placement_retained': True})
                raise RuntimeError('E2B daemon stop failed; shared resource restoration was not attempted') from (original_error or error)
            if diagnostic is not None:
                try:
                    diagnostic.seal()
                except BaseException as error:
                    diagnostic.note_pending('seal', error)
            if storage is not None:
                try:
                    storage.restore_stopped()
                except BaseException as error:
                    failures = [error_record(error)]
                    save_recovery_guard({'reason': 'E2B RAM storage restoration failed; resources retained',
                        'evidence':str(out),'original_error':error_record(original_error),'restoration_errors':failures})
                    save(out/'transaction-result.json', {'original_error':error_record(original_error),
                        'restoration_errors':failures,'placement_retained':True})
                    raise RuntimeError('E2B RAM storage restoration failed; original services were not restarted') from (original_error or error)
            def attempt(operation):
                try:
                    operation()
                except Exception as error:
                    errors.append(error_record(error))
            for path in drops.values():
                attempt(lambda p=path: p.unlink(missing_ok=True))
            for name in UNITS:
                original = before['units'][name]
                attempt(lambda u=name, b=original: run('systemctl', 'set-property', '--runtime', u, *[key + '=' + b[key] for key in RESOURCE_PROPERTIES]))
            for path, saved in saved_controls.items():
                if saved is None:
                    attempt(lambda p=path: p.unlink(missing_ok=True))
                else:
                    attempt(lambda p=path, b=saved: (p.write_bytes(b[0]), p.chmod(b[1])))
            attempt(lambda: run('systemctl', 'daemon-reload'))
            attempt(lambda: restore_stopped_unit_cgroups(before))
            for name in CONTAINERS:
                b = before['containers'][name]
                # Docker ignores empty update values; restore the original
                # effective inherited mask explicitly, as in the Cube context.
                attempt(lambda b=b: run('docker', 'update', '--cpuset-cpus', b['cpus'] or b['process']['cpus'], '--cpuset-mems', b['mems'] or b['process']['mems'], b['id']))
            attempt(lambda: restore_root(before['vm_root']))
            try:
                restoration_readiness = readiness.next_restart()
            except Exception as error:
                errors.append(error_record(error))
            attempt(lambda: run('systemctl', 'start', *reversed(UNITS)))
        restored = None
        try:
            restored = wait_restored(config, before, readiness=restoration_readiness)
            if storage is not None:
                storage.verify_restored(restored['units']['ae-e2b-orchestrator.service'])
            if diagnostic is not None:
                diagnostic.verify_restored(restored)
        except Exception as error:
            errors.append(error_record(error))
        guard_changed = False
        if diagnostic is not None:
            try:
                diagnostic.check_guard()
            except Exception as error:
                guard_changed = True
                errors.append(error_record(error))
        save(out / 'after.json', {'restored': restored, 'errors': errors,
             'container_empty_masks': 'Original empty Docker masks are restored as their explicitly recorded effective masks'})
        save(out / 'transaction-result.json', {'original_error': error_record(original_error), 'restoration_errors': errors, 'placement_retained': bool(errors)})
        if errors:
            if not guard_changed:
                save(GUARD, {'reason': 'E2B service restoration failed', 'evidence': str(out), 'original_error': error_record(original_error), 'restoration_errors': errors})
            raise RuntimeError('E2B service restoration failed: ' + '; '.join(row['type'] + ': ' + row['message'] for row in errors)) from original_error
        GUARD.unlink()
