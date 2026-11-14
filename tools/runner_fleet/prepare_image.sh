#!/bin/bash
# Execute inside a disposable preparation guest, never on the host.
set -euo pipefail
set -a
. /etc/environment
set +a
PYTHONPATH=/opt/runspindle-image-tools/tools python3 - <<'PY'
from pathlib import Path
from runner_fleet.staging import verify
print(verify(Path('/opt/runspindle-image-tools'), Path('/home/runner/preparation')))
PY
sudo sed -i 's,http://,https://,g' /etc/apt/sources.list.d/ubuntu.sources
sudo -E env DEBIAN_FRONTEND=noninteractive apt-get update
sudo -E env DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv git curl ca-certificates build-essential libicu74 libssl3t64 zlib1g libkrb5-3
mkdir -p /home/runner/runner /home/runner/preparation
cd /home/runner/preparation
echo 'baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40  actions-runner-linux-x64-2.338.2.tar.gz' | sha256sum -c -
tar -xzf actions-runner-linux-x64-2.338.2.tar.gz -C /home/runner/runner
echo '294b3d81fa72e62581276290c60c81eb8b58498d333d422ca1dfc432877d0c40  rust-1.94.1-x86_64-unknown-linux-gnu.tar.xz' | sha256sum -c -
tar -xf rust-1.94.1-x86_64-unknown-linux-gnu.tar.xz
sudo ./rust-1.94.1-x86_64-unknown-linux-gnu/install.sh --prefix=/opt/rust --disable-ldconfig
for file in /opt/rust/bin/*; do sudo ln -sf "$file" /usr/local/bin/; done
sudo mkdir -p /usr/share/runspindle/licenses
sudo cp rust-1.94.1-x86_64-unknown-linux-gnu/{COPYRIGHT,LICENSE-APACHE,LICENSE-MIT} /usr/share/runspindle/licenses/
echo 'cdeb8670a0c81f51b4dc4ac61919f9b1ad917b304bf8221eb939570663d240f8  node.tar.xz' | sha256sum -c -
sudo mkdir -p /opt/node
sudo tar -xJf node.tar.xz --strip-components=1 -C /opt/node
for file in /opt/node/bin/*; do sudo ln -sf "$file" /usr/local/bin/; done
sudo python3 -m venv /opt/runspindle-quality
sudo /opt/runspindle-quality/bin/pip install --no-index --find-links=/home/runner/preparation/quality-wheels -r /home/runner/preparation/requirements-quality.txt
sudo mkdir -p /opt/runspindle-quality-node
sudo tar -xzf quality-node-modules.tar.gz -C /opt/runspindle-quality-node
python3 - <<'PY' | sudo tee /etc/runspindle-image.json >/dev/null
import hashlib, json, pathlib, subprocess
packages=subprocess.check_output(['dpkg-query','-W','-f=${Package}\t${Version}\n'],text=True)
licenses={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path('/usr/share/doc').glob('*/copyright') if p.is_file()}
versions={name:subprocess.check_output([name,'--version'],text=True).strip() for name in ['python3','git','rustc','cargo','node']}
print(json.dumps({'schema_version':1,'identity':'strix-ubuntu24.04-runner2.338.2-rust1.94.1-quality-v1','versions':versions,'packages':packages.splitlines(),'license_sha256':licenses,'runner_sha256':'baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40','ubuntu_sha256':'04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94','rust_sha256':'294b3d81fa72e62581276290c60c81eb8b58498d333d422ca1dfc432877d0c40'},sort_keys=True))
PY
mkdir -p /home/runner/.cargo /home/runner/preparation/core /home/runner/preparation/host
tar -xzf cargo-closure.tar.gz -C /home/runner/.cargo
tar -xzf core-source.tar.gz -C /home/runner/preparation/core
tar -xzf host-source.tar.gz -C /home/runner/preparation/host
python3 - <<'PY'
import hashlib,json,pathlib
root=pathlib.Path('/home/runner/preparation')
pins=json.loads(pathlib.Path('/opt/runspindle-image-tools/fleet/rust-closure.json').read_text())
for name in ('core','host'):
    value=hashlib.sha256((root/name/'Cargo.lock').read_bytes()).hexdigest()
    if value != pins[name]['lock_sha256']:
        raise SystemExit('locked Rust source changed')
PY
for name in host core; do
  cd "/home/runner/preparation/$name"
  CARGO_NET_OFFLINE=true cargo test --offline --locked
  CARGO_NET_OFFLINE=true cargo clippy --offline --locked --all-targets -- -D warnings
done
python3 - <<'PY' | sudo tee /opt/runspindle-rust-dependencies.json >/dev/null
import hashlib, json, os, pathlib, subprocess, tomllib
root = pathlib.Path('/home/runner/preparation')
crates = []
for source in pathlib.Path('/home/runner/.cargo/registry/src').glob('*/*'):
    manifest = source / 'Cargo.toml'
    if manifest.is_file():
        package = tomllib.loads(manifest.read_text()).get('package', {})
        crates.append({'name': package.get('name'), 'version': package.get('version'),
                       'license': package.get('license'), 'license_file': package.get('license-file')})
archives = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in pathlib.Path('/home/runner/.cargo/registry/cache').glob('*/*.crate')}
projects = {}
for name in ('core', 'host'):
    path = root / name
    projects[name] = {'commit': json.loads(pathlib.Path('/opt/runspindle-image-tools/fleet/rust-closure.json').read_text())[name]['commit'],
                      'lock_sha256': hashlib.sha256((path / 'Cargo.lock').read_bytes()).hexdigest(),
                      'packages': tomllib.loads((path / 'Cargo.lock').read_text())['package']}
print(json.dumps({'schema_version': 1, 'projects': projects, 'crates': sorted(crates, key=lambda x:(x['name'], x['version'])),
                  'archive_sha256': archives}, sort_keys=True))
PY
cd /home/runner
sudo apt-get clean
sudo systemctl disable apt-daily.timer apt-daily-upgrade.timer
bash /opt/runspindle-image-tools/tools/runner_fleet/prepare_temporal.sh
sudo rm -rf /home/runner/preparation
