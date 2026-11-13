#!/bin/bash
# Execute inside a disposable preparation guest, never on the host.
set -euo pipefail
set -a
. /etc/environment
set +a
sudo sed -i 's,http://,https://,g' /etc/apt/sources.list.d/ubuntu.sources
sudo -E env DEBIAN_FRONTEND=noninteractive apt-get update
sudo -E env DEBIAN_FRONTEND=noninteractive apt-get install -y python3 git curl ca-certificates build-essential libicu74 libssl3t64 zlib1g libkrb5-3 nodejs npm
mkdir -p /home/runner/runner /home/runner/preparation
cd /home/runner/preparation
curl -fL --retry 2 https://github.com/actions/runner/releases/download/v2.338.2/actions-runner-linux-x64-2.338.2.tar.gz -o runner.tar.gz
echo 'baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40  runner.tar.gz' | sha256sum -c -
tar -xzf runner.tar.gz -C /home/runner/runner
curl -fL --retry 2 https://static.rust-lang.org/dist/rust-1.93.0-x86_64-unknown-linux-gnu.tar.xz -o rust.tar.xz
echo 'b9d9f01a96a2542852ccfddd82194276ba1c86bc76353309ff636b737fc0a772  rust.tar.xz' | sha256sum -c -
tar -xf rust.tar.xz
sudo ./rust-1.93.0-x86_64-unknown-linux-gnu/install.sh --prefix=/opt/rust --disable-ldconfig
for file in /opt/rust/bin/*; do sudo ln -sf "$file" /usr/local/bin/; done
sudo mkdir -p /usr/share/runspindle/licenses
sudo cp rust-1.93.0-x86_64-unknown-linux-gnu/{COPYRIGHT,LICENSE-APACHE,LICENSE-MIT} /usr/share/runspindle/licenses/
curl -fL --retry 2 https://nodejs.org/dist/v22.23.5/node-v22.23.5-linux-x64.tar.xz -o node.tar.xz
echo 'cdeb8670a0c81f51b4dc4ac61919f9b1ad917b304bf8221eb939570663d240f8  node.tar.xz' | sha256sum -c -
sudo mkdir -p /opt/node
sudo tar -xJf node.tar.xz --strip-components=1 -C /opt/node
for file in /opt/node/bin/*; do sudo ln -sf "$file" /usr/local/bin/; done
python3 - <<'PY' | sudo tee /etc/runspindle-image.json >/dev/null
import hashlib, json, pathlib, subprocess
packages=subprocess.check_output(['dpkg-query','-W','-f=${Package}\t${Version}\n'],text=True)
licenses={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path('/usr/share/doc').glob('*/copyright') if p.is_file()}
versions={name:subprocess.check_output([name,'--version'],text=True).strip() for name in ['python3','git','rustc','cargo','node']}
print(json.dumps({'schema_version':1,'identity':'strix-ubuntu24.04-runner2.338.2-rust1.93.0-v1','versions':versions,'packages':packages.splitlines(),'license_sha256':licenses,'runner_sha256':'baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40','ubuntu_sha256':'04e8f2461a16a44e78a370284471f29616cdd9baf1893df25807b71f985b6c94','rust_sha256':'b9d9f01a96a2542852ccfddd82194276ba1c86bc76353309ff636b737fc0a772'},sort_keys=True))
PY
# Verify the offline Rust components without pulling any crate.
mkdir -p /tmp/offline-probe/src
printf '[package]\nname="offline-probe"\nversion="0.0.0"\nedition="2021"\n' > /tmp/offline-probe/Cargo.toml
printf 'pub fn ready() -> bool { true }\n' > /tmp/offline-probe/src/lib.rs
cd /tmp/offline-probe
cargo test --offline
cargo clippy --offline -- -D warnings
sudo rm -rf /home/runner/preparation /tmp/offline-probe
sudo apt-get clean
sudo systemctl disable apt-daily.timer apt-daily-upgrade.timer
bash /opt/runspindle-image-tools/tools/runner_fleet/prepare_temporal.sh
