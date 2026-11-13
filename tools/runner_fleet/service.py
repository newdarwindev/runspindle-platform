"""Observed Strix service controls, fresh preflight and bounded qualification."""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time

from .backend import Backend, ROOT, digest, execute, helper
from .manager import Manager, gh, pages
from .model import empty_report
from .state import State

UNIT = 'runspindle-fleet.service'
CODE = Path('/opt/runspindle-fleet')


def observed(directory):
    state = State(directory)
    service = execute(['systemctl', 'show', UNIT, '--property=ActiveState,SubState,UnitFileState,MainPID'], check=False)
    fields = dict(line.split('=', 1) for line in service.stdout.splitlines() if '=' in line)
    heartbeat = state.get('heartbeat')
    live = fields.get('ActiveState') == 'active' and heartbeat and time.time() - heartbeat < 120
    return {'status': 'passed' if live and not state.get('error') else 'blocked',
            'service': fields, 'heartbeat': heartbeat, 'error': state.get('error'),
            'drained': state.get('drained', True), 'resources': state.active(),
            'guests': [helper('observe', slot) for slot in range(2)],
            'queue': state.get('queue', []), 'reconciled_at': state.get('reconciled_at'),
            'peak_slots': state.get('peak_slots', 0), 'startup_enabled': fields.get('UnitFileState') == 'enabled'}


def preflight(cfg, directory):
    result = {'status': 'blocked', 'host': 'strix', 'checks': []}

    def check(name, function):
        try:
            detail = function()
            if detail is False:
                raise RuntimeError('Probe returned false')
            result['checks'].append({'check': name, 'status': 'passed', 'detail': detail})
        except Exception as error:
            result['checks'].append({'check': name, 'status': 'blocked', 'reason': str(error)})

    check('host', lambda: platform.system() == 'Linux' and platform.machine() == 'x86_64')
    check('tools', lambda: all(shutil.which(tool) for tool in ('qemu-system-x86_64', 'qemu-img', 'cloud-localds', 'ssh', 'gh')))
    check('kvm', lambda: os.access('/dev/kvm', os.R_OK | os.W_OK))
    check('disk', lambda: shutil.disk_usage(ROOT).free >= cfg['hosts']['strix']['minimum_disk_gib'] * 1024**3)
    def memory():
        available = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))) / 1024**2
        required = cfg['hosts']['strix']['reserve_memory_gib'] + 2 * cfg['lanes']['strix-linux-x64']['memory_gib']
        if available < required:
            raise RuntimeError(f'{available:.1f} GiB available; {required} GiB required')
        return {'available_gib': available, 'required_gib': required}
    check('memory', memory)
    def image():
        path = ROOT / 'images/qualified.qcow2'
        manifest = json.loads((ROOT / 'images/qualified.json').read_text())
        if path.stat().st_uid != 0 or path.stat().st_mode & 0o222:
            raise RuntimeError('Base image must be root-owned and read-only')
        if digest(path) != manifest['sha256']:
            raise RuntimeError('Immutable image digest mismatch')
        return manifest
    check('image', image)
    for repo in cfg['repositories']:
        check('admin:' + repo, lambda repo=repo: gh(f'repos/{cfg["owner"]}/{repo}')['permissions']['admin'])
    def background():
        value = execute(['sudo', '-n', 'systemd-run', '--quiet', '--wait', '--pipe', '--collect',
                         '--unit=runspindle-auth-probe', '-p', 'User=standard',
                         '--setenv=HOME=/home/standard',
                         '--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',
                         '/usr/bin/gh', 'api', 'user', '--jq', '.login']).stdout.strip()
        if value != cfg['owner']:
            raise RuntimeError('Service context could not use the existing account')
        return {'account': value, 'credential_replacement': False}
    check('background_credentials', background)
    check('network_rules', lambda: helper('network'))
    if all(c['status'] == 'passed' for c in result['checks']):
        state = State(directory)
        def guest_probe():
            with state.lock():
                if state.active():
                    raise RuntimeError('Drain and stop manager before fresh preflight')
                backend = Backend(state)
                resource = state.reserve({'repository': 'runspindle-platform', 'id': -1})
                try:
                    backend.create(resource)
                    deadline = time.monotonic() + 240
                    while time.monotonic() < deadline:
                        if backend.ready(resource):
                            backend.isolation(resource)
                            return json.loads((backend.directory(resource) / 'isolation.json').read_text())
                        time.sleep(2)
                    raise RuntimeError('Guest boot deadline exceeded')
                finally:
                    state.update(resource, 'cleaning')
                    try:
                        backend.diagnostics(resource)
                        backend.destroy(resource)
                        state.update(resource, 'destroyed', cleanup_verified=True)
                    except Exception as error:
                        state.update(resource, 'uncertain', error=str(error))
                        raise
        check('fresh_guest_isolation', guest_probe)
    if all(c['status'] == 'passed' for c in result['checks']) and any(c['check'] == 'fresh_guest_isolation' for c in result['checks']):
        result['status'] = 'passed'
    result['observed_at'] = time.time()
    State(directory).set('preflight', result)
    return result


