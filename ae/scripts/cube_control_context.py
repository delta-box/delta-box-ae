"""Scoped Cube control-plane placement and verified private metadata storage."""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
from ae.scripts.cube_memory_context import sandboxes
from ae.repro.result_storage import run_lock
from ae.vendor.finalbench.fc_diff_dm.fc_capacity import node_available, GIB


def metadata_enabled(config):
    value = config.get('cube', {}).get('manage_metadata_memory', False)
    if type(value) is not bool:
        raise ValueError('cube.manage_metadata_memory must be a boolean')
    if value:
        cube = config['cube']
        if cube.get('manage_memory_service') is not True or config.get('baseline_storage') != 'tmpfs':
            raise ValueError('Managed Cube metadata requires the managed RAM data plane')
        url = os.path.expandvars(str(cube.get('api_url', ''))).rstrip('/')
        if url not in ('http://127.0.0.1:3000', 'http://localhost:3000'):
            raise ValueError('Managed Cube metadata requires the local managed API')
    return value


def require_pinned_parent(node, cpus):
    """The AE entry must own this node before any shared service is changed."""
    from ae.runners.cube_memory import cpuset
    identity = json.loads(os.environ.get('AE_MEASUREMENT_IDENTITY', '{}'))
    if identity.get('node') != node or cpuset(str(identity.get('cpus', ''))) != cpuset(cpus):
        raise ValueError('Use the pinned AE entry for managed Cube metadata')
    if set(os.sched_getaffinity(0)) != cpuset(cpus):
        raise ValueError('Managed Cube caller CPU binding differs from its plan')
    policy = dict(line.split(':', 1) for line in output('numactl', '--show').splitlines() if ':' in line)
    if policy.get('policy', '').strip() != 'bind' or policy.get('membind', '').split() != [str(node)]:
        raise ValueError('Managed Cube caller memory policy differs from its plan')
    ancestors = set()
    pid = os.getpid()
    while pid > 0 and pid not in ancestors:
        ancestors.add(pid)
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        pid = int(fields[1])
    lock = Path(f'/run/lock/deltabox-numa-{node}.lock').stat()
    wanted = (os.major(lock.st_dev), os.minor(lock.st_dev), lock.st_ino)
    for line in Path('/proc/locks').read_text().splitlines():
        fields = line.split()
        if len(fields) < 8 or fields[1:4] != ['FLOCK','ADVISORY','WRITE']:
            continue
        dev = fields[5].split(':')
        actual = (int(dev[0],16), int(dev[1],16), int(dev[2]))
        if actual == wanted and int(fields[4]) in ancestors:
            return int(fields[4])
    raise ValueError('No ancestor holds the required NUMA measurement lease')

UNITS = ['cube-sandbox-' + n + '.service' for n in
         ('cube-api', 'cubemaster', 'cubelet', 'mysql', 'redis', 'cube-proxy', 'coredns', 'network-agent')]


CONTAINERS = ['cube-sandbox-mysql', 'cube-sandbox-redis', 'cube-proxy', 'cube-proxy-coredns']


FRONT = ['cube-sandbox-cube-api.service', 'cube-sandbox-cubemaster.service']


MYSQL_UNIT = 'cube-sandbox-mysql.service'


MYSQL = 'cube-sandbox-mysql'


MYSQL_DROP = Path('/run/systemd/system/cube-sandbox-mysql.service.d/zzzz-deltabox-preserve-container.conf')


UNIT_PROPERTIES = ('AllowedCPUs', 'AllowedMemoryNodes', 'CPUAffinity', 'NUMAPolicy', 'NUMAMask')


WEBUI = 'cube-sandbox-webui.service'


WEBUI_DROP = Path('/run/systemd/system/cube-sandbox-webui.service.d/zzzz-deltabox-quiesce.conf')


def run(*cmd, **kw):
    return subprocess.run(list(map(str, cmd)), check=True, **kw)


def output(*cmd):
    return subprocess.check_output(list(map(str, cmd)), text=True).strip()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + '\n')


