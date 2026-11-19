from pathlib import Path
import subprocess,socket
assert socket.gethostname()=='deltabox-ae'
p=Path('/etc/systemd/resolved.conf.d');p.mkdir(exist_ok=True)
(p/'deltabox.conf').write_text('[Resolve]\nDNS=10.0.2.3\n')
subprocess.run(['systemctl','restart','systemd-resolved'],check=True)
p=Path('/etc/systemd/system/cube-sandbox-cube-proxy.service.d');p.mkdir(exist_ok=True)
(p/'appliance-prebuilt.conf').write_text('''[Service]
ExecStart=
ExecStart=/usr/bin/docker compose -f /usr/local/services/cubetoolbox/cubeproxy/docker-compose.yaml up --no-build
''')
# Offline package ships the already built image; startup must not rebuild it from a registry.
subprocess.run(['docker','image','inspect','cube-proxy:one-click'],check=True,stdout=subprocess.DEVNULL)
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','restart','cube-sandbox-coredns','cube-sandbox-cube-proxy'],check=True)
print('Prebuilt Cube proxy starts offline; DNS server declared explicitly.')
