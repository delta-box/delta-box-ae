"""CoW prepayment inside the Figure 6 fork-only agent's own address space.

MADV_POPULATE_WRITE faults pages writable without copying user bytes back.
An external process cannot request this advice on Linux 6.8. The caller must
quiesce this worker before every template command, then retain the existing
single-threaded fork guard. This module is never enabled for durable dumps.
"""
from __future__ import annotations

import ctypes
import errno
import json
import mmap
import os
import struct
import threading
import time

MADV_POPULATE_WRITE = 23
CHUNK_BYTES = 512 * 1024
HOT_ANON_BYTES = 4 * 1024 * 1024
JOURNAL_MAGIC = b"DBWARM1\n"
JOURNAL_SLOTS = 1024
JOURNAL_SLOT_BYTES = 1028
JOURNAL_BYTES = len(JOURNAL_MAGIC) + JOURNAL_SLOTS * JOURNAL_SLOT_BYTES


class PrewarmError(RuntimeError):
    """A requested warm policy failed; it must not become a successful run."""


def anonymous_private_ranges(lines):
    """Return tiered ordinary writable anonymous mappings from procfs maps."""
    tiers = {1: [], 2: []}
    for line in lines:
        fields = line.strip().split(None, 5)
        if len(fields) < 5 or fields[1] != "rw-p" or fields[4] != "0":
            continue
        path = fields[5] if len(fields) == 6 else ""
        if path not in ("", "[heap]", "[stack]") and not path.startswith("[anon:"):
            continue
        start, end = (int(value, 16) for value in fields[0].split("-"))
        tier = 1 if path in ("[heap]", "[stack]") or end - start >= HOT_ANON_BYTES else 2
        tiers[tier].append((start, end))
    return [(tier, ranges) for tier, ranges in tiers.items() if ranges]


def _ranges():
    with open("/proc/self/maps") as stream:
        return anonymous_private_ranges(stream)


def _subtract(start, end, excluded):
    ranges = [(start, end)]
    for left, right in excluded:
        remaining = []
        for first, last in ranges:
            if last <= left or first >= right:
                remaining.append((first, last))
            else:
                if first < left:
                    remaining.append((first, left))
                if right < last:
                    remaining.append((right, last))
        ranges = remaining
    return ranges


def _covers(start, end, ranges):
    cursor = start
    for left, right in sorted(ranges):
        if right <= cursor:
            continue
        if left > cursor:
            return False
        cursor = max(cursor, right)
        if cursor >= end:
            return True
    return False


def journal_records(path):
    """Decode after measurement; incomplete publication never becomes success."""
    with open(path, "rb") as stream:
        data = stream.read()
    if len(data) != JOURNAL_BYTES or not data.startswith(JOURNAL_MAGIC):
        raise PrewarmError("Invalid CoW prewarm diagnostic journal")
    records = []
    for slot in range(JOURNAL_SLOTS):
        offset = len(JOURNAL_MAGIC) + slot * JOURNAL_SLOT_BYTES
        size = struct.unpack_from("<I", data, offset)[0]
        if not size:
            continue
        if size > JOURNAL_SLOT_BYTES - 4:
            raise PrewarmError("Invalid CoW prewarm diagnostic record length")
        records.append(json.loads(data[offset + 4:offset + 4 + size]))
    return sorted(records, key=lambda row: row["epoch"])


