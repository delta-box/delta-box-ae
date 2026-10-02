"""Thread-only placement of the VM observer inside an admitted hosted CPU unit.

The workload masks come from the existing hosted layouts. Nothing in this
module changes a cgroup, a service, another task, or existing memory pages.
"""
import ctypes
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading

PROC = Path('/proc')
CGROUP = Path('/sys/fs/cgroup')


def mask(text):
    result = set()
    for item in str(text).replace(' ', ',').split(','):
        if not item:
            continue
        if not re.fullmatch(r'\d+(?:-\d+)?', item):
            raise ValueError('Invalid observer placement mask')
        first, _, last = item.partition('-')
        if int(last or first) < int(first):
            raise ValueError('Invalid observer placement range')
        result.update(range(int(first), int(last or first) + 1))
    return result


def task_identity(tid):
    base = PROC / str(tid)
    before = (base / 'stat').read_text().rsplit(')', 1)[1].split()
    status = dict(line.split(':', 1) for line in (base / 'status').read_text().splitlines() if ':' in line)
    member = (base / 'cgroup').read_text().strip()
    after = (base / 'stat').read_text().rsplit(')', 1)[1].split()
    if before[19] != after[19] or before[1] != after[1]:
        raise RuntimeError('Observer admission task identity changed')
    return {'tid': tid, 'tgid': int(status['Tgid']), 'parent_pid': int(after[1]), 'uids': list(map(int, status['Uid'].split())),
            'start_ticks': int(after[19]), 'cgroup': member}


def ancestors():
    result, pid = {}, os.getpid()
    while pid > 0 and pid not in result:
        row = task_identity(pid)
        result[pid] = row
        pid = row['parent_pid']
    return result


def service_state(unit):
    properties = ('Id', 'ControlGroup', 'MainPID', 'ActiveState', 'SubState', 'Transient',
                  'WorkingDirectory', 'AllowedCPUs', 'AllowedMemoryNodes', 'CPUAffinity',
                  'NUMAPolicy', 'NUMAMask', 'MemorySwapMax')
    text = subprocess.check_output(['/usr/bin/systemctl', 'show', unit,
        '--property=' + ','.join(properties)], text=True, timeout=10)
    rows = text.splitlines()
    result = dict(line.split('=', 1) for line in rows if '=' in line)
    if len(result) != len(rows) or set(result) != set(properties):
        raise RuntimeError('Incomplete or ambiguous hosted CPU service identity')
    return result


def group_identity(path):
    before = path.lstat()
    if not stat.S_ISDIR(before.st_mode) or before.st_uid != 0 or before.st_mode & 0o022:
        raise RuntimeError('Untrusted observer cgroup')
    result = {'path': str(path), 'device': before.st_dev, 'inode': before.st_ino}
    for key in ('cpuset.cpus.effective', 'cpuset.mems.effective', 'memory.swap.max'):
        result[key] = (path / key).read_text().strip()
    after = path.lstat()
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise RuntimeError('Observer cgroup changed while reading admission')
    return result


def admit(root, node, cpus, leases):
    # These are the same authoritative tables used by the protected launcher
    # and lane scheduler; no caller environment or arbitrary mask is accepted.
    from ae.scripts.hosted_launcher import CPU_LAYOUT_PROPERTIES, CPU_UNIT_PATTERN
    from ae.scripts.run_cpu_parallel import CPU_LAYOUTS
    if os.geteuid() != 0 or set(leases) != {'numa_lease_owner', 'results_lease_owner'}:
        raise ValueError('Observer requires the existing hosted lease admission')
    chain = ancestors()
    leader = chain[os.getpid()]
    match = re.fullmatch(r'0::(/system.slice/(' + CPU_UNIT_PATTERN + r'))', leader['cgroup'])
    if match is None:
        raise RuntimeError('Observer is not in an owned hosted CPU service')
    relative, unit = match.group(1), match.group(2)
    state = service_state(unit)
    main = int(state['MainPID'])
    if (state['Id'] != unit or state['ControlGroup'] != relative or
            state['ActiveState'] != 'active' or state['SubState'] != 'running' or
            state['Transient'] != 'yes' or state['WorkingDirectory'] != str(root) or
            state['MemorySwapMax'] != '0' or main not in chain):
        raise RuntimeError('Observer hosted CPU service is not an active ancestor')
    pids = {main, os.getpid(), *leases.values()}
    if any(type(pid) is not int or pid not in chain or
           (chain[pid]['cgroup'] != leader['cgroup'] or chain[pid]['uids'] != [0] * 4) for pid in pids):
        raise RuntimeError('Observer lease owners do not belong to this CPU service ancestry')
    group = group_identity(CGROUP / relative.lstrip('/'))
    matches = []
    for layout, properties in CPU_LAYOUT_PROPERTIES.items():
        if (node in CPU_LAYOUTS[layout] and mask(cpus) == mask(CPU_LAYOUTS[layout][node]) and
                all(mask(state[key]) == mask(properties[key]) for key in
                    ('AllowedCPUs', 'AllowedMemoryNodes', 'CPUAffinity', 'NUMAMask')) and
                state['NUMAPolicy'] == properties['NUMAPolicy'] == 'bind' and
                mask(group['cpuset.cpus.effective']) == mask(properties['AllowedCPUs']) and
                mask(group['cpuset.mems.effective']) == mask(properties['AllowedMemoryNodes'])):
            matches.append(layout)
    if len(matches) != 1 or group['memory.swap.max'] != '0':
        raise RuntimeError('Observer requires one exact hosted CPU layout and no-swap cgroup')
    layout = matches[0]
    props = CPU_LAYOUT_PROPERTIES[layout]
    controller_cpus, controller_mems = mask(props['CPUAffinity']), mask(props['NUMAMask'])
    if (len(controller_mems) != 1 or not controller_cpus <= mask(group['cpuset.cpus.effective']) or
            not controller_mems <= mask(group['cpuset.mems.effective'])):
        raise RuntimeError('Controller placement is outside the admitted cgroup')
    result = {'layout': layout, 'unit': unit, 'service': state, 'group': group,
              'ancestors': {str(pid): chain[pid] for pid in sorted(pids)},
              'leader': leader, 'controller_cpus': sorted(controller_cpus),
              'controller_node': next(iter(controller_mems)),
              'workload_cpus': sorted(mask(cpus)), 'workload_node': node}
    recheck(result, service=True)
    return result


