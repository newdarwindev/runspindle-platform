#!/bin/bash
set -euo pipefail
set -a
. /etc/environment
set +a
sudo sed -i 's,http://,https://,g' /etc/apt/sources.list.d/ubuntu.sources
sudo -E env DEBIAN_FRONTEND=noninteractive apt-get update
sudo -E env DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-buildx
sudo systemctl enable --now docker
sudo usermod -aG docker runner
sudo install -d -m 0755 /opt/runspindle-temporal
sudo install -m 0644 /home/runner/preparation/temporal_cli_1.10.0_linux_amd64.tar.gz /opt/runspindle-temporal/
sudo install -m 0644 /home/runner/preparation/LICENSE-CLI /opt/runspindle-temporal/LICENSE
sudo -H -E python3 /opt/runspindle-image-tools/tools/temporal_lane.py prepare --directory /opt/runspindle-temporal --architecture amd64 --offline
sudo -H -E python3 /opt/runspindle-image-tools/tools/temporal_lane.py smoke --directory /opt/runspindle-temporal
python3 - <<'PY' | sudo tee /etc/runspindle-image.json >/dev/null
import hashlib,json,pathlib,subprocess
v={'schema_version':1, 'ubuntu_sha256':'04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94','runner_sha256':'baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40','rust_sha256':'294b3d81fa72e62581276290c60c81eb8b58498d333d422ca1dfc432877d0c40'}
v['versions']={n:subprocess.check_output([n,'--version'],text=True).strip() for n in ['python3','git','rustc','cargo']}
v['identity']='strix-ubuntu24.04-runner2.338.2-rust1.94.1-node22.23.5-temporal1.10.0-quality-v1'
v['versions'].update({n:subprocess.check_output([n,'--version'],text=True).strip() for n in ['node','npm','docker']})
v['packages']=subprocess.check_output(['dpkg-query','-W','-f=${Package}\t${Version}\n'],text=True).splitlines()
v['license_sha256']={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path('/usr/share/doc').glob('*/copyright') if p.is_file()}
v['temporal']=json.loads(pathlib.Path('/opt/runspindle-temporal/prepared.json').read_text())
v['rust_dependencies']=json.loads(pathlib.Path('/opt/runspindle-rust-dependencies.json').read_text())
v['quality_closure']=json.loads(pathlib.Path('/opt/runspindle-image-tools/fleet/quality-closure.json').read_text())
v['rust_closure_sha256']=hashlib.sha256(pathlib.Path('/opt/runspindle-image-tools/fleet/rust-closure.json').read_bytes()).hexdigest()
v['quality_python_packages']=subprocess.check_output(['/opt/runspindle-quality/bin/pip','freeze'],text=True).splitlines()
v['quality_node_archive_sha256']=v['quality_closure']['node']['modules_archive_sha256']
v['quality_license_sha256']={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for base in [pathlib.Path('/opt/runspindle-quality/lib'),pathlib.Path('/opt/runspindle-quality-node/node_modules')] for p in base.rglob('*') if p.is_file() and p.name.upper().startswith(('LICENSE','COPYING','NOTICE'))}
v['identity'] += '-rust-closure'
print(json.dumps(v,sort_keys=True))
PY
sudo -E env RUNSPINDLE_TEMPORAL_PREPARED=/opt/runspindle-temporal python3 -m unittest discover -s /opt/runspindle-image-tools/tests -v
sudo rm -rf /tmp/debugbuild
sudo chown -R runner:runner /home/runner/.docker 2>/dev/null || true
sudo apt-get clean
printf 'RUNSPINDLE_IMAGE='; cat /etc/runspindle-image.json
sudo cloud-init clean --logs --machine-id
sudo rm -f /etc/ssh/ssh_host_* /home/runner/.ssh/authorized_keys
sudo shutdown -h now