class CooperativePrewarm:
    error_type = PrewarmError

    def __init__(self, *, log_path="/tmp/prewarm.events"):
        if (os.environ.get("DELTABOX_PAPER_MEMORY_POLICY") != "warm"
                or os.environ.get("DELTABOX_FORK_ONLY_MEMCURVE") != "1"
                or os.environ.get("DELTABOX_ASYNC_INCREMENTAL_DUMP") == "1"):
            raise PrewarmError("CoW prewarm requires Figure 6 warm fork-only mode, without async-incremental")
        self.log_path = log_path
        self._thread = None
        self._cancel = threading.Event()
        self._failure = None
        self._epoch = None
        self._excluded = set()
        self.last_result = None
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._libc.madvise.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        self._libc.madvise.restype = ctypes.c_int
        # Linux/glibc exposes the pthread stack so the warm worker does not
        # fault its own mostly unused stack into the measured agent footprint.
        try:
            self._libc.pthread_self.restype = ctypes.c_ulong
            self._libc.pthread_getattr_np.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
            self._libc.pthread_getattr_np.restype = ctypes.c_int
            self._libc.pthread_attr_getstack.argtypes = [ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_size_t)]
            self._libc.pthread_attr_getstack.restype = ctypes.c_int
            self._libc.pthread_attr_destroy.argtypes = [ctypes.c_void_p]
            self._libc.pthread_attr_destroy.restype = ctypes.c_int
        except AttributeError as exc:
            raise PrewarmError("CoW prewarm requires Linux/glibc pthread stack discovery") from exc
        # Probe in the actual agent before any benchmark event. Missing
        # kernel support/permissions is an explicit startup failure.
        region = mmap.mmap(-1, mmap.PAGESIZE, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
                           prot=mmap.PROT_READ | mmap.PROT_WRITE)
        view = ctypes.c_char.from_buffer(region)
        try:
            try:
                self._populate(ctypes.addressof(view), mmap.PAGESIZE)
            except OSError as exc:
                raise PrewarmError(f"MADV_POPULATE_WRITE is unavailable: {exc}") from exc
        finally:
            del view
            region.close()
        # One fixed shared mapping survives template forks and process exit.
        # Publication below uses memory stores only; syscall/file export is
        # deferred to the runner after the measurement has ended.
        fd = os.open(log_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.ftruncate(fd, JOURNAL_BYTES)
            self._journal = mmap.mmap(fd, JOURNAL_BYTES, flags=mmap.MAP_SHARED,
                                      prot=mmap.PROT_READ | mmap.PROT_WRITE)
        finally:
            os.close(fd)
        self._journal[:len(JOURNAL_MAGIC)] = JOURNAL_MAGIC

    def _populate(self, address, size):
        if self._libc.madvise(address, size, MADV_POPULATE_WRITE) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))

    def _own_stack(self):
        # pthread_attr_t is 56 bytes on the supported x86_64 glibc guests;
        # reserve aligned storage larger than the ABI structure.
        attrs = (ctypes.c_ulong * 16)()
        rc = self._libc.pthread_getattr_np(self._libc.pthread_self(), ctypes.byref(attrs))
        if rc:
            raise OSError(rc, "pthread_getattr_np")
        try:
            address, size = ctypes.c_void_p(), ctypes.c_size_t()
            rc = self._libc.pthread_attr_getstack(ctypes.byref(attrs), ctypes.byref(address), ctypes.byref(size))
            if rc or not address.value or not size.value:
                raise OSError(rc or errno.EINVAL, "pthread_attr_getstack")
            return address.value, address.value + size.value
        finally:
            rc = self._libc.pthread_attr_destroy(ctypes.byref(attrs))
            if rc:
                raise OSError(rc, "pthread_attr_destroy")

    def check(self):
        if self._failure is not None:
            raise PrewarmError(f"CoW prewarm failed: {self._failure}") from self._failure

    def start_epoch(self, epoch):
        """Called only when restore changes the agent's replay epoch."""
        self.check()
        if epoch == self._epoch:
            return
        self.quiesce()
        self._epoch = epoch
        self._cancel.clear()
        # Capture before creating the pthread to avoid its new stack mapping.
        try:
            ranges = _ranges()
            self._thread = threading.Thread(target=self._run, args=(epoch, ranges),
                                            name=f"cow-prewarm-{epoch}", daemon=True)
            self._thread.start()
        except Exception as exc:
            self._thread = None
            self._failure = exc
            raise PrewarmError(f"Cannot start CoW prewarm: {exc}") from exc

    def quiesce(self):
        """Cancel between bounded chunks, join, then propagate worker errors."""
        thread = self._thread
        if thread is not None:
            self._cancel.set()
            # Never continue a fork while this thread is still alive. Keeping
            # the join unbounded is deliberate: a timeout must not bypass the
            # single-threaded invariant. The outer experiment has a timeout.
            thread.join()
            self._thread = None
        self.check()

    def close(self):
        try:
            self.quiesce()
        finally:
            self._journal.close()

    def _record(self, result):
        payload = json.dumps(result, separators=(",", ":")).encode()
        if len(payload) > JOURNAL_SLOT_BYTES - 4:
            raise PrewarmError("CoW prewarm diagnostic record exceeded its fixed bound")
        offset = len(JOURNAL_MAGIC) + (int(result["epoch"]) % JOURNAL_SLOTS) * JOURNAL_SLOT_BYTES
        # Length is published last; a killed writer cannot leave a partial
        # JSON document labelled as a complete record.
        self._journal[offset:offset + 4] = b"\0" * 4
        self._journal[offset + 4:offset + 4 + len(payload)] = payload
        self._journal[offset:offset + 4] = struct.pack("<I", len(payload))

    def _run(self, epoch, tiered):
        started = time.monotonic()
        result = {"pid": os.getpid(), "epoch": epoch, "mode": "populate-write-self",
                  "write_cow": True, "mapping_scope": "private-anonymous",
                  "pages": 0, "skipped_bytes": 0, "cancelled": False,
                  "complete": False, "ok": False, "state": "running", "tiers": [],
                  "journal_capacity": JOURNAL_SLOTS}
        try:
            self._record(result)
            self._excluded.add(self._own_stack())
            for tier, ranges in tiered:
                count = 0
                for start, end in ranges:
                    for first, last in _subtract(start, end, self._excluded):
                        for address in range(first, last, CHUNK_BYTES):
                            if self._cancel.is_set():
                                result["cancelled"] = True
                                break
                            size = min(CHUNK_BYTES, last - address)
                            try:
                                self._populate(address, size)
                            except OSError as exc:
                                # An agent may unmap an allocation concurrently.
                                # Do not hide ENOMEM on an unchanged VMA (OOM),
                                # permission errors or unsupported advice.
                                live = [pair for _, pairs in _ranges() for pair in pairs]
                                still_mapped = _covers(address, address + size, live)
                                if exc.errno != errno.ENOMEM or still_mapped:
                                    raise
                                result["skipped_bytes"] += size
                            else:
                                count += size // mmap.PAGESIZE
                        if result["cancelled"]:
                            break
                    if result["cancelled"]:
                        break
                result["pages"] += count
                result["tiers"].append({"tier": tier, "pages": count})
                if result["cancelled"]:
                    break
            result.update(ok=True, complete=not result["cancelled"],
                          state="cancelled" if result["cancelled"] else "completed")
        except BaseException as exc:
            self._failure = exc
            result["error"] = f"{type(exc).__name__}: {exc}"[:200]
            result["state"] = "failed"
        finally:
            result["elapsed_ms"] = (time.monotonic() - started) * 1000
            self.last_result = result
            # No open/write/flush/fsync or printing in the worker or the
            # fork join path. Shared diagnostic pages have a real, bounded
            # memory/CPU cost; they are not claimed to be zero overhead.
            try:
                self._record(result)
            except BaseException as exc:
                self._failure = self._failure or exc
