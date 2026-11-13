#!/usr/bin/env python3
"""Root-owned fixed-scope Strix helper. Install under /opt, never run from guest storage.

Only two fixed slots and fleet-owned units/interfaces are accepted. No caller
supplied paths, shell fragments, QEMU flags or service names cross this boundary.
"""
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys

ROOT = Path('/var/lib/runspindle-fleet')
CODE = Path('/opt/runspindle-fleet')


def run(*argv, check=True, **kwargs):
    return subprocess.run(argv, check=check, text=True, capture_output=True, timeout=60, **kwargs)


def unit(slot):
    return f'runspindle-vm-{slot}.service'


def network():
    rules = ['table inet runspindle_fleet {',
             'chain input { type filter hook input priority -20; policy accept;']
    for slot in range(2):
        interface = f'rsvm{slot}'
        subnet = f'10.231.{slot}'
        # These are dedicated fleet TAPs, never bridge existing infrastructure.
        if run('ip', 'link', 'show', interface, check=False).returncode:
            run('ip', 'tuntap', 'add', 'dev', interface, 'mode', 'tap', 'user', f'runspindle-vm{slot}')
        else:
            link = json.loads(run('ip', '-j', '-d', 'link', 'show', interface).stdout)[0]
            if link.get('linkinfo', {}).get('info_kind') != 'tun':
                raise RuntimeError('Fleet interface name occupied by unrelated resource')
        run('ip', 'addr', 'replace', f'{subnet}.1/30', 'dev', interface)
        run('ip', 'link', 'set', interface, 'up')
        rules += [f'iifname "{interface}" meta nfproto ipv6 drop',
                  f'iifname "{interface}" ip saddr != {subnet}.2 drop',
                  f'iifname "{interface}" ct state established,related accept',
                  f'iifname "{interface}" ip daddr {subnet}.1 tcp dport 3128 accept',
                  f'iifname "{interface}" counter drop']
    rules += ['}', 'chain forward { type filter hook forward priority -20; policy accept;',
              'iifname { "rsvm0", "rsvm1" } counter drop',
              'oifname { "rsvm0", "rsvm1" } counter drop', '}', '}']
    exists = run('nft', 'list', 'table', 'inet', 'runspindle_fleet', check=False).returncode == 0
    script = ('delete table inet runspindle_fleet\n' if exists else '') + '\n'.join(rules) + '\n'
    run('nft', '-f', '-', input=script)
    # Reachable synthetic LAN namespace, outside both guest subnets. A host-side
    # reachability probe distinguishes filtering from a nonexistent canary.
    if run('ip', 'netns', 'list').stdout.find('runspindle-canary') < 0:
        run('ip', 'netns', 'add', 'runspindle-canary')
        run('ip', 'link', 'add', 'rscan0', 'type', 'veth', 'peer', 'name', 'rscan1')
        run('ip', 'link', 'set', 'rscan1', 'netns', 'runspindle-canary')
        run('ip', 'addr', 'add', '198.18.231.1/30', 'dev', 'rscan0')
        run('ip', 'link', 'set', 'rscan0', 'up')
        run('ip', '-n', 'runspindle-canary', 'addr', 'add', '198.18.231.2/30', 'dev', 'rscan1')
        run('ip', '-n', 'runspindle-canary', 'link', 'set', 'rscan1', 'up')
        run('ip', '-n', 'runspindle-canary', 'link', 'set', 'lo', 'up')
        run('ip', '-n', 'runspindle-canary', 'route', 'add', 'default', 'via', '198.18.231.1')
    for slot in range(2):
        run('systemctl', 'start', f'runspindle-proxy-{slot}.service')
    run('systemctl', 'start', 'runspindle-host-canary.service', 'runspindle-lan-canary.service')


def observe(slot):
    result = run('systemctl', 'show', unit(slot), '--property=ActiveState,SubState,MainPID,ExecMainStatus,ControlGroup,MemoryPeak,CPUUsageNSec', check=False)
    fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    fields['slot'] = slot
    return fields


def start(slot):
    directory = ROOT / 'slots' / str(slot)
    account = pwd.getpwnam(f'runspindle-vm{slot}')
    state = observe(slot)
    if state.get('ActiveState') in ('active', 'activating', 'deactivating'):
        raise RuntimeError('Slot already has a service; reconcile before boot')
    for name in ('disk.qcow2', 'seed.iso'):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('Missing or linked slot input')
        os.chown(path, account.pw_uid, account.pw_gid)
        os.chmod(path, 0o600)
    os.chown(directory, account.pw_uid, account.pw_gid)
    os.chmod(directory, 0o770)
    run('systemd-run', '--quiet', '--collect', '--unit=' + unit(slot),
        '-p', 'User=' + account.pw_name, '-p', 'Group=runspindle-fleet',
        '-p', 'SupplementaryGroups=kvm', '-p', 'ProtectHome=yes',
        '-p', 'ProtectSystem=strict', '-p', 'PrivateTmp=yes', '-p', 'NoNewPrivileges=yes',
        '-p', 'ReadWritePaths=' + str(directory), '-p', 'KillMode=control-group',
        '-p', 'TimeoutStopSec=20', '-p', 'RuntimeMaxSec=1500',
        '-p', 'MemoryMax=7G', '-p', 'TasksMax=128',
        '/usr/bin/qemu-system-x86_64', '-name', f'runspindle-slot-{slot}',
        '-enable-kvm', '-machine', 'q35,mem-merge=off', '-cpu', 'host',
        '-smp', '4', '-m', '6144', '-nodefaults', '-no-user-config',
        '-display', 'none', '-monitor', 'none', '-serial', 'file:' + str(directory / 'serial.log'),
        '-drive', f'file={directory}/disk.qcow2,format=qcow2,if=virtio,cache=none',
        '-drive', f'file={directory}/seed.iso,format=raw,media=cdrom,readonly=on',
        '-netdev', f'tap,id=net0,ifname=rsvm{slot},script=no,downscript=no',
        '-device', 'virtio-net-pci,netdev=net0,mac=52:54:00:23:10:0' + str(slot),
        '-sandbox', 'on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny')


def stop(slot):
    run('systemctl', 'stop', unit(slot), check=False)
    state = observe(slot)
    if state.get('ActiveState') not in ('inactive', 'failed', None) or int(state.get('MainPID', 0)) != 0:
        raise RuntimeError('Guest service termination uncertain')
    run('systemctl', 'reset-failed', unit(slot), check=False)
    # Ownership returns only after the entire service cgroup has stopped.
    directory = ROOT / 'slots' / str(slot)
    operator = pwd.getpwnam('standard')
    for name in ('disk.qcow2', 'seed.iso', 'serial.log'):
        path = directory / name
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            os.chown(path, operator.pw_uid, pwd.getpwnam(f'runspindle-vm{slot}').pw_gid)
            os.chmod(path, 0o600)
    os.chown(directory, operator.pw_uid, pwd.getpwnam(f'runspindle-vm{slot}').pw_gid)


def main():
    if os.geteuid() != 0:
        raise SystemExit('Requires root-owned installation')
    command = sys.argv[1]
    if command == 'network' and len(sys.argv) == 2:
        network()
        print(json.dumps({'status': 'applied'}))
        return
    if command not in ('start', 'stop', 'observe') or len(sys.argv) != 3 or sys.argv[2] not in ('0', '1'):
        raise SystemExit('Unsupported fixed-scope operation')
    slot = int(sys.argv[2])
    if command == 'start':
        start(slot)
    elif command == 'stop':
        stop(slot)
    print(json.dumps(observe(slot)))


if __name__ == '__main__':
    main()
