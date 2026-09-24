"""Admission checks for memory-backed FC-diff jobs; all checks run outside timers."""
from pathlib import Path
import json
import os
import shutil

GIB = 1024 ** 3


def node_available(node, node_root=Path('/sys/devices/system/node')):
    values = {}
    for line in (node_root / f'node{node}' / 'meminfo').read_text().splitlines():
        fields = line.split()
        values[fields[2].rstrip(':')] = int(fields[3]) * 1024
    reclaimable = max(0, values.get('Active(file)', 0) + values.get('Inactive(file)', 0)
                      - values.get('Dirty', 0) - values.get('Writeback', 0) - values.get('Mapped', 0))
    return values['MemFree'] + reclaimable


def job_size_gib(experiment, config):
    size = config.get('memory_job_size_gib', 64 if experiment == 'table-02-fc-diff' else 16)
    if type(size) is not int or size <= 0:
        raise ValueError('memory_job_size_gib must be a positive integer')
    if experiment == 'table-02-fc-diff':
        # Thin root + base snapshot + one merged image, with room for staging.
        minimum = 8 + 2 * int(config.get('mem_mib', 8192)) / 1024 + 4
        if size < minimum:
            raise ValueError(f'FC-diff tmpfs needs at least {minimum:g} GiB; configured {size} GiB')
    return size


def check_capacity(path, phase, allocation, record, *, node=None, reserve=2 * GIB):
    """Require space for the next allocation, preserving evidence before cleanup."""
    usage = shutil.disk_usage(path)
    row = dict(phase=phase, tmpfs_total_bytes=usage.total,
               tmpfs_used_bytes=usage.used, tmpfs_free_bytes=usage.free,
               next_allocation_bytes=allocation, reserve_bytes=reserve)
    if node is not None:
        row.update(numa_node=node, numa_available_bytes=node_available(node))
    record.parent.mkdir(parents=True, exist_ok=True)
    with record.open('a') as stream:
        stream.write(json.dumps(row, sort_keys=True) + '\n')
    required = allocation + reserve
    if usage.free < required:
        raise RuntimeError(f'{phase}: tmpfs has {usage.free/GIB:.2f} GiB free; needs {required/GIB:.2f} GiB')
    if node is not None and row['numa_available_bytes'] < required:
        raise RuntimeError(f'{phase}: NUMA {node} has {row["numa_available_bytes"]/GIB:.2f} GiB available; needs {required/GIB:.2f} GiB')
    return row