def inspect(name):
    j = json.loads(output('docker', 'inspect', name))[0]
    effective = {}
    if j['State']['Running']:
        status = Path(f"/proc/{j['State']['Pid']}/status").read_text().splitlines()
        for key, field in [('effective_cpus', 'Cpus_allowed_list:'), ('effective_mems', 'Mems_allowed_list:')]:
            effective[key] = next(l.split(':', 1)[1].strip() for l in status if l.startswith(field))
    return {'id': j['Id'], 'pid': j['State']['Pid'], 'running': j['State']['Running'],
            'started_at': j['State']['StartedAt'],
            'finished_at': j['State']['FinishedAt'],
            'cpus': j['HostConfig']['CpusetCpus'], 'mems': j['HostConfig']['CpusetMems'],
            'mounts': j['Mounts'], **effective}


def restore_masks(before):
    # Docker ignores empty strings in an update. Explicitly restore the actual
    # inherited masks recorded before the run, rather than leaving the test pin.
    return (before['cpus'] or before['effective_cpus'], before['mems'] or before['effective_mems'])


def variables():
    sql = "SHOW GLOBAL VARIABLES WHERE Variable_name IN ('innodb_flush_log_at_trx_commit','sync_binlog','log_bin');"
    return output('docker', 'exec', MYSQL, 'sh', '-c',
        'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot -N -B -e "$1"', 'read-vars', sql)


def idle(timeout=45):
    end = time.monotonic() + timeout
    while True:
        pids = set()
        for path in Path('/sys/fs/cgroup/cube_sandbox').rglob('cgroup.procs'):
            try:
                pids.update(path.read_text().splitlines())
            except FileNotFoundError:
                pass
        cubelet = output('systemctl', 'show', 'cube-sandbox-cubelet.service', '-p', 'MainPID', '--value')
        pids.discard(cubelet)
        if not sandboxes() and not pids:
            return
        if time.monotonic() >= end:
            raise RuntimeError('Cube inventory did not return to empty; preserving evidence')
        time.sleep(.5)


def service_start():
    run('systemctl', 'start', MYSQL_UNIT)
    end = time.monotonic() + 60
    while True:
        try:
            variables()
            break
        except subprocess.CalledProcessError:
            if time.monotonic() >= end:
                raise
            time.sleep(.5)
    run('systemctl', 'start', *reversed(FRONT))
    end = time.monotonic() + 60
    while True:
        try:
            idle(timeout=1)
            return
        except Exception:
            if time.monotonic() >= end:
                raise
            time.sleep(.5)


@contextmanager
def quiesce_webui(out, recovery_guard):
    """Its restart transaction otherwise pulls API -> Master -> MySQL back up."""
    if WEBUI_DROP.exists():
        raise RuntimeError('An earlier Cube UI quiesce override exists')
    before = {k: output('systemctl', 'show', WEBUI, '-p', k, '--value')
              for k in ('ActiveState', 'SubState', 'Restart')}
    save(out/'webui-before.json', before)
    changed = False
    try:
        WEBUI_DROP.parent.mkdir(parents=True, exist_ok=True)
        changed = True
        WEBUI_DROP.write_text('[Service]\nRestart=no\n')
        run('systemctl', 'daemon-reload')
        run('systemctl', 'stop', WEBUI)
        stopped_ui = {k: output('systemctl','show',WEBUI,'-p',k,'--value')
                      for k in ('ActiveState','SubState','MainPID','ControlPID','Job','Restart')}
        save(out/'webui-quiesced.json', stopped_ui)
        assert stopped_ui['ActiveState'] in ('inactive','failed'), stopped_ui
        assert stopped_ui['MainPID'] == stopped_ui['ControlPID'] == '0', stopped_ui
        assert stopped_ui['Job'] in ('','0') and stopped_ui['Restart']=='no', stopped_ui
        yield
    finally:
        if recovery_guard.exists():
            save(out/'webui-retained.json', {'guard': str(recovery_guard)})
            raise RuntimeError('Cube UI remains quiesced until resource recovery')
        save(recovery_guard, {'reason': 'Cube UI restoration in progress'})
        if changed:
            WEBUI_DROP.unlink(missing_ok=True)
            run('systemctl', 'daemon-reload')
            if before['ActiveState'] in ('active', 'activating', 'reloading'):
                run('systemctl', 'start', '--no-block', WEBUI)
            assert output('systemctl', 'show', WEBUI, '-p', 'Restart', '--value') == before['Restart']
            save(out/'webui-restored.json', {'original': before,
                 'restart_policy': output('systemctl', 'show', WEBUI, '-p', 'Restart', '--value'),
                 'prior_start_intent_restored': before['ActiveState'] in ('active','activating','reloading')})
        recovery_guard.unlink()


