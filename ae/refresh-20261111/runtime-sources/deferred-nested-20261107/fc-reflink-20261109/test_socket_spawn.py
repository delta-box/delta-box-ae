"""Linux preflight: bind a real FC control socket with an overlong TMPDIR.

No guest is booted and no snapshot is taken.
"""
import importlib.util
import json
import os
from pathlib import Path

P=Path(__file__).resolve().parent
for key in ['AE_D_OVERLAY','SPR_PAYLOAD','AE_KERNEL','AE_BASE_XFS']:
    os.environ.setdefault(key,'/unused-in-socket-only-preflight')
os.environ['FIRECRACKER']='/usr/local/bin/firecracker'
long=P/'tmp'/('long-parent-'*10)
long.mkdir(parents=True,exist_ok=True)
os.environ['TMPDIR']=str(long)
spec=importlib.util.spec_from_file_location('fc_dm_pilot',P/'vendor/finalbench/fc_diff_dm/fc_dm_pilot.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
vm=m.FirecrackerVM(api_sock=long/'api.sock',log_path=P/'socket-preflight.log',root_dev='/unused',mem_mib=8192,vcpus=4)
try:
    vm.spawn()
    assert len(os.fsencode(vm.api_sock))<100
    reply=json.loads(vm.api('GET','machine-config'))
    result=dict(ok=True,requested=str(vm.requested_api_sock),actual=str(vm.api_sock),
                actual_bytes=len(os.fsencode(vm.api_sock)),api_reply=reply,guest_booted=False)
finally:
    vm.kill()
(P/'socket-preflight.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
