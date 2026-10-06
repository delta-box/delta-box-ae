#!/usr/bin/env python3
"""Run one baseline producer with its work directory on host NVMe.

The producer keeps its canonical output path. A private mount namespace bind-mounts
a fresh owned directory under nvme_work_root over that suite, so checkpoint and
rsync traffic stays on /mnt/disk2. Earlier jobs under that root are left intact.
After the producer exits, reconstructable copies are removed and the remaining
evidence is copied back to the real results directory.
"""
from __future__ import annotations
import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'ae')]
from ae.repro.coordination import coordination_root
from ae.scripts.job_mount_namespace import release_inherited_cube_mounts
from repro.common import write_json
from repro.staging_cleanup import cleanup_reconstructable_staging

NVME = Path('/mnt/disk2')


def mount_info(path):
    return json.loads(subprocess.check_output(
        ['findmnt', '--json', '--target', str(path), '--output', 'TARGET,SOURCE,FSTYPE'],
        text=True))['filesystems'][0]


def all_mount_records():
    records = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        point = re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), fields[4])
        records.append(dict(mount_id=int(fields[0]), target=point))
    return records


def mount_record(path):
    target = str(path)
    records = [record for record in all_mount_records() if record['target'] == target]
    if len(records) > 1:
        raise RuntimeError('Multiple mounts at owned target: ' + target)
    return records[0] if records else None


def bind_private(source, target, mounts):
    if mount_record(target) is not None:
        raise RuntimeError('Refuse to cover an existing mount: ' + str(target))
    source_stat = source.stat()
    try:
        subprocess.run(['mount', '--bind', str(source), str(target)], check=True)
    finally:
        record = mount_record(target)
        if record is not None:
            observed = target.stat()
            if (observed.st_dev, observed.st_ino) != (source_stat.st_dev, source_stat.st_ino):
                raise RuntimeError('Bind mount source identity changed: ' + str(target))
            mounts.append(dict(record, device=observed.st_dev, inode=observed.st_ino))
    if record is None:
        raise RuntimeError('Bind mount was not observed: ' + str(target))


def verify_owned_mount(record):
    current = mount_record(Path(record['target']))
    observed = Path(record['target']).stat()
    if (current != {key: record[key] for key in ('mount_id', 'target')}
            or (observed.st_dev, observed.st_ino) != (record['device'], record['inode'])):
        raise RuntimeError('Owned mount identity changed; refusing cleanup: ' + record['target'])


def unmount_owned(record):
    verify_owned_mount(record)
    subprocess.run(['umount', record['target']], check=True)
    if mount_record(Path(record['target'])) is not None:
        raise RuntimeError('Mount remains after unmount: ' + record['target'])
    record['unmounted'] = True


def direct_children():
    return {int(pid) for path in Path('/proc/self/task').glob('*/children')
            for pid in path.read_text().split()}


def process_identity(pid):
    raw = Path(f'/proc/{pid}/stat').read_text()
    fields = raw[raw.rfind(')') + 2:].split()
    if int(fields[1]) != os.getpid():
        raise RuntimeError('Process is not an owned direct child: ' + str(pid))
    return dict(pid=pid, start_ticks=int(fields[19]))


def subreaper_flag(value=None):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    flag = ctypes.c_int()
    code = libc.prctl(37 if value is None else 36,
                      ctypes.cast(ctypes.byref(flag), ctypes.c_void_p) if value is None else ctypes.c_void_p(value),
                      0, 0, 0)
    if code != 0:
        raise OSError(ctypes.get_errno(), 'prctl child subreaper failed')
    return flag.value if value is None else value


