#!/usr/bin/env python3
"""Disable THP only in an owned Cube service tree before executing its launcher.

The deployed VMM resolves pagemap PFNs before reading kpageflags. THP collapse
can relocate pages between those reads, making its anonymous-page mask stale.
This process setting survives fork/exec and never changes host-wide THP policy.
"""
import ctypes
import os
import sys


def main(argv=None):
    command = sys.argv[1:] if argv is None else argv
    if not command:
        raise ValueError('A Cube launcher command is required')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                          ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(41, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if libc.prctl(42, 0, 0, 0, 0) != 1:
        raise RuntimeError('Cube process THP disable did not take effect')
    os.execvp(command[0], command)


if __name__ == '__main__':
    main()
