"""Small, read-only CRIU 4.2 pagemap statistics for completed local dumps.

Call only after CRIU has closed its images, in the background dump worker.
No page payload is read, no hash/JSON/log is generated. Unknown or malformed
wire data is rejected so a broken parser cannot report fictional page reuse.
"""
from pathlib import Path
import os
import stat
import struct

PAGEMAP_MAGIC = bytes.fromhex('1943565425400856')
MAX_RECORD_BYTES = 1024
UINT64_MAX = (1 << 64) - 1
PE_PARENT, PE_LAZY, PE_PRESENT = 1, 2, 4


def _varint(data, offset):
    result = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError('Truncated pagemap protobuf varint')
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            raise ValueError('Pagemap protobuf varint exceeds uint64')
        result |= (byte & 127) << shift
        if not byte & 128:
            return result, offset
    raise ValueError('Pagemap protobuf varint exceeds uint64')


def _fields(data):
    fields, offset = {}, 0
    while offset < len(data):
        key, offset = _varint(data, offset)
        number, wire = key >> 3, key & 7
        if not number or wire or number in fields:
            raise ValueError('Unsupported or duplicate pagemap protobuf field')
        fields[number], offset = _varint(data, offset)
    return fields


def _records(stream):
    while True:
        raw = stream.read(4)
        if not raw:
            return
        if len(raw) != 4:
            raise ValueError('Truncated pagemap record length')
        size, = struct.unpack('<I', raw)
        if not 0 < size <= MAX_RECORD_BYTES:
            raise ValueError('Unsupported pagemap record size')
        payload = stream.read(size)
        if len(payload) != size:
            raise ValueError('Truncated pagemap record')
        yield _fields(payload)


def read_pagemap_stats(path, page_size=4096):
    """Decode one pinned pagemap image, supporting its legacy compatibility fields."""
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size <= 0 or page_size & (page_size - 1):
        raise ValueError('Page size must be a positive power of two')
    result = dict(total_pages=0, parent_pages=0, present_pages=0,
                  lazy_eligible_pages=0, parent_lazy_pages=0, entries=0)
    with Path(path).open('rb') as stream:
        if stream.read(8) != PAGEMAP_MAGIC:
            raise ValueError('Unexpected CRIU pagemap image magic')
        records = _records(stream)
        head = next(records, None)
        if head is None or set(head) != {1} or head[1] > (1 << 32) - 1:
            raise ValueError('Invalid CRIU pagemap head')
        result['pages_id'] = head[1]
        previous_end = 0
        for row in records:
            if not {1, 2}.issubset(row) or set(row) - {1, 2, 3, 4, 5}:
                raise ValueError('Unsupported CRIU pagemap entry schema')
            # CRIU 4.2 writes compat_nr_pages=0 and the real nr_pages at field 5.
            count = row.get(5, row[2])
            address = row[1]
            if row[2] > (1 << 32) - 1 or row.get(3, 0) not in (0, 1):
                raise ValueError('Invalid CRIU compatibility field')
            flags = row.get(4, PE_PARENT if row.get(3, 0) else PE_PRESENT)
            if (not count or count > UINT64_MAX // page_size or address % page_size
                    or address < previous_end or address > UINT64_MAX - count * page_size):
                raise ValueError('Invalid or overlapping CRIU pagemap range')
            if (flags & ~(PE_PARENT | PE_LAZY | PE_PRESENT)
                    or not flags & (PE_PARENT | PE_PRESENT)
                    or flags & PE_PARENT and flags & PE_PRESENT):
                raise ValueError('Unsupported CRIU page flags for a regular local dump')
            previous_end = address + count * page_size
            if flags & PE_PARENT and flags & PE_LAZY:
                result['parent_lazy_pages'] += count
            result['entries'] += 1
            result['total_pages'] += count
            for key, bit in (('parent_pages', PE_PARENT), ('present_pages', PE_PRESENT),
                             ('lazy_eligible_pages', PE_LAZY)):
                if flags & bit:
                    result[key] += count
    return result


def _regular_size(path):
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f'Expected a regular CRIU image: {path.name}')
    return info.st_size


def collect_page_stats(image_dir, page_size=4096):
    """Summarize task pagemaps and stat payload files; no page data is opened.

    ``private_pages_bytes`` covers files referenced by task pagemaps;
    ``pages_data_bytes`` also includes payloads for shared-memory images.
    Parent bytes are references, not bytes written into the current directory.
    """
    root = Path(image_dir)
    task_images = sorted(p for p in root.glob('pagemap-*.img')
                         if p.name[len('pagemap-'):-len('.img')].isdigit())
    if not task_images:
        raise ValueError('Completed CRIU dump has no task pagemap images')
    result = dict(format='criu-pagemap-4.2', page_size=page_size,
                  pagemap_images=len(task_images), total_pages=0,
                  parent_pages=0, present_pages=0, lazy_eligible_pages=0, parent_lazy_pages=0,
                  private_pages_bytes=0, pages_data_bytes=0, tasks=[])
    seen_page_ids = set()
    for image in task_images:
        _regular_size(image)
        task = read_pagemap_stats(image, page_size)
        if task['pages_id'] in seen_page_ids:
            raise ValueError('Task pagemaps unexpectedly share a pages image')
        seen_page_ids.add(task['pages_id'])
        pages = root / ('pages-%d.img' % task['pages_id'])
        size = _regular_size(pages)
        if size != task['present_pages'] * page_size:
            raise ValueError(f'CRIU pages length disagrees with pagemap: {image.name}')
        task.update(image=image.name, pages_file=pages.name, pages_bytes=size)
        result['tasks'].append(task)
        result['private_pages_bytes'] += size
        for key in ('total_pages', 'parent_pages', 'present_pages', 'lazy_eligible_pages', 'parent_lazy_pages'):
            result[key] += task[key]
    result['pages_data_bytes'] = sum(_regular_size(p) for p in root.glob('pages-*.img'))
    return result