class OwnedChildren:
    """A process-local subreaper; signal/reap only proven direct descendants."""
    def __init__(self):
        if (not hasattr(os, 'pidfd_open') or not hasattr(os, 'P_PIDFD')
                or not hasattr(signal, 'pidfd_send_signal')):
            raise RuntimeError('NVMe lifecycle requires Linux pidfd support')
        if direct_children():
            raise RuntimeError('NVMe helper already owns unrelated asynchronous children')
        self.previous = subreaper_flag()
        self.entries = {}
        self.evidence = []
        self.producer_pid = None
        self.live_reap_error = None
        self.reap_busy = False
        self.reap_pending = False
        subreaper_flag(1)

    def register(self, pid):
        identity = process_identity(pid)
        fd = os.pidfd_open(pid, 0)
        try:
            if process_identity(pid) != identity:
                raise RuntimeError('Owned child identity changed during pidfd acquisition')
        except BaseException:
            os.close(fd)
            raise
        proof = dict(identity)
        record = dict(identity, pidfd=fd, term_sent=False, kill_sent=False, proof=proof)
        self.entries[pid] = record
        self.evidence.append(proof)
        return record

    def start_live_reaping(self, producer_pid):
        """Reap terminal adopted children while the sole producer is active.

        CRIU restores an original PID. A restored orphan can become our child;
        leaving its zombie until final cleanup would block the next restore.
        This window contains only producer.wait, never synchronous helper calls.
        """
        if self.producer_pid is not None or producer_pid not in self.entries:
            raise RuntimeError('Live reaping requires one registered producer')
        self.producer_pid = producer_pid
        self.previous_sigchld = signal.signal(signal.SIGCHLD, self.reap_terminated)
        self.reap_terminated(None, None)  # Adopted exit can precede handler installation.

    def reap_terminated(self, signum, frame):
        if self.producer_pid is None:
            return
        if self.reap_busy:
            self.reap_pending = True
            return
        self.reap_busy = True
        try:
            while True:
                self.reap_pending = False
                for pid in direct_children() - {self.producer_pid}:
                    try:
                        record = self.entries.get(pid) or self.register(pid)
                        result = os.waitid(os.P_PIDFD, record['pidfd'], os.WEXITED | os.WNOHANG)
                        if result is not None:
                            record['proof'].update(reaped=True, reaped_during_producer=True,
                                                   exit_status=result.si_status)
                            os.close(self.entries.pop(pid)['pidfd'])
                    except (FileNotFoundError, ProcessLookupError):
                        # Process identity is rechecked before each acquisition;
                        # a disappearing candidate is not signaled or guessed.
                        continue
                if not self.reap_pending:
                    break
        except Exception as error:
            self.live_reap_error = f'{type(error).__name__}: {error}'
        finally:
            self.reap_busy = False
        # SIGCHLD can reenter after the loop decides to break but before busy
        # is cleared. Drain that deferred notification before returning.
        if self.reap_pending and self.producer_pid is not None:
            self.reap_terminated(None, None)

    def stop_live_reaping(self):
        if self.producer_pid is not None:
            self.producer_pid = None
            signal.signal(signal.SIGCHLD, self.previous_sigchld)
        if self.live_reap_error:
            raise RuntimeError('Adopted-child live reaping failed: ' + self.live_reap_error)

    def send(self, record, signum):
        if process_identity(record['pid']) != {key: record[key] for key in ('pid', 'start_ticks')}:
            raise RuntimeError('Owned child identity changed; refusing signal')
        signal.pidfd_send_signal(record['pidfd'], signum)

    def cleanup(self, child):
        # Popen must reap its own leader to preserve the actual producer code.
        if child is not None and child.poll() is None:
            record = self.entries.get(child.pid) or self.register(child.pid)
            try:
                try:
                    self.send(record, signal.SIGTERM)
                except (FileNotFoundError, ProcessLookupError):
                    pass  # Natural exit races are completed by Popen.wait.
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                try:
                    self.send(record, signal.SIGKILL)
                except (FileNotFoundError, ProcessLookupError):
                    pass
                child.wait(timeout=5)
        if child is not None and child.poll() is None:
            raise RuntimeError('Owned producer was not reaped')
        if child is not None and child.pid in self.entries:
            record = self.entries.pop(child.pid)
            record['proof'].update(reaped=True, returncode=child.returncode)
            os.close(record['pidfd'])
        deadline = time.monotonic() + 20
        force_deadline = None
        while True:
            pids = direct_children()
            if not pids:
                break
            for pid in pids:
                try:
                    record = self.entries.get(pid) or self.register(pid)
                    result = os.waitid(os.P_PIDFD, record['pidfd'], os.WEXITED | os.WNOHANG)
                    if result is not None:
                        record['proof']['reaped'] = True
                        os.close(self.entries.pop(pid)['pidfd'])
                        continue
                    if not record['term_sent']:
                        self.send(record, signal.SIGTERM)
                        record['term_sent'] = True
                        record['proof']['term_sent'] = True
                    if time.monotonic() >= deadline and not record['kill_sent']:
                        self.send(record, signal.SIGKILL)
                        record['kill_sent'] = True
                        record['proof']['kill_sent'] = True
                    result = os.waitid(os.P_PIDFD, record['pidfd'], os.WEXITED | os.WNOHANG)
                    if result is not None:
                        record['proof']['reaped'] = True
                        os.close(self.entries.pop(pid)['pidfd'])
                except (FileNotFoundError, ProcessLookupError):
                    if pid in self.entries:
                        record = self.entries[pid]
                        result = os.waitid(os.P_PIDFD, record['pidfd'], os.WEXITED | os.WNOHANG)
                        if result is not None:
                            record['proof']['reaped'] = True
                            os.close(self.entries.pop(pid)['pidfd'])
                    # An exiting child may still be listed before waitid can
                    # observe its terminal state. Keep its pinned fd and retry.
            if time.monotonic() >= deadline:
                if force_deadline is None:
                    force_deadline = time.monotonic() + 5
                if time.monotonic() >= force_deadline and direct_children():
                    raise RuntimeError('Owned descendants remain after pidfd cleanup')
            time.sleep(0.05)

    def restore(self):
        self.stop_live_reaping()
        if direct_children():
            raise RuntimeError('Refuse subreaper restore while owned descendants remain')
        subreaper_flag(self.previous)
        if subreaper_flag() != self.previous or direct_children():
            raise RuntimeError('Owned descendants appeared during subreaper restore')
        for record in self.entries.values():
            os.close(record['pidfd'])
        self.entries.clear()