@contextmanager
def mysql_launcher(out, recovery_guard):
    """Keep the existing, already-pinned container during the two treatments."""
    drop = MYSQL_DROP
    if drop.exists():
        raise RuntimeError('An earlier Cube database operation has a service override')
    original = inspect(MYSQL)
    before_start = output('systemctl', 'show', MYSQL_UNIT, '-p', 'ExecMainStartTimestampMonotonic', '--value')
    if output('systemctl', 'show', MYSQL_UNIT, '-p', 'Type', '--value') != 'simple':
        raise RuntimeError('Unexpected Cube MySQL service type')
    save(out / 'mysql-launcher-before.json', original)
    changed = False
    try:
        drop.parent.mkdir(parents=True, exist_ok=True)
        changed = True
        drop.write_text('[Service]\nExecStart=\nExecStart=/usr/bin/docker start --attach ' + original['id'] +
                        '\nExecStop=\nExecStop=/usr/bin/docker stop --time 30 ' + original['id'] + '\n')
        run('systemctl', 'daemon-reload')
        yield
    finally:
        if recovery_guard.exists():
            save(out / 'mysql-launcher-retained.json', {'guard': str(recovery_guard), 'override': str(drop)})
            raise RuntimeError('Database launcher retained for resource recovery')
        save(recovery_guard, {'reason': 'MySQL supervisor restoration in progress'})
        if changed:
            drop.unlink(missing_ok=True)
            run('systemctl', 'daemon-reload')
            after_start = output('systemctl', 'show', MYSQL_UNIT, '-p', 'ExecMainStartTimestampMonotonic', '--value')
            if after_start != before_start:
                # Restore the original supervisor as well as its original
                # volume. The original script may recreate its container.
                errors = []
                for operation in [lambda: run('systemctl', 'stop', *FRONT),
                                  lambda: run('systemctl', 'restart', MYSQL_UNIT),
                                  service_start,
                                  lambda: run('docker', 'update', '--cpuset-cpus', restore_masks(original)[0],
                                      '--cpuset-mems', restore_masks(original)[1], MYSQL, stdout=subprocess.DEVNULL)]:
                    try:
                        operation()
                    except Exception as exc:
                        errors.append(str(exc))
                if errors:
                    save(recovery_guard, {'reason': 'Original MySQL supervisor restoration failed', 'errors': errors})
                    raise RuntimeError('; '.join(errors))
            restored = inspect(MYSQL)
            assert (restored['effective_cpus'], restored['effective_mems']) == (original['effective_cpus'], original['effective_mems'])
            save(out / 'mysql-launcher-restored.json', {'override_removed': not drop.exists(), 'container': restored})
        recovery_guard.unlink()


def service_cgroup_masks(unit):
    relative = Path(output('systemctl', 'show', unit, '-p', 'ControlGroup', '--value'))
    if not relative.is_absolute() or '..' in relative.parts or relative.name != unit:
        raise RuntimeError('Unexpected Cube service cgroup: ' + unit)
    group = Path('/sys/fs/cgroup') / str(relative).lstrip('/')
    return {'path': str(group), **{name: (group / name).read_text().strip()
        for name in ('cpuset.cpus', 'cpuset.mems', 'cpuset.cpus.effective', 'cpuset.mems.effective')}}


def restore_service_cgroup_masks(unit, expected):
    actual = service_cgroup_masks(unit)
    if actual['path'] != expected['path']:
        raise RuntimeError('Cube service cgroup identity changed: ' + unit)
    group = Path(actual['path'])
    # An empty systemd resource property does not clear a previously written
    # kernel cpuset. Restore its exact prior raw masks, then check effective
    # masks rather than treating the declarative property as proof.
    for name in ('cpuset.mems', 'cpuset.cpus'):
        (group / name).write_text(expected[name] + '\n')
    restored = service_cgroup_masks(unit)
    if restored != expected:
        raise RuntimeError('Cube service effective cgroup masks did not restore: ' + unit)
    return restored


