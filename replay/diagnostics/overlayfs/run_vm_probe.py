#!/usr/bin/env python3
"""Boot an isolated rootfs copy for semantic probes (not a timing benchmark).

Run as root inside `unshare --mount --net --propagation private` on spr4numa.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--runtime-repo', type=Path, required=True)
p.add_argument('--kernel', type=Path, required=True)
p.add_argument('--base-xfs', type=Path, required=True)
p.add_argument('--data-xfs', type=Path, required=True, help='Attached read-only as /dev/vdb')
p.add_argument('--out', type=Path, required=True)
p.add_argument('--probe-only', action='store_true', help='Run the strict Python probe without legacy suites')
a = p.parse_args()
a.out.mkdir(parents=True, exist_ok=False)
sys.path.insert(0, str(a.runtime_repo / 'ae/runners'))
import vm

with tempfile.TemporaryDirectory(prefix='ovl-probe-', dir=a.out) as directory:
    runtime = Path(directory)
    args = SimpleNamespace(kernel=a.kernel, base_xfs=a.base_xfs, data_xfs=a.data_xfs,
        run_rootfs=runtime / 'rootfs.xfs', socket=runtime / 'fc.sock', log=a.out / 'firecracker.log',
        ssh_pubkey=None, tap='ovl'+runtime.name[-9:], guest_ip=vm.GUEST_IP,
        vcpus=2, mem_mib=2048, ssh_timeout=120, reuse_rootfs=False, no_nat=True,
        inherit_process_group=True)
    process = None
    ssh = ['ssh', *vm.ssh_opts(), '-o', 'ConnectTimeout=10', f'root@{args.guest_ip}']
    scp = ['scp', '-O', *vm.ssh_opts(), '-o', 'ConnectTimeout=10']
    try:
        subprocess.run(['ip', 'link', 'set', 'lo', 'up'], check=True)
        process = vm.start_vm(args)
        (a.out / 'vm.json').write_text(json.dumps({'pid': process.pid, 'kernel': str(a.kernel),
            'kernel_sha256': hashlib.sha256(a.kernel.read_bytes()).hexdigest(),
            'host_affinity_cpus': sorted(os.sched_getaffinity(0)),
            'host_numa_policy': subprocess.check_output(['numactl', '--show'], text=True),
            'probe_sha256': hashlib.sha256((Path(__file__).parent / 'stale_fd_probe.py').read_bytes()).hexdigest(),
            'rootfs_copy': str(args.run_rootfs), 'base_read_only_source': str(a.base_xfs)}, indent=2))
        files = Path(__file__).parent
        subprocess.run(scp + [str(files / 'stale_fd_probe.py'), f'root@{args.guest_ip}:/tmp/'], check=True)
        # The packaged script corpus is copied, never edited in the source tree.
        subprocess.run(scp + ['-r', str(a.runtime_repo / 'ae/vendor/d-overlayfs/agentfs'),
                             f'root@{args.guest_ip}:/tmp/agentfs'], check=True)
        trace_setup = r'''mkdir -p /sys/kernel/debug; mount -t debugfs debugfs /sys/kernel/debug || true
T=/sys/kernel/debug/tracing
if test -f "$T/kprobe_events"; then
  echo 'r:ovldiag_cow ovl_rehome_or_anon_cow result=$retval' >> "$T/kprobe_events"
  echo 'r:ovldiag_ensure ovl_ensure_upper_and_switch result=$retval' >> "$T/kprobe_events"
  echo 'r:ovldiag_open ovl_open_realfile result=$retval' >> "$T/kprobe_events"
  echo 'r:ovldiag_write backing_file_write_iter result=$retval' >> "$T/kprobe_events"
  echo 1 > "$T/events/kprobes/enable"
  echo 1 > "$T/tracing_on"
fi
'''
        with (a.out / 'trace-setup.log').open('w') as log:
            subprocess.run(ssh + [trace_setup], stdout=log, stderr=subprocess.STDOUT, timeout=30)
        results = {}
        commands = [('probe', 'python3 /tmp/stale_fd_probe.py')]
        if not a.probe_only:
            commands += [(name, 'bash /tmp/agentfs/' + name + '.sh') for name in
                         ('test_full', 'test_cross_checkpoint_fd_cow', 'test_deleted_open_resurrect')]
        for name, command in commands:
            with (a.out / (name + '.log')).open('w') as log:
                result = subprocess.run(ssh + [command], stdout=log, stderr=subprocess.STDOUT, timeout=240)
                results[name] = result.returncode
            # Legacy scripts use dmesg -c, so retain each stage before the next
            # script can clear the ring buffer.
            with (a.out / (name + '-dmesg.log')).open('w') as log:
                subprocess.run(ssh + ['dmesg'], stdout=log, stderr=subprocess.STDOUT, timeout=30)
        for name, command in [('trace', 'cat /sys/kernel/debug/tracing/trace'), ('dmesg', 'dmesg')]:
            with (a.out / (name + '.log')).open('w') as log:
                subprocess.run(ssh + [command], stdout=log, stderr=subprocess.STDOUT, timeout=30)
        (a.out / 'results.json').write_text(json.dumps(results, indent=2))
    finally:
        vm.stop_vm(args, process)
raise SystemExit(0 if all(code == 0 for code in results.values()) else 1)