def verify_owned_directory(path, recorded):
    current = path.lstat()
    if (not stat.S_ISDIR(current.st_mode) or current.st_dev != recorded.st_dev
            or current.st_ino != recorded.st_ino or current.st_uid != recorded.st_uid):
        raise RuntimeError('Owned directory identity changed; refusing removal: ' + str(path))


def retain_recovery(meta, errors):
    record = dict(meta, status='cleanup-failed', cleanup_errors=errors)
    fd, name = tempfile.mkstemp(prefix='nvme-job-cleanup-error-', suffix='.json', dir=ROOT / 'ae/work')
    with os.fdopen(fd, 'w') as stream:
        json.dump(record, stream, indent=2)
        stream.write('\n')
    if 'AE_HOSTED_CALLER_UID' in os.environ:
        guard = coordination_root(ROOT) / 'CPU_SERVICE_RECOVERY_REQUIRED.json'
        try:
            fd = os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            return name
        with os.fdopen(fd, 'w') as stream:
            json.dump(dict(reason='NVMe producer cleanup failed', receipt=name,
                           work_root=meta['work_root'], work_identity=meta['work_identity']), stream, indent=2)
            stream.write('\n')
    return name


def run(args):
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    suite = args.suite.resolve(strict=True)
    work_root = args.work_root
    if (os.geteuid() != 0 or not command or Path(args.key).name != args.key
            or args.key in ('.', '..')):
        raise ValueError('Require root, a command, and a safe job key')
    if not work_root.is_absolute() or any(parent.is_symlink() for parent in (work_root, *work_root.parents)):
        raise ValueError('NVMe work root must be an absolute path without symlinks')
    work_root.mkdir(parents=True, exist_ok=True)
    if work_root.stat().st_dev != NVME.stat().st_dev:
        raise ValueError('NVMe work root is not on /mnt/disk2')
    if '--out' not in command or Path(command[command.index('--out') + 1]).resolve() != suite / args.key:
        raise ValueError('Producer output must be the selected suite/job path')
    if (suite / args.key).exists():
        raise FileExistsError(args.key)
    suite_stat = suite.stat()
    work = Path(tempfile.mkdtemp(prefix='job-', dir=work_root))
    work_stat = work.stat()
    env = dict(os.environ)
    meta = dict(storage_mode='nvme', configured_work_root=str(work_root), work_root=str(work),
                device=work_stat.st_dev,
                work_identity=dict(device=work_stat.st_dev, inode=work_stat.st_ino),
                output_path=str(work / args.key), experiment_key=args.key)
    code = 1
    archive = None
    child = None
    mounts = []
    errors = []
    archived = False
    retained = False
    children = None
    archived_stat = None

    def interrupted(signum, frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt('NVMe producer terminated')

    previous_handler = signal.signal(signal.SIGTERM, interrupted)
    try:
        meta['inherited_cube_mounts'] = release_inherited_cube_mounts(ROOT)
        children = OwnedChildren()
        archive = Path(tempfile.mkdtemp(prefix='nvme-archive-', dir=ROOT / 'ae/work'))
        archive_stat = archive.stat()
        meta.update(archive_path=str(archive),
                    archive_identity=dict(device=archive_stat.st_dev, inode=archive_stat.st_ino))
        bind_private(suite, archive, mounts)
        bind_private(work, suite, mounts)
        info = mount_info(suite)
        if NVME.stat().st_dev != suite.stat().st_dev:
            raise RuntimeError('Suite did not switch to the NVMe device: ' + json.dumps(info))
        meta['mount'] = info
        identity = json.loads(env.get('AE_MEASUREMENT_IDENTITY', '{}'))
        # Analysis groups use the configured storage condition, while the
        # invocation-specific path remains explicit in runtime evidence.
        identity.update(storage_mode='nvme', nvme_work_root=str(work_root))
        env['AE_MEASUREMENT_IDENTITY'] = json.dumps(identity)
        env['AE_NVME_WORK_NAMESPACE'] = str(work)
        child = subprocess.Popen(command, env=env, start_new_session=True)
        children.register(child.pid)
        children.start_live_reaping(child.pid)
        code = child.wait()
    except BaseException as error:
        meta['original_error'] = f'{type(error).__name__}: {error}'
        code = 1
        raise
    finally:
        try:
            child_cleanup_ok = True
            try:
                if children is not None:
                    try:
                        children.stop_live_reaping()
                    except BaseException as error:
                        child_cleanup_ok = False
                        errors.append(f'live reap cleanup: {type(error).__name__}: {error}')
                        code = 1
                    children.cleanup(child)
                    children.restore()
            except BaseException as error:
                child_cleanup_ok = False
                errors.append(f'child cleanup: {type(error).__name__}: {error}')
                code = 1
            finally:
                meta['owned_children'] = children.evidence if children is not None else []
                meta['producer_returncode'] = child.returncode if child is not None else None
            output = work / args.key
            if output.is_symlink():
                errors.append('Producer output is a symlink; refusing archive or removal')
                code = 1
            if output.is_dir() and not errors:
                try:
                    try:
                        if code == 0:
                            report = cleanup_reconstructable_staging(output)
                            write_json(output / 'staging-cleanup.json', report)
                    except BaseException as error:
                        errors.append(f'staging cleanup: {type(error).__name__}: {error}')
                        code = 1
                    finally:
                        write_json(output / 'nvme-job.json', dict(meta, returncode=code, cleanup_errors=errors))
                        kept = 0
                        for dirpath, _dirs, files in os.walk(output):
                            for name in files:
                                kept += os.lstat(os.path.join(dirpath, name)).st_size
                        if code == 0 or kept <= 2 * 1024 ** 3:
                            verify_owned_mount(mounts[0])
                            if (archive / args.key).exists():
                                raise FileExistsError('Canonical archive destination already exists')
                            subprocess.run(['cp', '-a', '--sparse=always', '--reflink=never',
                                            str(output), str(archive / args.key)], check=True)
                            archived = True
                            archived_stat = (archive / args.key).stat()
                        else:
                            verify_owned_mount(mounts[0])
                            pointer = archive / args.key
                            pointer.mkdir()
                            write_json(pointer / 'nvme-retained.json', dict(
                                meta, status='retained-on-nvme', returncode=code,
                                path=str(output),
                                reason='failed job is larger than 2GiB; left on NVMe'))
                            retained = True
                            archived_stat = pointer.stat()
                except BaseException as error:
                    errors.append(f'archive: {type(error).__name__}: {error}')
                    code = 1
            if child_cleanup_ok:
                for record in reversed(mounts):
                    try:
                        unmount_owned(record)
                    except BaseException as error:
                        errors.append(f'unmount: {type(error).__name__}: {error}')
                        code = 1
            meta.update(returncode=code, mounts=mounts)
            if not errors:
                try:
                    if archive is not None:
                        verify_owned_directory(archive, archive_stat)
                        archive.rmdir()
                    if not retained and (archived or not output.exists()):
                        verify_owned_directory(work, work_stat)
                        if any(str(point).startswith(str(work) + '/') or point == str(work)
                               for point in (record['target'] for record in all_mount_records())):
                            raise RuntimeError('Owned work namespace still contains a mount')
                        shutil.rmtree(work)
                except BaseException as error:
                    errors.append(f'directory cleanup: {type(error).__name__}: {error}')
                    code = 1
            if archived_stat is not None and all(record.get('unmounted') for record in mounts):
                try:
                    verify_owned_directory(suite, suite_stat)
                    verify_owned_directory(suite / args.key, archived_stat)
                    final = dict(meta, returncode=code, cleanup_errors=errors,
                                 cleanup_status='failed' if errors else 'ok')
                    if retained:
                        final.update(status='retained-on-nvme', path=str(output),
                                     reason='failed job is larger than 2GiB; left on NVMe')
                    write_json(suite / args.key / ('nvme-retained.json' if retained else 'nvme-job.json'), final)
                except BaseException as error:
                    errors.append(f'final receipt: {type(error).__name__}: {error}')
                    code = 1
            if errors:
                meta['returncode'] = code
                receipt = retain_recovery(meta, errors)
                raise RuntimeError('NVMe cleanup failed; owned resources retained: ' + receipt)
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
    return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--work-root', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