def recheck(admission, *, service=False):
    if group_identity(Path(admission['group']['path'])) != admission['group']:
        raise RuntimeError('Observer admitted cgroup identity or limits changed')
    for row in admission['ancestors'].values():
        if task_identity(row['tid']) != row:
            raise RuntimeError('Observer admitted task or lease ancestor changed')
    if service and service_state(admission['unit']) != admission['service']:
        raise RuntimeError('Observer admitted hosted CPU service changed')


class MemoryPolicy:
    """libnuma's Linux syscall wrappers, always for the calling native thread.

    flags=0 and addr=NULL read the default thread policy, not an address or its
    cgroup upper bound. No migration/mbind flags or calls are used.
    """
    MAXNODE = 4096
    MPOL_BIND = 2

    def __init__(self):
        if sys.platform != 'linux':
            raise RuntimeError('Observer memory policy requires Linux')
        self.lib = ctypes.CDLL('libnuma.so.1', use_errno=True)
        self.words = self.MAXNODE // (ctypes.sizeof(ctypes.c_ulong) * 8)
        self.array = ctypes.c_ulong * self.words
        self.lib.get_mempolicy.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
                                           ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong]
        self.lib.get_mempolicy.restype = ctypes.c_int
        self.lib.set_mempolicy.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong]
        self.lib.set_mempolicy.restype = ctypes.c_int

    @staticmethod
    def checked(code):
        if code != 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))

    def get(self):
        mode, nodes = ctypes.c_int(), self.array()
        self.checked(self.lib.get_mempolicy(ctypes.byref(mode), nodes, self.MAXNODE, None, 0))
        bits = ctypes.sizeof(ctypes.c_ulong) * 8
        return {'mode': mode.value, 'nodes': [word * bits + bit for word, value in enumerate(nodes)
                if value for bit in range(bits) if value & (1 << bit)]}

    def bind(self, node):
        if type(node) is not int or not 0 <= node < self.MAXNODE:
            raise ValueError('Observer memory node is out of range')
        nodes, bits = self.array(), ctypes.sizeof(ctypes.c_ulong) * 8
        nodes[node // bits] |= 1 << (node % bits)
        self.checked(self.lib.set_mempolicy(self.MPOL_BIND, nodes, self.MAXNODE))


class ThreadPlacement:
    def __init__(self, admission):
        self.admission = admission
        self.policy = MemoryPolicy()
        self.leader_affinity = sorted(os.sched_getaffinity(0))
        self.leader_policy = self.policy.get()
        if (threading.get_native_id() != os.getpid() or
                self.leader_affinity != admission['workload_cpus'] or
                self.leader_policy != {'mode': MemoryPolicy.MPOL_BIND, 'nodes': [admission['workload_node']]}):
            raise RuntimeError('Observer preparation must run in the admitted workload main thread')
        self.receipt = {'admission': admission, 'main_before': {
            'identity': admission['leader'], 'cpus': self.leader_affinity, 'memory_policy': self.leader_policy},
            'scope': 'Observer native thread only; existing shared pages are not migrated; GIL and target mm contention remain possible'}

    def setup(self):
        tid = threading.get_native_id()
        row = task_identity(tid)
        self.receipt['setup_task'] = row
        if tid == os.getpid() or row['tgid'] != os.getpid() or row['cgroup'] != self.admission['leader']['cgroup']:
            raise RuntimeError('Observer setup is not its own hosted native thread')
        recheck(self.admission, service=True)
        os.sched_setaffinity(0, self.admission['controller_cpus'])
        self.policy.bind(self.admission['controller_node'])
        self.identity = row
        self.check()
        self.receipt['ready'] = dict(self.receipt['latest'])

    def state(self):
        return {'identity': task_identity(threading.get_native_id()),
                'cpus': sorted(os.sched_getaffinity(0)), 'memory_policy': self.policy.get()}

    def check(self):
        actual = self.state()
        if (actual['identity'] != self.identity or actual['cpus'] != self.admission['controller_cpus'] or
                actual['memory_policy'] != {'mode': MemoryPolicy.MPOL_BIND, 'nodes': [self.admission['controller_node']]}):
            raise RuntimeError('Observer native thread identity or placement changed')
        if group_identity(Path(self.admission['group']['path'])) != self.admission['group']:
            raise RuntimeError('Observer cgroup changed at scan boundary')
        self.receipt['latest'] = actual

    def check_main(self):
        if threading.get_native_id() != os.getpid():
            raise RuntimeError('Observer main-thread verification called from another task')
        actual = {'identity': task_identity(os.getpid()), 'cpus': sorted(os.sched_getaffinity(0)),
                  'memory_policy': self.policy.get()}
        self.receipt['main_after'] = actual
        if actual != self.receipt['main_before']:
            raise RuntimeError('Observer setup changed the workload main thread')