@contextmanager
def placement(node, cpus, out, recovery_guard=None):
    before = {'units': {}, 'containers': {}, 'unit_cgroups': {}}
    changed_units, changed_containers, changed_dropins = [], [], []
    cg = Path('/sys/fs/cgroup/cube_sandbox')
    before['sandbox_cgroup'] = {n: (cg / n).read_text().strip() for n in ('cpuset.cpus', 'cpuset.mems')}
    for unit in UNITS:
        if output('systemctl', 'is-active', unit) != 'active':
            raise RuntimeError('Cube service not active: ' + unit)
        before['units'][unit] = {n: output('systemctl', 'show', unit, '-p', n, '--value')
                                 for n in UNIT_PROPERTIES}
        before['unit_cgroups'][unit] = service_cgroup_masks(unit)
    for name in CONTAINERS:
        before['containers'][name] = inspect(name)
        if not before['containers'][name]['running']:
            raise RuntimeError('Cube container not running: ' + name)
    save(out / 'placement-before.json', before)
    try:
        selected = [u for u in UNITS if u != 'cube-sandbox-cubelet.service']
        drops = {u: Path('/run/systemd/system')/(u+'.d')/'zzzz-deltabox-measurement-placement.conf'
                 for u in selected}
        if any(p.exists() for p in drops.values()):
            raise RuntimeError('An earlier Cube control-plane placement override exists')
        changed_units.extend(selected)
        for unit, drop in drops.items():
            drop.parent.mkdir(parents=True, exist_ok=True)
            changed_dropins.append(drop)
            drop.write_text('[Service]\nAllowedCPUs=\nAllowedCPUs='+cpus+'\nAllowedMemoryNodes=\nAllowedMemoryNodes='+str(node)+
                            '\nCPUAffinity=\nCPUAffinity='+cpus+'\nNUMAPolicy=bind\nNUMAMask=\nNUMAMask='+str(node)+'\n')
        run('systemctl', 'daemon-reload')
        for unit in UNITS:
            if unit == 'cube-sandbox-cubelet.service':
                # memory_service owns Cubelet's binding and must observe its
                # original unit policy for restoration of its numactl command.
                continue
            run('systemctl', 'set-property', '--runtime', unit,
                'AllowedCPUs=' + cpus, 'AllowedMemoryNodes=' + str(node))
        for name in CONTAINERS:
            changed_containers.append(name)
            run('docker', 'update', '--cpuset-cpus', cpus, '--cpuset-mems', str(node), name,
                stdout=subprocess.DEVNULL)
        yield
    finally:
        if recovery_guard is not None and recovery_guard.exists():
            save(out / 'placement-retained.json', {'reason': 'Unresolved owned Cube resources',
                                                 'guard': str(recovery_guard)})
            raise RuntimeError('Cube placement retained for resource recovery')
        if recovery_guard is not None:
            save(recovery_guard, {'reason': 'Cube placement restoration in progress'})
        errors = []
        for drop in reversed(changed_dropins):
            try:
                drop.unlink(missing_ok=True)
            except Exception as exc:
                errors.append(str(exc))
        if changed_dropins:
            try:
                run('systemctl', 'daemon-reload')
            except Exception as exc:
                errors.append(str(exc))
        for name in reversed(changed_containers):
            try:
                b = before['containers'][name]
                cpus_before, mems_before = restore_masks(b)
                run('docker', 'update', '--cpuset-cpus', cpus_before, '--cpuset-mems', mems_before, name,
                    stdout=subprocess.DEVNULL)
            except Exception as exc:
                errors.append(str(exc))
        for unit in reversed(changed_units):
            try:
                b = before['units'][unit]
                run('systemctl', 'set-property', '--runtime', unit,
                    'AllowedCPUs=' + b['AllowedCPUs'], 'AllowedMemoryNodes=' + b['AllowedMemoryNodes'])
                restore_service_cgroup_masks(unit, before['unit_cgroups'][unit])
            except Exception as exc:
                errors.append(str(exc))
        for name in ('cpuset.mems', 'cpuset.cpus'):
            try:
                (cg / name).write_text(before['sandbox_cgroup'][name])
            except Exception as exc:
                errors.append(str(exc))
        restored = {'units': {u: {k: output('systemctl', 'show', u, '-p', k, '--value')
                         for k in UNIT_PROPERTIES} for u in changed_units},
                    'unit_cgroups': {u: service_cgroup_masks(u) for u in changed_units},
                    'containers': {n: inspect(n) for n in changed_containers}}
        for unit, values in restored['units'].items():
            if values != before['units'][unit]:
                errors.append('Placement did not restore: ' + unit)
        for unit, values in restored['unit_cgroups'].items():
            if values != before['unit_cgroups'][unit]:
                errors.append('Effective cgroup masks did not restore: ' + unit)
        for name, values in restored['containers'].items():
            if any(values[k] != before['containers'][name][k] for k in ('effective_cpus', 'effective_mems')):
                errors.append('Placement did not restore: ' + name)
        save(out / 'placement-restored.json', dict(restored, errors=errors))
        if errors:
            raise RuntimeError('Cube placement restoration failed: ' + '; '.join(errors))
        if recovery_guard is not None:
            recovery_guard.unlink()