def install(cfg, directory):
    report = preflight(cfg, directory)
    if report['status'] != 'passed':
        return report
    directory = Path(directory).resolve()
    if any(character.isspace() for character in str(directory)):
        raise ValueError('Service state path must not contain whitespace')
    content = f'''[Unit]
Description=Runspindle disposable Strix manager
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=standard
Group=standard
SupplementaryGroups=runspindle-fleet
Environment=HOME=/home/standard
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
ExecStart=/usr/bin/python3 {CODE}/tools/fleet.py manage --host strix --state-dir {directory}
Restart=on-failure
RestartSec=10
TimeoutStopSec=30
KillMode=process
UMask=0077
[Install]
WantedBy=multi-user.target
'''
    temporary = Path(directory) / 'manager.service'
    temporary.write_text(content)
    execute(['sudo', '-n', 'install', '-m', '0644', str(temporary), '/etc/systemd/system/' + UNIT])
    execute(['sudo', '-n', 'systemctl', 'daemon-reload'])
    State(directory).set('drained', True)
    execute(['sudo', '-n', 'systemctl', 'start', UNIT])
    return {'status': 'passed', 'action': 'service started drained; startup remains disabled', 'preflight': report}


def control(cfg, directory, command):
    state = State(directory)
    if command == 'drain':
        state.set('drained', True)
    elif command == 'resume':
        report = state.get('preflight', {})
        if report.get('status') != 'passed' or time.time() - report.get('observed_at', 0) > 86400:
            return {'status': 'blocked', 'reason': 'Fresh successful preflight required'}
        if digest(ROOT / 'images/qualified.qcow2') != json.loads((ROOT / 'images/qualified.json').read_text())['sha256']:
            return {'status': 'blocked', 'reason': 'Image changed after preflight'}
        helper('network')
        service = observed(directory)
        if service['status'] != 'passed':
            return service
        state.set('drained', False)
    return observed(directory)


def uninstall(cfg, directory):
    state = State(directory)
    state.set('drained', True)
    execute(['sudo', '-n', 'systemctl', 'stop', UNIT], check=False)
    with state.lock():
        manager = Manager(cfg, directory)
        for resource in state.active():
            manager.cleanup(resource, 'uninstall')
        if state.active():
            return {'status': 'blocked', 'reason': 'Uncertain resources retained', 'resources': state.active()}
        manager.reconcile()
        for slot in range(2):
            helper('stop', slot)
            execute(['sudo', '-n', 'systemctl', 'stop', f'runspindle-proxy-{slot}.service'])
        execute(['sudo', '-n', 'systemctl', 'disable', UNIT], check=False)
        execute(['sudo', '-n', 'rm', '-f', '/etc/systemd/system/' + UNIT])
        execute(['sudo', '-n', 'systemctl', 'daemon-reload'])
    return {'status': 'passed', 'action': 'fleet manager and guests removed; private logs and images retained; isolation rules retained'}


def qualify(cfg, revisions, directory, ref):
    """Dispatch and follow real jobs; never infer live scenario qualification."""
    report = empty_report(cfg, revisions)
    dispatched = {}
    start = time.monotonic()
    for repo, sha in revisions.items():
        full = cfg['owner'] + '/' + repo
        actual = gh(f'repos/{full}/commits/{ref}')['sha']
        if actual != sha:
            raise RuntimeError(f'{repo}: requested ref does not match exact revision')
        before = {r['id'] for r in gh(f'repos/{full}/actions/workflows/fleet-qualification.yml/runs?per_page=30')['workflow_runs']}
        execute(['gh', 'workflow', 'run', 'fleet-qualification.yml', '-R', full, '--ref', ref])
        dispatched[repo] = {'before': before, 'sha': sha, 'id': None}
    deadline = start + cfg['queue_timeout_seconds'] + cfg['job_timeout_seconds']
    while time.monotonic() < deadline:
        terminal = True
        for repo, item in dispatched.items():
            full = cfg['owner'] + '/' + repo
            if item['id'] is None:
                candidates = [r for r in gh(f'repos/{full}/actions/workflows/fleet-qualification.yml/runs?per_page=30')['workflow_runs']
                              if r['id'] not in item['before'] and r['head_sha'] == item['sha'] and r['event'] == 'workflow_dispatch']
                if len(candidates) > 1:
                    raise RuntimeError('Ambiguous concurrent qualification dispatch')
                if candidates:
                    item['id'] = candidates[0]['id']
            if item['id'] is None:
                terminal = False
                continue
            run = gh(f'repos/{full}/actions/runs/{item["id"]}')
            jobs = pages(f'repos/{full}/actions/runs/{item["id"]}/jobs', 'jobs')
            cell = next(c for c in report['coverage'] if c['repository'] == full and c['lane'] == 'strix-linux-x64')
            cell.update({'jobs': jobs, 'run_id': item['id'], 'url': run['html_url']})
            matching = [j for j in jobs if j['name'] == 'strix-linux-x64']
            if len(matching) != 1:
                terminal = False
                continue
            job = matching[0]
            if job['status'] == 'queued' and time.monotonic() - start > cfg['queue_timeout_seconds']:
                cell.update(status='blocked', reason='30-minute queue deadline exceeded')
            elif run['status'] == 'completed':
                from .model import classify_job
                status, reason = classify_job(job | {'head_sha': run['head_sha']}, item['sha'], cfg['lanes']['strix-linux-x64'])
                cell.update(status=status, reason=reason)
                if job['status'] == 'completed':
                    log = execute(['gh', 'api', f'repos/{full}/actions/jobs/{job["id"]}/logs'], timeout=45)
                    path = Path(directory) / f'qualification-{repo}-{job["id"]}.log'
                    path.write_text(log.stdout)
                    path.chmod(0o600)
                    cell['log_path'] = str(path)
            else:
                terminal = False
        path = Path(directory) / 'qualification.json'
        path.write_text(json.dumps(report, indent=2))
        path.chmod(0o600)
        if terminal:
            break
        time.sleep(15)
    # Mac lanes and fault scenarios remain explicitly pending even if all
    # dispatched workloads passed. This is a workload report, not rollout proof.
    report['dispatch_elapsed_seconds'] = time.monotonic() - start
    return report
