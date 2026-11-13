#!/usr/bin/env python3
"""Install fixed-scope fleet plumbing. Starts proxies/canaries, never the manager.

Run as root from a reviewed source checkout on Strix. Existing services, firewall
tables, keyrings and credentials are preserved. No units are enabled at boot.
"""
import grp
import os
from pathlib import Path
import pwd
import shutil
import subprocess

SOURCE = Path(__file__).resolve().parents[2]
CODE = Path('/opt/runspindle-fleet')
ROOT = Path('/var/lib/runspindle-fleet')


def run(*args):
    subprocess.run(args, check=True, timeout=60)


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run reviewed bootstrap as root')
    try:
        grp.getgrnam('runspindle-fleet')
    except KeyError:
        run('groupadd', '--system', 'runspindle-fleet')
    for name in ('runspindle-vm0', 'runspindle-vm1', 'runspindle-proxy'):
        try:
            pwd.getpwnam(name)
        except KeyError:
            run('useradd', '--system', '--gid', 'runspindle-fleet', '--no-create-home',
                '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', name)
    run('usermod', '-aG', 'runspindle-fleet', 'standard')
    CODE.mkdir(mode=0o755, exist_ok=True)
    for relative in ('tools', 'fleet', 'temporal'):
        shutil.copytree(SOURCE / relative, CODE / relative, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for path in CODE.rglob('*'):
        os.chown(path, 0, 0)
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    ROOT.mkdir(mode=0o755, exist_ok=True)
    (ROOT / 'images').mkdir(mode=0o755, exist_ok=True)
    (ROOT / 'slots').mkdir(mode=0o755, exist_ok=True)
    operator = pwd.getpwnam('standard')
    for slot in range(2):
        path = ROOT / 'slots' / str(slot)
        if not path.exists():
            path.mkdir(mode=0o770)
            os.chown(path, operator.pw_uid, grp.getgrnam('runspindle-fleet').gr_gid)
        content = f'''[Unit]
Description=Runspindle restricted egress slot {slot}
[Service]
User=runspindle-proxy
Group=runspindle-fleet
ExecStart=/usr/bin/python3 {CODE}/tools/runner_fleet/proxy.py {CODE}/fleet/egress.json 10.231.{slot}.1
Restart=on-failure
NoNewPrivileges=yes
ProtectHome=yes
ProtectSystem=strict
PrivateTmp=yes
TasksMax=256
MemoryMax=256M
UMask=0077
'''
        Path(f'/etc/systemd/system/runspindle-proxy-{slot}.service').write_text(content)
    for name, args in [('host', '0.0.0.0 18081'), ('lan', '198.18.231.2 18082')]:
        prefix = '/usr/bin/ip netns exec runspindle-canary ' if name == 'lan' else ''
        Path(f'/etc/systemd/system/runspindle-{name}-canary.service').write_text(f'''[Unit]
Description=Runspindle synthetic {name} canary
[Service]
ExecStart={prefix}/usr/bin/python3 {CODE}/tools/runner_fleet/canary.py {args}
Restart=on-failure
ProtectHome=yes
ProtectSystem=strict
PrivateTmp=yes
''')
    run('systemctl', 'daemon-reload')
    run('python3', str(CODE / 'tools/runner_fleet/host_helper.py'), 'network')


if __name__ == '__main__':
    main()
