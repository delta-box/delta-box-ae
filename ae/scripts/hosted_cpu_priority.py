"""Reviewer admission leases for a yielding background CPU campaign.

Reviewers hold a shared lease through their complete owned-unit cleanup. The
background controller only probes with a short exclusive nonblocking lock;
it must not retain an exclusive lease while measuring or waiting. Results'
exclusive measurement lease remains the shared-backend serialization barrier.

The installed root launcher supplies the fixed path. A service helper takes its
own shared lease again and keeps its descriptor inheritable when execing the
runner; systemd cannot receive the outer launcher's file descriptor.
"""
from contextlib import contextmanager
import fcntl
import math
import os
from pathlib import Path
import stat
import time

PRIORITY_PATH = Path('/run/lock/deltabox-ae-reviewer-priority.lock')
DEFAULT_PRIORITY_LOCK = PRIORITY_PATH
LOCK_OWNER_UID = 0


def _open_lock(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Reviewer priority lock requires an absolute fixed path')
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError('Reviewer priority lock refuses linked parents')
        sticky = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        if info.st_uid not in (0, LOCK_OWNER_UID) or info.st_mode & 0o022 and not sticky:
            raise ValueError('Reviewer priority lock parent is not protected')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info, named = os.fstat(fd), path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != LOCK_OWNER_UID or info.st_mode & 0o022
                or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
            raise ValueError('Reviewer priority lock must be a protected root-owned regular file')
        return fd
    except BaseException:
        os.close(fd)
        raise


def acquire_reviewer(path=PRIORITY_PATH):
    """Return an owned inheritable SH fd; caller closes it after all cleanup."""
    fd = _open_lock(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        os.set_inheritable(fd, True)
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def reviewer_lease(path=PRIORITY_PATH):
    """Hold reviewer intent even while waiting for the results/measurement lease."""
    fd = acquire_reviewer(path)
    try:
        yield fd
    finally:
        os.close(fd)


def reviewer_waiting(path=PRIORITY_PATH):
    """Probe only: never prevent a new reviewer from acquiring its shared lease."""
    fd = _open_lock(path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)  # Release the transient exclusive probe immediately.


def reviewers_active(path=PRIORITY_PATH):
    return reviewer_waiting(path)


def wait_for_reviewers(path=PRIORITY_PATH, *, stop_event=None, interval=0.25):
    """Return true when idle, false when cancelled; caller must still monitor.

    This is not a reservation. A reviewer may arrive after the probe: the normal
    results lease prevents overlap and the background controller must stop its
    own unit on the next active-reviewer observation.
    """
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError('Reviewer polling interval must be positive and finite')
    while True:
        if stop_event is not None and stop_event.is_set():
            return False
        if not reviewer_waiting(path):
            return True
        if stop_event is not None:
            if stop_event.wait(interval):
                return False
        else:
            time.sleep(interval)