def copy_verified(source, target, manifest, quiescent=None):
    records = []
    total = 0
    for directory, dirs, files in os.walk(source):
        src = Path(directory)
        if any((src / name).is_symlink() for name in dirs):
            raise RuntimeError('Unexpected symlink directory in stopped database')
        dest = target / src.relative_to(source)
        dest.mkdir(parents=True, exist_ok=True)
        for name in files:
            if quiescent is not None:
                quiescent()
            a, b = src / name, dest / name
            info = a.lstat()
            if stat.S_ISLNK(info.st_mode):
                b.symlink_to(os.readlink(a))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeError('Unexpected live database entry: ' + str(a))
            digest = hashlib.sha256()
            with a.open('rb') as read, b.open('wb') as write:
                for block in iter(lambda: read.read(4 * 1024**2), b''):
                    digest.update(block)
                    write.write(block)
                    total += len(block)
            copied = hashlib.sha256()
            with b.open('rb') as read:
                for block in iter(lambda: read.read(4 * 1024**2), b''):
                    copied.update(block)
            assert copied.digest() == digest.digest(), a
            after = a.stat()
            assert (after.st_size, after.st_mtime_ns) == (info.st_size, info.st_mtime_ns), a
            shutil.copystat(a, b)
            os.chown(b, info.st_uid, info.st_gid)
            records.append({'path': str(a.relative_to(source)), 'bytes': info.st_size,
                            'sha256': digest.hexdigest()})
            save(manifest.with_name('copy-progress.json'), {'copied_bytes': total, 'files': len(records)})
        shutil.copystat(src, dest)
        st = src.stat()
        os.chown(dest, st.st_uid, st.st_gid)
    save(manifest, {'files': records, 'bytes': total, 'verified': True})
    if quiescent is not None:
        quiescent()


