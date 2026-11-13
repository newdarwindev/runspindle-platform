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
sudo -E python3 /opt/runspindle-image-tools/tools/temporal_lane.py prepare --directory /opt/runspindle-temporal --architecture amd64
sudo -E python3 /opt/runspindle-image-tools/tools/temporal_lane.py smoke --directory /opt/runspindle-temporal
python3 - <<'PY' | sudo tee /etc/runspindle-image.json >/dev/null
import hashlib,json,pathlib,subprocess
v={'schema_version':1, 'ubuntu_sha256':'04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94','runner_sha256':'baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40','rust_sha256':'b9d9f01a96a2542852ccfddd82194276ba1c86bc76353309ff636b737fc0a772'}
v['versions']={n:subprocess.check_output([n,'--version'],text=True).strip() for n in ['python3','git','rustc','cargo']}
v['identity']='strix-ubuntu24.04-runner2.338.2-rust1.93.0-node22.23.5-temporal1.10.0-v2'
v['versions'].update({n:subprocess.check_output([n,'--version'],text=True).strip() for n in ['node','npm','docker']})
v['packages']=subprocess.check_output(['dpkg-query','-W','-f=${Package}\t${Version}\n'],text=True).splitlines()
v['license_sha256']={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path('/usr/share/doc').glob('*/copyright') if p.is_file()}
v['temporal']=json.loads(pathlib.Path('/opt/runspindle-temporal/prepared.json').read_text())
print(json.dumps(v,sort_keys=True))
PY
sudo -E env RUNSPINDLE_TEMPORAL_PREPARED=/opt/runspindle-temporal python3 -m unittest discover -s /opt/runspindle-image-tools/tests -v
sudo rm -rf /tmp/debugbuild
sudo apt-get clean
printf 'RUNSPINDLE_IMAGE='; cat /etc/runspindle-image.json
sudo cloud-init clean --logs --machine-id
sudo rm -f /etc/ssh/ssh_host_* /home/runner/.ssh/authorized_keys
sudo shutdown -h now
