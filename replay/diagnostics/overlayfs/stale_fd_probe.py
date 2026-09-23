#!/usr/bin/env python3
"""Strict, shell-independent stale FD probes inside a disposable Linux guest."""
import ctypes
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


class CheckpointArgs(ctypes.Structure):
    _fields_ = [('options', ctypes.c_void_p), ('options_len', ctypes.c_uint32)]


def checkpoint(root):
    options = ctypes.create_string_buffer(
        f'lowerdir={root}/lower2,upperdir={root}/upper2,workdir={root}/work2'.encode())
    args = CheckpointArgs(ctypes.addressof(options), len(options.value))
    with DirectoryFD(root / 'merged') as fd:
        fcntl.ioctl(fd, (1 << 30) | (ctypes.sizeof(args) << 16) | (ord('O') << 8) | 1, args)


class DirectoryFD:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)

    def __enter__(self):
        return self.fd

    def __exit__(self, *_):
        os.close(self.fd)


def case(kind):
    root = Path(tempfile.mkdtemp(prefix='ovl-stale-fd-'))
    record = {'case': kind, 'pid': os.getpid(), 'ok': False}
    fd = None
    mounted = False
    try:
        for name in ('lower1', 'lower2', 'upper1', 'upper2', 'work1', 'work2', 'merged'):
            (root / name).mkdir()
        original = b'base-before-checkpoint\n'
        (root / 'lower1/file').write_bytes(original)
        subprocess.run(['mount', '-t', 'overlay', 'overlay', '-o',
                        f'lowerdir={root}/lower1,upperdir={root}/upper1,workdir={root}/work1',
                        str(root / 'merged')], check=True)
        mounted = True
        fd = os.open(root / 'merged/file', os.O_RDWR)
        os.pwrite(fd, b'old-', 0)
        snapshot = (root / 'upper1/file').read_bytes()
        if kind == 'unlink-before-checkpoint':
            os.unlink(root / 'merged/file')
        checkpoint(root)
        if kind == 'unlink-after-checkpoint':
            os.unlink(root / 'merged/file')
        elif kind == 'replace-after-checkpoint':
            (root / 'merged/file').write_bytes(b'new-branch\n')
        record['fd_before_write'] = {
            'fd': fd, 'flags': fcntl.fcntl(fd, fcntl.F_GETFL),
            'inode': os.fstat(fd).st_ino, 'nlink': os.fstat(fd).st_nlink,
            'target': os.readlink(f'/proc/self/fd/{fd}'),
            'fdinfo': Path(f'/proc/self/fdinfo/{fd}').read_text(),
        }
        marker = b'private-stale-fd\n'
        record['written'] = os.pwrite(fd, marker, 0)
        record['readback'] = os.pread(fd, len(marker), 0).decode()
        assert record['written'] == len(marker), 'short write'
        assert record['readback'].encode() == marker, 'private readback mismatch'

        # A nonzero seek reaches ovl_real_fdget(); SEEK_SET to zero takes a
        # special fast path and would miss a stale-backing regression there.
        seek_offset = 3
        record['seek_offset'] = os.lseek(fd, seek_offset, os.SEEK_SET)
        assert record['seek_offset'] == seek_offset, 'nonzero seek failed'
        suffix = os.read(fd, len(marker) - seek_offset)
        record['seek_readback'] = suffix.decode()
        assert suffix == marker[seek_offset:], 'ordinary read used wrong backing'
        assert os.lseek(fd, 0, os.SEEK_CUR) == len(marker), 'read offset mismatch'

        ordinary_marker = b'ORDINARY'
        assert os.lseek(fd, seek_offset, os.SEEK_SET) == seek_offset
        record['ordinary_written'] = os.write(fd, ordinary_marker)
        assert record['ordinary_written'] == len(ordinary_marker), 'ordinary short write'
        assert os.lseek(fd, 0, os.SEEK_CUR) == seek_offset + len(ordinary_marker), 'write offset mismatch'
        expected = marker[:seek_offset] + ordinary_marker + marker[seek_offset + len(ordinary_marker):]
        assert os.lseek(fd, 1, os.SEEK_SET) == 1
        ordinary_readback = os.read(fd, len(expected) - 1)
        record['ordinary_readback'] = ordinary_readback.decode()
        assert ordinary_readback == expected[1:], 'ordinary write/read mismatch'
        assert os.pread(fd, len(expected), 0) == expected, 'pread disagrees with ordinary write'
        if kind == 'rehome':
            assert (root / 'merged/file').read_bytes().startswith(expected)
        elif kind == 'replace-after-checkpoint':
            assert (root / 'merged/file').read_bytes() == b'new-branch\n', 'overwrote new path'
        else:
            assert not (root / 'merged/file').exists(), 'resurrected deleted path'
        if kind != 'unlink-before-checkpoint':
            assert (root / 'upper1/file').read_bytes() == snapshot, 'changed parent snapshot'
        record['ok'] = True
    except Exception as error:
        record['error'] = repr(error)
        record['errno'] = getattr(error, 'errno', None)
    finally:
        if fd is not None:
            os.close(fd)
        if mounted:
            subprocess.run(['umount', str(root / 'merged')], check=True)
        shutil.rmtree(root)
    print(json.dumps(record), flush=True)
    return record


if __name__ == '__main__':
    rows = [case(name) for name in ('rehome', 'unlink-before-checkpoint',
                                   'unlink-after-checkpoint', 'replace-after-checkpoint')]
    raise SystemExit(0 if all(row['ok'] for row in rows) else 1)