@contextmanager
def ram_database(node, out, recovery_guard=None, *, size_gib=13):
    before = inspect(MYSQL)
    source = Path(next(m['Source'] for m in before['mounts'] if m['Destination'] == '/var/lib/mysql'))
    if os.path.ismount(source):
        raise RuntimeError('Unexpected pre-existing database bind mount')
    if node_available(node) < (size_gib + 2) * GIB:
        raise RuntimeError('Insufficient selected-node memory for private database copy')
    before_vars = variables()
    out.mkdir(parents=True, exist_ok=False)
    ram = out / 'ram'
    ram.mkdir()
    mounted = bound = stopped = front_stopped = False
    try:
        idle()
        front_stopped = True
        run('systemctl', 'stop', *FRONT)
        stopped = True
        run('systemctl', 'stop', MYSQL_UNIT)
        stopped_state = inspect(MYSQL)
        assert not stopped_state['running']
        def quiescent():
            current = inspect(MYSQL)
            if (current['running'] or current['id'] != stopped_state['id']
                    or current['started_at'] != stopped_state['started_at']
                    or current['finished_at'] != stopped_state['finished_at']):
                raise RuntimeError('MySQL restarted while its stopped-state copy was being made')
        mounted = True
        run('mount', '-t', 'tmpfs', '-o', f'size={size_gib}G,noswap,mpol=bind:{node},mode=0700', 'tmpfs', ram)
        copy_verified(source, ram / 'data', out / 'copy-manifest.json', quiescent=quiescent)
        bound = True
        run('mount', '--bind', ram / 'data', source)
        service_start()
        assert variables() == before_vars, 'Database durability settings changed'
        running = inspect(MYSQL)
        mountinfo = Path(f"/proc/{running['pid']}/mountinfo").read_text().splitlines()
        actual = [line for line in mountinfo if line.split()[4] == '/var/lib/mysql']
        visible = Path(f"/proc/{running['pid']}/root/var/lib/mysql").stat()
        expected = (ram/'data').stat()
        observation = {'mountinfo': actual, 'visible_dev_ino': [visible.st_dev, visible.st_ino],
                       'expected_dev_ino': [expected.st_dev, expected.st_ino], 'container_id': running['id']}
        after_mount_check = inspect(MYSQL)
        assert after_mount_check['running'] and all(after_mount_check[k] == running[k]
            for k in ('id','pid','started_at')), 'MySQL changed during mount verification'
        save(out/'mount-observation.json', observation)
        assert (visible.st_dev,visible.st_ino)==(expected.st_dev,expected.st_ino), 'MySQL did not mount the same RAM directory'
        assert any(' - tmpfs ' in line for line in actual), 'MySQL data is not on tmpfs'
        save(out / 'active.json', {'source': str(source), 'original': before,
             'variables': before_vars, 'mount': json.loads(output('findmnt', '-J', '-T', source)),
             'container_mountinfo': actual, 'running': running})
        yield
    finally:
        if recovery_guard is not None and recovery_guard.exists():
            save(out / 'retained.json', {'reason': 'Unresolved owned Cube resources',
                                        'guard': str(recovery_guard), 'source': str(source)})
            raise RuntimeError('Private database retained for resource recovery')
        if recovery_guard is not None:
            save(recovery_guard, {'reason': 'Database restoration in progress', 'source': str(source)})
        errors = []
        def cleanup(label, action):
            try:
                return action()
            except Exception as exc:
                errors.append(label + ': ' + str(exc))
        if stopped:
            cleanup('stop API/master', lambda: run('systemctl', 'stop', *FRONT))
            cleanup('stop database', lambda: run('systemctl', 'stop', MYSQL_UNIT))
        can_unmount = not inspect(MYSQL)['running'] if stopped else True
        if can_unmount:
            if bound and os.path.ismount(source):
                cleanup('remove database bind', lambda: run('umount', source))
            if mounted and os.path.ismount(ram) and not os.path.ismount(source):
                cleanup('release private database RAM', lambda: run('umount', ram))
            if stopped and not os.path.ismount(source):
                cleanup('restart original database and services', service_start)
            elif stopped:
                errors.append('Database bind remains; refusing to restart against the private copy')
            elif front_stopped:
                cleanup('restart API/master', lambda: run('systemctl', 'start', *reversed(FRONT)))
        else:
            errors.append('Database did not stop; private mount retained to preserve its live files')
        restored_vars = cleanup('verify database variables', variables)
        if restored_vars != before_vars:
            errors.append('Database durability variables did not restore')
        save(out / 'restored.json', {'bound': os.path.ismount(source), 'errors': errors,
             'variables_unchanged': restored_vars == before_vars, 'running': inspect(MYSQL)})
        if errors:
            raise RuntimeError('Database restoration needs attention: ' + '; '.join(errors))
        if recovery_guard is not None:
            recovery_guard.unlink()


