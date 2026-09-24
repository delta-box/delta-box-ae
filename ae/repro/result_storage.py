"""Keep the latest AE run at a stable path and verify backups before replacing it."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

DEFAULT_BACKUP_ROOT = Path('/mnt/disk2/dyp/deltabox-runtime/ae/work/results-backups/public-ae')
RESERVE_BYTES = 10 * 1024**3


def timestamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')


def no_symlink_parents(path):
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('Result paths must not contain parent traversal')
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise ValueError('Result storage paths must not contain symlinks: ' + str(part))
    return path


@contextmanager
def run_lock(path):
    """Shared by full, selected, quick, resume, and analysis invocations."""
    path = no_symlink_parents(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('Results lock must be a regular file with one link')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Another AE run or results backup is active') from error
        yield fd
    finally:
        os.close(fd)


def active_references(root):
    """Detect older producers which predate the shared results lock.

    Inspect only path references into this result directory, never other jobs'
    file contents. Private mount namespaces are checked through each process.
    """
    root = str(root)
    def matches(value):
        value = value.removesuffix(' (deleted)')
        return value == root or value.startswith(root + '/')
    proc_root = Path('/proc')
    if not proc_root.is_dir():
        raise ValueError('Active result checks require Linux /proc')
    references = []
    for proc in sorted(proc_root.iterdir(), key=lambda p: p.name != str(os.getpid())):
        if not proc.name.isdecimal():
            continue
        try:
            for name in ('cwd', 'root'):
                try:
                    if matches(os.readlink(proc / name)):
                        references.append(proc.name + ':' + name)
                except FileNotFoundError:
                    pass
            for entry in (proc / 'fd').iterdir():
                try:
                    if matches(os.readlink(entry)):
                        references.append(proc.name + ':fd/' + entry.name)
                except FileNotFoundError:
                    pass
            with (proc / 'maps').open() as stream:
                for line in stream:
                    fields = line.rstrip().split(None, 5)
                    if len(fields) == 6 and matches(fields[5]):
                        references.append(proc.name + ':mapping')
                        break
            with (proc / 'mountinfo').open() as stream:
                for line in stream:
                    fields = line.split()
                    if len(fields) > 4:
                        mount = fields[4].replace('\\040', ' ').replace('\\011', '\t').replace('\\134', '\\')
                        if matches(mount):
                            references.append(proc.name + ':mount')
                            break
        except (FileNotFoundError, ProcessLookupError):
            pass
        except PermissionError:
            try:
                state = (proc / 'status').read_text()
                if any(line.startswith('State:') and 'Z (zombie)' in line for line in state.splitlines()):
                    continue
            except (FileNotFoundError, ProcessLookupError):
                continue
            # A non-root self-hosted run cannot inspect unrelated users.
            # If one of its own producers cannot be inspected, fail closed.
            try:
                if os.geteuid() == 0 or proc.stat().st_uid == os.geteuid():
                    raise ValueError('Cannot verify process references: ' + str(proc))
            except FileNotFoundError:
                pass
        if references:
            return references
    return references


def require_idle(root):
    refs = active_references(root)
    if refs:
        raise ValueError('Results still have active references: ' + ', '.join(refs[:8]))


def inventory(root, *, hashes=True):
    """Relative paths, bytes, metadata, symlink text, and internal hard links."""
    result = {}
    inodes = {}
    allocated = 0
    paths = [root]
    for directory, dirs, files in os.walk(root, followlinks=False):
        paths.extend(Path(directory) / name for name in dirs + files)
    for path in sorted(set(paths)):
        info = path.lstat()
        name = path.relative_to(root).as_posix()
        key = (info.st_dev, info.st_ino)
        if key not in inodes:
            allocated += info.st_blocks * 512
        record = dict(mode=stat.S_IMODE(info.st_mode), uid=info.st_uid,
                      gid=info.st_gid, mtime_ns=info.st_mtime_ns)
        record['xattrs'] = {
            attr: os.getxattr(path, attr, follow_symlinks=False).hex()
            for attr in sorted(os.listxattr(path, follow_symlinks=False))
        }
        if stat.S_ISLNK(info.st_mode):
            record.update(type='symlink', target=os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            record['type'] = 'directory'
        elif stat.S_ISREG(info.st_mode):
            record.update(type='file', size=info.st_size, hardlink=inodes.get(key, name))
            if hashes:
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(block)
                record['sha256'] = digest.hexdigest()
        else:
            raise ValueError('Unsupported live/special result entry: ' + str(path))
        inodes.setdefault(key, name)
        result[name] = record
    return result, allocated


def durable_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    sync_directory(path.parent)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_tree(root):
    for directory, _, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(directory) / name
            if path.is_symlink():
                continue
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        sync_directory(Path(directory))


def copy_tree(source, destination):
    # GNU cp preserves sparse files, hardlinks, ownership, ACLs and xattrs;
    # no source symlink is followed and no experiment dependency is relocated.
    subprocess.run(['cp', '--archive', '--reflink=never', '--sparse=always',
                    '--', str(source), str(destination)], check=True)


def prepare_latest(root, backup_root):
    """Caller holds run_lock until the new run exits, including this backup."""
    root = no_symlink_parents(root)
    backup_root = no_symlink_parents(backup_root)
    if (root == backup_root or root.is_relative_to(backup_root)
            or backup_root.is_relative_to(root)):
        raise ValueError('Backup and working results must be separate directories')
    if list(root.parent.glob('.' + root.name + '-retired-*')):
        raise ValueError('An interrupted results replacement needs inspection before a new run')
    if not root.exists():
        root.mkdir(parents=True)
        return None
    if not root.is_dir():
        raise ValueError('Results must be a directory')
    require_idle(root)
    if not any(root.iterdir()):
        return None
    metadata, allocated = inventory(root, hashes=False)
    backup_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(backup_root).free
    required = allocated + RESERVE_BYTES
    if free < required:
        raise ValueError(f'Backup requires {required / 1024**3:.2f} GiB including '
                         f'10 GiB reserve, but {backup_root} has {free / 1024**3:.2f} GiB; results kept')
    original, _ = inventory(root)
    if {name: {k: v for k, v in row.items() if k != 'sha256'}
            for name, row in original.items()} != metadata:
        raise ValueError('Results changed during backup preparation; results kept')
    archive = backup_root / ('results-' + timestamp())
    archive.mkdir(mode=0o750)
    copy = archive / 'results'
    record = dict(schema_version=1, status='copying', original_results_root=str(root),
                  archived_results_root=str(copy), created_at=datetime.now(timezone.utc).isoformat(),
                  source_allocated_bytes=allocated, reserve_bytes=RESERVE_BYTES, files=original,
                  path_mapping={str(root): str(copy)},
                  note='Historical JSON paths and symlink text are preserved verbatim; '
                       'the path mapping records the archived location.')
    manifest = archive / 'backup.json'
    durable_json(manifest, record)
    try:
        copy_tree(root, copy)
        if shutil.disk_usage(backup_root).free < RESERVE_BYTES:
            raise ValueError('Backup target no longer has 10 GiB reserve; results kept')
        copied, _ = inventory(copy)
        unchanged, _ = inventory(root)
        if copied != original or unchanged != original:
            raise ValueError('Backup content or metadata verification failed; results kept')
        sync_tree(copy)
        require_idle(root)
        record.update(status='verified', verified_at=datetime.now(timezone.utc).isoformat())
        durable_json(manifest, record)
    except BaseException as error:
        record.update(status='failed', error=str(error))
        durable_json(manifest, record)
        raise
    retired = root.with_name('.' + root.name + '-retired-' + archive.name)
    root.rename(retired)
    try:
        root.mkdir(mode=stat.S_IMODE(retired.stat().st_mode))
        if os.geteuid() == 0:
            os.chown(root, retired.stat().st_uid, retired.stat().st_gid)
        shutil.copystat(retired, root, follow_symlinks=False)
        sync_directory(root.parent)
    except BaseException:
        if root.exists():
            root.rmdir()
        retired.rename(root)
        raise
    # Only the duplicate whose complete copy was verified is removed.
    shutil.rmtree(retired)
    sync_directory(root.parent)
    record.update(status='complete', completed_at=datetime.now(timezone.utc).isoformat())
    durable_json(manifest, record)
    return dict(path=str(archive), manifest=str(manifest), files=len(original),
                source_allocated_bytes=allocated)
