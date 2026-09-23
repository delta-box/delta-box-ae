#!/usr/bin/env python3
"""Sum bytes in non-shared (non-reflinked) extents across given files.

On XFS+reflink, extents that share blocks with another file carry the
FIEMAP_EXTENT_SHARED flag. On ext4 / XFS-no-reflink this flag is never set
(no sharing concept), so all extents count as non-shared -- which matches
"fully duplicated data" semantics.

Usage: nonshared_bytes.py <file> [<file> ...]
Prints: total non-shared bytes across all arguments (integer).
"""
import fcntl
import os
import struct
import sys

FS_IOC_FIEMAP = 0xC020660B
FIEMAP_FLAG_SYNC = 0x00000001
FIEMAP_EXTENT_SHARED = 0x00002000


def nonshared_bytes(path: str) -> int:
    if not os.path.isfile(path):
        return 0
    fd = os.open(path, os.O_RDONLY)
    try:
        max_extents = 1024
        header_sz = 32
        ext_sz = 56
        buf = bytearray(header_sz + ext_sz * max_extents)
        # fiemap header: fm_start, fm_length, fm_flags, fm_mapped_extents,
        #                fm_extent_count, fm_reserved
        struct.pack_into(
            "QQIIII", buf, 0,
            0, (1 << 63) - 1, FIEMAP_FLAG_SYNC, 0, max_extents, 0,
        )
        fcntl.ioctl(fd, FS_IOC_FIEMAP, buf, True)
        _, _, _, mapped, _, _ = struct.unpack_from("QQIIII", buf, 0)
        total = 0
        for i in range(mapped):
            off = header_sz + i * ext_sz
            # fiemap_extent: fe_logical, fe_physical, fe_length,
            #                fe_reserved64[2], fe_flags, fe_reserved[3]
            _, _, length, _, _, flags = struct.unpack_from(
                "QQQQQI", buf, off
            )
            if not (flags & FIEMAP_EXTENT_SHARED):
                total += length
        return total
    finally:
        os.close(fd)


if __name__ == "__main__":
    total = sum(nonshared_bytes(p) for p in sys.argv[1:])
    print(total)
