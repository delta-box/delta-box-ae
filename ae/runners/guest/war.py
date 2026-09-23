#!/usr/bin/env python3
"""Measure each recorded edit on a private, memory-backed loop filesystem."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import time

out = Path('/tmp/ae-output')
out.mkdir(exist_ok=True)
config = json.loads(Path('/app/experiment.json').read_text())
arm = config['arm']
work = Path('/tmp/ae-war')
work.mkdir()  # Must not silently reuse an earlier filesystem.
loop = None
mounted = False
fs = work/'fs'
merged = work/'merged'
try:
    subprocess.run(['mount', '-t', 'tmpfs', '-o', 'size=5G,noswap,mode=0700',
                    'ae-war-memory', str(work)], check=True)
    mounted = True
    backing = subprocess.check_output(['stat', '-f', '-c', '%T', str(work)], text=True).strip()
    mount = next(line.split() for line in Path('/proc/mounts').read_text().splitlines()
                 if line.split()[1] == str(work))
    if backing != 'tmpfs' or 'noswap' not in mount[3].split(','):
        raise RuntimeError('WAR requires a noswap tmpfs backing store')
    image = work/'loop.img'
    with image.open('wb') as stream:
        stream.truncate(4 * 1024**3)
    loop = subprocess.check_output(['losetup', '-f', '--show', str(image)], text=True).strip()
    fs.mkdir()
    merged.mkdir()
    if arm == 'ext4':
        subprocess.run(['mkfs.ext4', '-q', '-F', loop], check=True)
    else:
        subprocess.run(['mkfs.xfs', '-q', '-f', '-m',
                        'reflink='+('1' if arm == 'xfs_reflink' else '0'), loop], check=True)
    subprocess.run(['mount', loop, str(fs)], check=True)
    storage = dict(backing_fstype=backing, mount_options=mount[3].split(','),
                   loop=loop, image=str(image), logical_bytes=image.stat().st_size,
                   allocated_bytes=image.stat().st_blocks*512,
                   filesystem_arm=arm, block_size=os.statvfs(fs).f_frsize,
                   physical_io_definition='loop sectors written times 512, including filesystem journal and metadata; not host disk writes',
                   write_protocol='historical first-hunk-to-EOF suffix write')
    if arm != 'ext4':
        storage['xfs_info'] = subprocess.check_output(['xfs_info', str(fs)], text=True)
    (out/'storage.json').write_text(json.dumps(storage, indent=2)+'\n')
    lower = fs/'lower'
    lower.mkdir()
    with tarfile.open('/app/lower.tar') as tar:
        for item in tar.getmembers():
            if not item.isfile() or not (lower/item.name).resolve().is_relative_to(lower):
                raise ValueError('unsafe lower input')
        tar.extractall(lower)
    from swesearch_replay_engine import apply_unified_diff
    from war_inputs import prepare_actions
    info = json.loads(Path('/app/actions.json').read_text())
    eligible, excluded, mismatches = prepare_actions(info, lower, apply_unified_diff)
    actions = work/'eligible-actions.json'
    actions.write_text(json.dumps(eligible))
    (out/'input-audit.json').write_text(json.dumps(dict(
        requested_edits=len(info['edits']), eligible_edits=len(eligible['edits']),
        excluded=excluded, recorded_path_mismatches=mismatches,
        protocol='Historical base-file mapping and parser retained; mismatches disclosed; no context-validation claim'
    ), indent=2)+'\n')
    result_path = out/(config['input_key']+'_'+arm+'.jsonl')
    subprocess.run(['python3', '/app/swesearch_replay_engine.py', '--actions', str(actions),
        '--lower', str(lower), '--upper-base', str(fs/'uppers'), '--merged', str(merged),
        '--fs-mnt', str(fs), '--ovl-ioctl-bin', '/app/ovl_ioctl', '--loop-name', Path(loop).name,
        '--fs-arm', arm, '--instance', config['input_key'], '--out-jsonl',
        str(result_path)], check=True)
    rows = [json.loads(line) for line in result_path.read_text().splitlines() if line]
    rows.extend(dict(row,instance=config['input_key'],fs_arm=arm) for row in excluded)
    rows.sort(key=lambda row: row['edit_idx'])
    result_path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
finally:
    for target in (merged, fs):
        if os.path.ismount(target):
            subprocess.run(['umount', str(target)], check=True)
    if loop is not None:
        subprocess.run(['losetup', '-d', loop], check=True)
    if mounted:
        # losetup -d uses deferred destruction; wait for the backing-file
        # reference to disappear before unmounting its tmpfs.
        for attempt in range(50):
            result = subprocess.run(['umount', str(work)], capture_output=True, text=True)
            if result.returncode == 0:
                break
            time.sleep(.1)
        else:
            raise RuntimeError('Cannot release WAR tmpfs: '+result.stderr)
    work.rmdir()
