"""Reclaim only this runner's inactive devices, between measured trajectories."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import time


def observe_owned_pids(root):
    result = set()
    for entry in Path('/proc').glob('[0-9]*'):
        try:
            args = (entry/'cmdline').read_bytes().split(b'\0')
            if any(is_owned_controller_arg(root, arg) for arg in args):
                result.add(int(entry.name))
        except OSError:
            pass
    return result


def is_owned_controller_arg(root, arg):
    # baseline.py copies the frozen driver into each trace's evidence directory.
    try:
        relative = Path(arg.decode()).relative_to(root/'formal/results')
    except (ValueError, UnicodeDecodeError):
        return False
    return (len(relative.parts) == 3 and relative.parts[1:] ==
            ('driver', 'fc_dm_controller_pilot.py'))


def reclaim(root, batch, owned_pids):
    assert not observe_owned_pids(root), 'storage barrier requires no active owned controller'
    record = {'epoch': time.time(), 'removed_dm': [], 'detached_loops': [],
              'root_free_before': shutil.disk_usage(root).free}
    devices = [line.split()[0] for line in subprocess.check_output(['dmsetup','ls'],text=True).splitlines()
               if line.startswith('fcdm')]
    for name in devices:
        match = re.fullmatch(r'fcdm([0-9]+)_(?:root|pool|seq[0-9]+)',name)
        assert match and int(match[1]) in owned_pids, ('unowned device',name)
    for name in sorted(devices, key=lambda n:n.endswith('_pool')):
        opened=int(subprocess.check_output(['dmsetup','info','-c','--noheadings','-o','open',name],text=True).strip())
        assert opened == 0, ('device still in use',name,opened)
        table=subprocess.check_output(['dmsetup','table',name],text=True)
        subprocess.run(['dmsetup','remove',name],check=True)
        record['removed_dm'].append({'name':name,'table':table})
    for entry in Path('/sys/block').glob('loop*/loop/backing_file'):
        backing=entry.read_text().strip()
        if not backing.startswith(str(batch/'results')+'/'):
            continue
        match=re.search(r'/fcdm([0-9]+)_thin_(?:data|meta)\.img$',backing)
        assert match and int(match[1]) in owned_pids, ('unowned loop',backing)
        device='/dev/'+entry.parts[-3]
        subprocess.run(['losetup','-d',device],check=True)
        Path(backing).unlink(missing_ok=True)
        record['detached_loops'].append({'device':device,'backing':backing})
    image=root/'snapshot-store.xfs'
    record['snapshot_allocated_before']=image.stat().st_blocks*512
    record['trim_snapshot']=subprocess.check_output(['fstrim','-v',str(root/'snapshots')],text=True)
    record['trim_root']=subprocess.check_output(['fstrim','-v','/'],text=True)
    record['snapshot_allocated_after']=image.stat().st_blocks*512
    record['root_free_after']=shutil.disk_usage(root).free
    with (batch/'resource-barriers.jsonl').open('a') as f:
        f.write(json.dumps(record)+'\n')
    return record
