"""Opt-in, owned UFFD diagnostic metadata; no service/RPC/guest operations.

The fixture/launcher admits the precise build/install/handover chain. This helper
rechecks its protected bytes and runtime/process scope, and records that chain.
Absence of the opt-in is handled by service_placement before importing us.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

REQUEST_ENV = 'AE_E2B_UFFD_AGGREGATE_REQUEST'
SELECTED_ENV = 'E2B_OWNED_UFFD_AGGREGATE_CONFIG'
CONTROL = 'c904a7e4e365d0366693c76312779d965b2de870c970f60f30033ea9ae5e45ae'
EXE = '/mnt/disk2/dyp/ae-hosted-20260922/e2b/bin/orchestrator'
UNIT = 'ae-e2b-orchestrator.service'
PROC = Path('/proc')
REFS = {'overlay_manifest', 'backend_build', 'backend_install', 'backend_release',
        'runtime_handover', 'runtime_release', 'source_archive_receipt'}
REQUEST_KEYS = {'schema', 'nonce', 'output', 'runtime_sha256', 'executable_sha256',
                'fixture', 'node', 'cpus', 'control_sha256', 'refs'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(path):
    p = Path(path)
    if not p.is_absolute() or str(p) != str(path) or p.resolve() != p:
        raise ValueError('Diagnostic path is not canonical')
    return p


def _identity(st):
    return {'dev': st.st_dev, 'inode': st.st_ino, 'uid': st.st_uid,
            'mode': stat.S_IMODE(st.st_mode), 'nlink': st.st_nlink}


def protected(path, limit, *, expected=None):
    """Read and parse the same bounded no-follow bytes, checking stable identity."""
    p = canonical(path)
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or
                before.st_nlink != 1 or before.st_mode & 0o022 or before.st_size > limit):
            raise ValueError('Untrusted diagnostic file')
        with os.fdopen(os.dup(fd), 'rb') as handle:
            data = handle.read(limit + 1)
        after = os.fstat(fd)
        if (len(data) > limit or len(data) != before.st_size or
                _identity(before) != _identity(after) or
                before.st_mtime_ns != after.st_mtime_ns or
                _identity(p.lstat()) != _identity(after)):
            raise ValueError('Diagnostic file changed during read')
        record = dict(path=str(p), bytes=len(data), sha256=digest(data), **_identity(after))
        if expected is not None and any(record[k] != expected[k] for k in ('path', 'bytes', 'sha256')):
            raise ValueError('Diagnostic reference bytes mismatch')
        return data, record
    finally:
        os.close(fd)


def descriptor(value, root):
    if (not isinstance(value, dict) or set(value) != {'path', 'bytes', 'sha256'} or
            type(value['bytes']) is not int or not 0 < value['bytes'] <= 2 * 1024**2 or
            not isinstance(value['sha256'], str) or not re.fullmatch('[a-f0-9]{64}', value['sha256'])):
        raise ValueError('Invalid diagnostic reference')
    p = canonical(value['path'])
    if not p.is_relative_to(root):
        raise ValueError('Diagnostic reference is outside the public repository')
    return value


def validate_request(reference, *, root, output, source_sha256, node, cpus):
    """Preparation-only validation shared by the protected fixture and context.

    Returns (request, actual request file record). References are byte-bound here;
    their schema-specific install/build semantics are admitted by the launcher.
    This function neither creates files nor requires a future daemon/guard.
    """
    root, output = canonical(root), canonical(output)
    if (output.parent != root/'ae/results/selected' or not 1 <= len(output.name) <= 100 or
            not re.fullmatch('[A-Za-z0-9_.-]+', output.name)):
        raise ValueError('Diagnostic output is outside the selected run namespace')
    ref = json.loads(reference) if isinstance(reference, str) else reference
    descriptor(ref, root)
    data, record = protected(ref['path'], 16384, expected=ref)
    request = json.loads(data)
    if not isinstance(request, dict) or set(request) != REQUEST_KEYS:
        raise ValueError('Invalid diagnostic request schema')
    if (request['schema'] != 'deltabox.owned-uffd-request.v1' or
            request['output'] != str(output) or request['runtime_sha256'] != source_sha256 or
            not isinstance(source_sha256, str) or not re.fullmatch('[a-f0-9]{64}', source_sha256) or
            type(request['node']) is not int or request['node'] != node or node != 3 or
            request['cpus'] != cpus or cpus != '72-75' or
            request['control_sha256'] != CONTROL or
            not isinstance(request['nonce'], str) or not re.fullmatch('[a-f0-9]{32}', request['nonce']) or
            not isinstance(request['executable_sha256'], str) or
            not re.fullmatch('[a-f0-9]{64}', request['executable_sha256'])):
        raise ValueError('Diagnostic request scope mismatch')
    if not isinstance(request['refs'], dict) or set(request['refs']) != REFS:
        raise ValueError('Diagnostic provenance reference set mismatch')
    for ref in [request['fixture'], *request['refs'].values()]:
        descriptor(ref, root)
        protected(ref['path'], 2 * 1024**2, expected=ref)
    return request, record


def _ticks(base):
    return int((base/'stat').read_text().rsplit(')', 1)[1].split()[19])


def selected_process(row, expected_sha):
    """Only the selected env value is returned; all other environ bytes stay local."""
    p = row['process']
    pid, ticks = p['pid'], p['start_ticks']
    if type(pid) is not int or pid <= 0 or type(ticks) is not int or ticks <= 0:
        raise ValueError('Invalid diagnostic daemon incarnation')
    base = PROC/str(pid)
    if _ticks(base) != ticks or os.readlink(base/'exe') != EXE or row['binary_sha256'] != expected_sha:
        raise ValueError('Diagnostic daemon identity mismatch')
    status = dict(line.split(':', 1) for line in (base/'status').read_text().splitlines() if ':' in line)
    if [int(x) for x in status['Uid'].split()] != [0, 0, 0, 0]:
        raise ValueError('Diagnostic daemon cannot access a root-private directory')
    if (base/'cgroup').read_text().strip() != '0::/system.slice/' + UNIT:
        raise ValueError('Diagnostic daemon is not in the registered service')
    with (base/'environ').open('rb') as handle:
        environment = handle.read(8 * 1024**2 + 1)
    if len(environment) > 8 * 1024**2:
        raise ValueError('Diagnostic daemon environment exceeds bound')
    values = [x.split(b'=', 1)[1] for x in environment.split(b'\0') if x.startswith(SELECTED_ENV.encode()+b'=')]
    if len(values) > 1:
        raise ValueError('Duplicate selected diagnostic environment')
    selected = {'present': bool(values), 'value': values[0].decode() if values else None}
    if _ticks(base) != ticks or os.readlink(base/'exe') != EXE:
        raise ValueError('Diagnostic daemon changed while reading selected environment')
    return {'pid': pid, 'start_ticks': ticks, 'exe': EXE,
            'executable_sha256': expected_sha, 'selected_environment': selected}


def _json(data):
    return (json.dumps(data, sort_keys=True, indent=2) + '\n').encode()


def exclusive_json(path, value, limit):
    """Publish complete fsynced bytes without replacing an existing final name."""
    return exclusive_bytes(path, _json(value), limit)


def exclusive_bytes(path, data, limit):
    if len(data) > limit:
        raise ValueError('Diagnostic metadata exceeds bound')
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.pending')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, 'wb', closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(fd)
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        os.close(fd)
    return {'path': str(path), 'bytes': len(data), 'sha256': digest(data)}


class Session:
    def __init__(self, reference, *, root, out, source_sha256, node, cpus, before, admission):
        self.root, self.out = canonical(root), canonical(out)
        output = self.out.parents[2]
        if self.out != output/'job/environment/e2b-placement':
            raise ValueError('Diagnostic placement path mismatch')
        self.request, self.request_record = validate_request(reference, root=self.root, output=output,
            source_sha256=source_sha256, node=node, cpus=cpus)
        from release.lock import runtime_identity
        if runtime_identity(self.root)['source_sha256'] != source_sha256:
            raise ValueError('Diagnostic current runtime source mismatch')
        if any(type(admission.get(key)) is not int or admission[key] <= 0 for key in ('numa_lease_owner', 'results_lease_owner')):
            raise ValueError('Diagnostic requires the original owned leases')
        self.before = selected_process(before['units'][UNIT], self.request['executable_sha256'])
        if self.before['selected_environment']['value'] not in (None, ''):
            raise ValueError('Original diagnostic environment is already enabled')
        self.directory = output/'job/environment/uffd-aggregate'
        self.directory.mkdir(mode=0o700, exist_ok=False)
        self.directory_identity = {k: v for k, v in _identity(self.directory.lstat()).items() if k != 'nlink'}
        self.check_directory()
        self.config = self.directory/'config.json'
        self.guard = self.root/'ae/work/E2B_SERVICE_RECOVERY_REQUIRED.json'
        self.guard_record = None
        self.active = None
        self.config_record = None
        self.pending = []
        exclusive_json(self.directory/'prepared.json', {'schema': 'deltabox.owned-uffd-prepared.v1',
            'request': self.request_record, 'references': self.request['refs'], 'fixture': self.request['fixture'],
            'before': self.before, 'admission': admission, 'directory': self.directory_identity,
            'provenance_scope': 'Protected reference bytes; exact build/install semantics are admitted by the launcher'}, 32768)

    def check_directory(self):
        st = self.directory.lstat()
        if (canonical(self.directory) != self.directory or not stat.S_ISDIR(st.st_mode) or
                st.st_uid != 0 or stat.S_IMODE(st.st_mode) != 0o700 or
                any(_identity(st)[k] != v for k, v in self.directory_identity.items())):
            raise ValueError('Diagnostic directory identity changed')

    def bind_guard(self):
        data, self.guard_record = protected(self.guard, 8192)
        if json.loads(data) != {'reason': 'E2B placement transaction in progress', 'evidence': str(self.out)}:
            raise ValueError('Diagnostic initial guard scope mismatch')
        self.check_directory()
        exclusive_bytes(self.directory/'initial-guard.json', data, 8192)

    def check_guard(self):
        if self.guard_record is None:
            raise ValueError('Diagnostic initial guard was not bound')
        _, record = protected(self.guard, 8192, expected=self.guard_record)
        if record != self.guard_record:
            raise ValueError('Diagnostic initial guard identity changed')

    def dropin(self):
        value = str(self.config)
        if not re.fullmatch('[A-Za-z0-9_./-]+', value):
            raise ValueError('Unsafe diagnostic environment path')
        return 'Environment="' + SELECTED_ENV + '=' + value + '"\n'

    def publish(self, active, storage):
        self.check_directory()
        self.check_guard()
        self.active = selected_process(active['units'][UNIT], self.request['executable_sha256'])
        if self.active['selected_environment'] != {'present': True, 'value': str(self.config)}:
            raise ValueError('Active diagnostic environment mismatch')
        socket_root = canonical(storage.ram/'tmp')
        if socket_root.parent.parent != self.root/'ae/work' or not re.fullmatch('et-[a-f0-9]{10}', socket_root.parent.name):
            raise ValueError('Diagnostic socket root mismatch')
        base = PROC/str(self.active['pid'])
        for path in (self.directory, socket_root):
            local, visible = path.stat(), (base/'root'/str(path).lstrip('/')).stat()
            if (local.st_dev, local.st_ino) != (visible.st_dev, visible.st_ino):
                raise ValueError('Diagnostic directory is not visible in the owned daemon')
        if _ticks(base) != self.active['start_ticks']:
            raise ValueError('Active diagnostic daemon changed before config publication')
        data = {'schema': 'deltabox.owned-uffd-aggregate-config.v1', 'nonce': self.request['nonce'],
            'output': self.request['output'], 'socket_root': str(socket_root), 'directory': str(self.directory),
            'pid': self.active['pid'], 'start_ticks': self.active['start_ticks'],
            'executable_sha256': self.request['executable_sha256'], 'runtime_sha256': self.request['runtime_sha256'],
            'control_sha256': CONTROL, 'guard_sha256': self.guard_record['sha256']}
        self.config_record = exclusive_json(self.config, data, 8192)
        _, actual_record = protected(self.out/'actual.json', 2 * 1024**2)
        exclusive_json(self.directory/'published.json', {'schema': 'deltabox.owned-uffd-published.v1',
            'config': self.config_record, 'request': self.request_record, 'actual': actual_record,
            'active': self.active, 'initial_guard': self.guard_record}, 32768)

    def seal(self):
        """Called only after the original stop_owned_units barrier succeeds."""
        self.check_directory()
        files, pending, identities, finals = [], [], {}, {}
        for i in range(1, 129):
            for kind, limit in (('identity', 2048), ('final', 12288)):
                path = self.directory/('instance-%03d-%s.json' % (i, kind))
                try:
                    raw, record = protected(path, limit)
                    row = json.loads(raw)
                    ident = row if kind == 'identity' else row['identity']
                    if (ident.get('schema') != 'deltabox.owned-uffd-identity.v1' or
                            (kind == 'final' and row.get('schema') != 'deltabox.owned-uffd-aggregate.v1') or
                            ident['nonce'] != self.request['nonce'] or ident['instance'] != i or
                            self.active is None or self.config_record is None or
                            ident['pid'] != self.active['pid'] or ident['start_ticks'] != self.active['start_ticks'] or
                            ident['config_sha256'] != self.config_record['sha256']):
                        raise ValueError('Diagnostic instance scope mismatch')
                    (identities if kind == 'identity' else finals)[i] = ident
                    files.append(record)
                    if kind == 'final' and row.get('statistics_complete') is not True:
                        pending.append({'file': path.name, 'reason': 'statistics-incomplete'})
                except FileNotFoundError:
                    pass
                except Exception as error:
                    pending.append({'file': path.name, 'reason': type(error).__name__})
        for i in sorted(identities.keys() | finals.keys()):
            if i not in identities or i not in finals or identities[i] != finals[i]:
                pending.append({'instance': i, 'reason': 'missing-or-different-identity-final'})
        if not identities:
            pending.append({'reason': 'no-instance-identities'})
        try:
            _, record = protected(self.directory/'instances-censored.json', 512)
            files.append(record)
            pending.append({'reason': 'instance-limit'})
        except FileNotFoundError:
            pass
        if self.config_record is None:
            pending.append({'reason': 'config-not-published'})
        else:
            protected(self.config, 8192, expected=self.config_record)
            files.append(self.config_record)
        for name, limit in (('prepared.json', 32768), ('published.json', 32768), ('initial-guard.json', 8192)):
            try:
                _, record = protected(self.directory/name, limit)
                files.append(record)
            except Exception as error:
                pending.append({'file': name, 'reason': type(error).__name__})
        return exclusive_json(self.directory/'sealed-index.json', {
            'schema': 'deltabox.owned-uffd-sealed-index.v1', 'request': self.request_record,
            'config': self.config_record, 'active': self.active, 'owned_stop_confirmed': True,
            'files': files, 'pending': pending,
            'terminal_fixed_files': ['restored.json', 'pending-seal.json', 'pending-restored-record.json',
                                     'pending-guard-admission.json', 'pending-guard-retained.json'],
            'scope': 'Bounded observation files; not experiment success or full VM coverage. Terminal fixed files require separate collection/hash after restoration; they are not sealed here.'}, 262144)

    def note_pending(self, phase, error):
        """Best effort only: no original error/environment contents are serialized."""
        try:
            self.pending.append({'phase': phase, 'reason': type(error).__name__})
            self.check_directory()
            exclusive_json(self.directory/('pending-' + phase + '.json'), {
                'schema': 'deltabox.owned-uffd-pending.v1', 'request': self.request_record,
                'pending': self.pending}, 16384)
        except BaseException:
            pass

    def verify_restored(self, restored):
        actual = selected_process(restored['units'][UNIT], self.request['executable_sha256'])
        if actual['selected_environment'] != self.before['selected_environment']:
            raise ValueError('Original diagnostic environment was not restored')
        if self.active is not None:
            try:
                if _ticks(PROC/str(self.active['pid'])) == self.active['start_ticks']:
                    raise ValueError('Measured diagnostic daemon is still alive')
            except FileNotFoundError:
                pass
        # Saving observation metadata is not a resource-restoration criterion.
        try:
            self.check_directory()
            exclusive_json(self.directory/'restored.json', {'schema': 'deltabox.owned-uffd-restored.v1',
                'before': self.before, 'restored': actual, 'pending': self.pending}, 16384)
        except BaseException as error:
            self.note_pending('restored-record', error)