def proof(node, cpus, path):
    wanted = set()
    for part in cpus.split(','):
        a, *b = part.split('-')
        wanted.update(range(int(a), int(b[0]) + 1) if b else [int(a)])
    records = {}
    pids = [int(output('systemctl', 'show', u, '-p', 'MainPID', '--value')) for u in UNITS]
    pids += [inspect(n)['pid'] for n in CONTAINERS]
    cg = Path('/sys/fs/cgroup/cube_sandbox')
    assert (cg / 'cpuset.mems.effective').read_text().strip() == str(node)
    for part in (cg / 'cpuset.cpus.effective').read_text().strip().split(','):
        a, *b = part.split('-')
        actual = set(range(int(a), int(b[0]) + 1) if b else [int(a)])
        assert actual <= wanted, actual
    for pid in pids:
        if not pid:
            continue
        assert set(os.sched_getaffinity(pid)) <= wanted, pid
        status = Path(f'/proc/{pid}/status').read_text()
        mems = next(l.split(':', 1)[1].strip() for l in status.splitlines() if l.startswith('Mems_allowed_list:'))
        assert mems == str(node), (pid, mems)
        records[str(pid)] = {'cpus': sorted(os.sched_getaffinity(pid)), 'mems': mems}
    save(path, records)


def database_capacity_gib(config):
    data = inspect(MYSQL)
    source = Path(next(m['Source'] for m in data['mounts'] if m['Destination']=='/var/lib/mysql'))
    logical = sum(p.lstat().st_size for p in source.rglob('*') if stat.S_ISREG(p.lstat().st_mode))
    minimum = max(2, (logical + 512*1024**2 + GIB - 1)//GIB)
    configured = config['cube'].get('metadata_memory_size_gib', minimum)
    if type(configured) is not int or configured < minimum:
        raise ValueError(f'Cube metadata needs at least {minimum} GiB of RAM capacity')
    return configured, logical


@contextmanager
def managed_memory_service(config, out):
    """Own and restore the complete Cube RAM environment inside the NUMA lease."""
    if os.geteuid() != 0 or not metadata_enabled(config):
        raise ValueError('Managed Cube metadata requires the privileged pinned entry')
    from ae.scripts.cube_memory_context import memory_service, DROP
    settings = config['cube']
    measurement = config.get('measurement', {})
    node, cpus = measurement.get('numa_node'), str(measurement.get('cpus', ''))
    if type(node) is not int or node < 0 or measurement.get('pin') is not True:
        raise ValueError('Managed Cube requires an explicit pinned NUMA node')
    parent_pid = require_pinned_parent(node, cpus)
    size = settings.get('memory_size_gib', 12)
    if type(size) is not int or size < 12:
        raise ValueError('Cube RAM workspace must be at least 12 GiB')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    recovery_guard = out / 'RECOVERY_REQUIRED.json'
    with ExitStack() as stack:
        lease = stack.enter_context(run_lock(Path('/run/lock/deltabox-cube-memory.lock')))
        if DROP.exists() or sandboxes():
            raise ValueError('Cube already has an operation; no service settings changed')
        idle(timeout=1)
        metadata_gib, logical_bytes = database_capacity_gib(config)
        needed = size + metadata_gib + 2
        if node_available(node) < needed * GIB:
            raise ValueError(f'Cube requires {needed} GiB available on NUMA {node}')
        save(out/'plan.json', {'node': node, 'cpus': cpus, 'numa_lease_pid': parent_pid,
             'data_capacity_gib': size, 'metadata_capacity_gib': metadata_gib,
             'metadata_logical_bytes': logical_bytes})
        stack.enter_context(quiesce_webui(out, recovery_guard))
        stack.enter_context(mysql_launcher(out, recovery_guard))
        stack.enter_context(placement(node, cpus, out, recovery_guard))
        manifest = stack.enter_context(memory_service(out/'cube-memory', node=node, cpus=cpus,
            size_gib=size, lease_fd=lease, recovery_guard=recovery_guard))
        stack.enter_context(ram_database(node, out/'mysql-memory', recovery_guard, size_gib=metadata_gib))
        proof(node, cpus, out/'placement.json')
        yield {'memory_manifest': str(manifest), 'recovery_guard': str(recovery_guard),
               'metadata_manifest': str(out/'mysql-memory/active.json'),
               'placement_manifest': str(out/'placement.json')}
        proof(node, cpus, out/'placement-after.json')
