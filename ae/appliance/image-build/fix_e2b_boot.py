from pathlib import Path
import subprocess,socket
assert socket.gethostname()=='deltabox-ae'
P=Path('/etc/systemd/system')
(P/'deltabox-e2b-deps.service').write_text('''[Unit]
Description=Start the preinstalled local E2B database and Redis
Requires=docker.service
After=docker.service
Before=ae-e2b-api.service ae-e2b-orchestrator.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/docker start ae-e2b-postgres ae-e2b-redis
[Install]
WantedBy=multi-user.target
''')
for name in ['ae-e2b-api','ae-e2b-orchestrator']:
 d=P/(name+'.service.d');d.mkdir(exist_ok=True)
 (d/'appliance-dependencies.conf').write_text('[Unit]\nRequires=deltabox-e2b-deps.service\nAfter=deltabox-e2b-deps.service\n')
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable','--now','deltabox-e2b-deps.service'],check=True)
subprocess.run(['systemctl','restart','ae-e2b-orchestrator','ae-e2b-api'],check=True)
print('E2B database/Redis boot dependency installed')
