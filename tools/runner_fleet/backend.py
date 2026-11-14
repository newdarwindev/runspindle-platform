"""Disposable QEMU lifecycle. All external mutations follow a durable reservation."""
import base64
import hashlib
import json
import os
import re
import resource as process_resource
import tempfile
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import time

ROOT = Path('/var/lib/runspindle-fleet')
HELPER = '/opt/runspindle-fleet/tools/runner_fleet/host_helper.py'


def execute(argv, *, timeout=60, input=None, check=True, output_limit=None):
    if output_limit is None:
        result = subprocess.run(argv, input=input, text=True, capture_output=True, timeout=timeout)
    else:
        # Guest-controlled stdout must not become unbounded host memory/disk.
        # RLIMIT_FSIZE applies to the host SSH process, never guest workloads.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            def bounded_files():
                process_resource.setrlimit(process_resource.RLIMIT_FSIZE, (output_limit, output_limit))
            completed = subprocess.run(argv, input=input, text=True, stdout=stdout,
                                       stderr=stderr, timeout=timeout, preexec_fn=bounded_files)
            if max(os.fstat(stdout.fileno()).st_size, os.fstat(stderr.fileno()).st_size) >= output_limit:
                raise RuntimeError('Guest diagnostic output limit exceeded')
            stdout.seek(0)
            stderr.seek(0)
            result = subprocess.CompletedProcess(argv, completed.returncode,
                stdout.read(output_limit).decode('utf-8', errors='replace'),
                stderr.read(output_limit).decode('utf-8', errors='replace'))
    if check and result.returncode:
        # Never embed input (registration token) or argv in an exception.
        raise RuntimeError(f'{Path(argv[0]).name} failed ({result.returncode}): {result.stderr[-2000:]}')
    return result


def helper(command, slot=None, identity=None):
    argv = ['sudo', '-n', 'python3', HELPER, command]
    if slot is not None:
        argv.append(str(slot))
    if identity is not None:
        argv.append(identity)
    return json.loads(execute(argv).stdout)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


class Backend:
    def __init__(self, state, image=None):
        self.state = state
        self.image = Path(image or ROOT / 'images/qualified.qcow2')

    def directory(self, resource):
        path = self.state.directory / 'resources' / resource['id']
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    def slot(self, resource):
        return ROOT / 'slots' / str(resource['slot'])

    def ssh(self, resource, script, timeout=30, check=True):
        directory = self.directory(resource)
        return execute(['ssh', '-F', '/dev/null', '-i', str(directory / 'key'),
                        '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                        '-o', 'StrictHostKeyChecking=accept-new',
                        '-o', 'UserKnownHostsFile=' + str(directory / 'known_hosts'),
                        '-o', 'ConnectTimeout=3', '-o', 'ForwardAgent=no',
                        '-o', 'ClearAllForwardings=yes',
                        f'runner@10.231.{resource["slot"]}.2', 'bash', '-s'],
                       input=script, timeout=timeout, check=check, output_limit=32 * 1024**2)

    def copy_prepared(self, resource, source, relative):
        """Transfer one preverified archive through the guest's existing SSH identity."""
        if not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', relative):
            raise ValueError('unsafe preparation destination')
        source = Path(source)
        if not source.is_file() or source.is_symlink():
            raise ValueError('missing preparation archive')
        directory = self.directory(resource)
        target = '/home/runner/preparation/' + relative
        parent = target.rsplit('/', 1)[0]
        self.ssh(resource, 'mkdir -p ' + shlex.quote(parent), timeout=15)
        result = execute(['scp', '-F', '/dev/null', '-i', str(directory / 'key'),
                          '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                          '-o', 'StrictHostKeyChecking=yes',
                          '-o', 'UserKnownHostsFile=' + str(directory / 'known_hosts'),
                          '-o', 'ConnectTimeout=3', '-o', 'ClearAllForwardings=yes',
                          str(source), f'runner@10.231.{resource["slot"]}.2:{target}'],
                         timeout=600, check=False)
        if result.returncode:
            raise RuntimeError('verified preparation transfer failed')

    def create(self, resource, prepare=False):
        directory = self.directory(resource)
        slot = self.slot(resource)
        image_sha = digest(self.image)
        self.state.update(resource, 'starting', image_sha256=image_sha, boot_deadline=time.time() + 240)
        helper('network')  # Firewall applied before any guest process exists.
        observed = helper('observe', resource['slot'])
        if observed.get('ActiveState') in ('active', 'activating', 'deactivating'):
            raise RuntimeError('Unreconciled occupied slot')
        execute(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(directory / 'key')])
        if (slot / 'disk.qcow2').exists() or (slot / 'seed.iso').exists():
            raise RuntimeError('Slot contains unreconciled files')
        execute(['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', str(self.image), str(slot / 'disk.qcow2'), '24G'])
        proxy = f'http://10.231.{resource["slot"]}.1:3128'
        cloud = {
            'hostname': 'disposable-' + resource['id'][:12], 'manage_etc_hosts': True,
            'ssh_pwauth': False, 'disable_root': True, 'apt': {'preserve_sources_list': True},
            'users': [{'name': 'runner', 'shell': '/bin/bash', 'lock_passwd': True,
                       'sudo': ['ALL=(ALL) NOPASSWD:ALL'] if prepare else [],
                       'ssh_authorized_keys': [(directory / 'key.pub').read_text().strip()]}],
            'write_files': [{'path': '/etc/runspindle-instance.json', 'content': json.dumps({'resource': resource['id'], 'slot': resource['slot'], 'image_sha256': image_sha})}, {'path': '/etc/environment', 'content':
                f'http_proxy={proxy}\nhttps_proxy={proxy}\nHTTP_PROXY={proxy}\nHTTPS_PROXY={proxy}\nno_proxy=localhost,127.0.0.1\nNO_PROXY=localhost,127.0.0.1\n'}],
            'runcmd': [['sysctl', '-w', 'net.ipv6.conf.all.disable_ipv6=1']],
        }
        if prepare:
            source = Path(__file__).resolve().parents[2]
            for relative in ('tools/temporal_lane.py', 'temporal/dependencies.json', 'temporal/LICENSE-CLI', 'temporal/LICENSE-SERVER', 'tools/runner_fleet/prepare_temporal.sh', 'tests/test_temporal_lane.py', 'fleet/image-inputs.json', 'fleet/quality-closure.json', 'fleet/rust-closure.json', 'requirements-quality.txt', 'tools/runner_fleet/quality.py', 'tools/runner_fleet/staging.py', 'tools/runner_fleet/__init__.py'):
                cloud['write_files'].append({'path': '/opt/runspindle-image-tools/' + relative,
                                             'content': (source / relative).read_text(), 'permissions': '0644'})
        network = {'version': 2, 'ethernets': {'fleet': {
            'match': {'macaddress': '52:54:00:23:10:0' + str(resource['slot'])},
            'set-name': 'fleet0', 'addresses': [f'10.231.{resource["slot"]}.2/30'],
            'routes': [{'to': 'default', 'via': f'10.231.{resource["slot"]}.1'}],
            'dhcp4': False, 'dhcp6': False, 'accept-ra': False}}}
        (directory / 'user-data').write_text('#cloud-config\n' + json.dumps(cloud))
        (directory / 'meta-data').write_text(json.dumps({'instance-id': resource['id']}))
        (directory / 'network-config').write_text(json.dumps(network))
        execute(['cloud-localds', '--network-config=' + str(directory / 'network-config'),
                 str(slot / 'seed.iso'), str(directory / 'user-data'), str(directory / 'meta-data')])
        helper('start', resource['slot'], resource['id'])

    def ready(self, resource):
        result = self.ssh(resource, 'test -f /var/lib/cloud/instance/boot-finished\n', timeout=10, check=False)
        return result.returncode == 0

    def isolation(self, resource, check_tooling=True):
        # Both canaries must be alive from host before denial is meaningful.
        for address, port in [(f'10.231.{resource["slot"]}.1', 18081), ('198.18.231.2', 18082)]:
            with socket.create_connection((address, port), timeout=3):
                pass
        script = '''python3 - <<'GUEST'
import json, os, pathlib, socket
checks = {}
for name, address, port in [
    ('host', 'GATEWAY', 18081), ('lan', '198.18.231.2', 18082),
    ('management', 'GATEWAY', 22), ('peer', 'PEER', 22),
    ('internet', '1.1.1.1', 443), ('dns', '1.1.1.1', 53)]:
    try:
        with socket.create_connection((address, port), timeout=1): pass
        checks[name] = False
    except OSError:
        checks[name] = True
mounts = pathlib.Path('/proc/mounts').read_text()
checks['no_host_mounts'] = not any(x in mounts for x in ['9p', 'virtiofs', '/Users/', '/home/standard'])
checks['no_management_credentials'] = not any(pathlib.Path(x).exists() for x in [
 '/home/runner/.config/gh/hosts.yml', '/home/runner/.ssh/id_ed25519'])
checks['fresh_workdir'] = not pathlib.Path('/home/runner/runner/_work').exists()
checks['ipv6_disabled'] = pathlib.Path('/proc/sys/net/ipv6/conf/all/disable_ipv6').read_text().strip() == '1'
print(json.dumps(checks))
raise SystemExit(0 if all(checks.values()) else 2)
GUEST
'''.replace('GATEWAY', f'10.231.{resource["slot"]}.1').replace('PEER', f'10.231.{1-resource["slot"]}.2')
        result = self.ssh(resource, script, timeout=25, check=False)
        (self.directory(resource) / 'isolation.json').write_text(result.stdout)
        if result.returncode:
            raise RuntimeError('Guest isolation or freshness probe failed')
        checks = json.loads(result.stdout)
        if check_tooling:
            self.ssh(resource, 'docker buildx inspect >/dev/null', timeout=30)
            checks['guest_docker_builder'] = True
        self.state.update(resource, 'isolated', isolation=checks)

    def register(self, resource, repository, token, labels):
        # Host account token stays in gh. Only repository registration token
        # crosses SSH stdin; neither command arguments nor diagnostic logs carry it.
        script = f'''set -e
cd /home/runner/runner
set -a
. /etc/environment
set +a
export PATH=/opt/runspindle-quality/bin:$PATH
./config.sh --unattended --ephemeral --disableupdate --url https://github.com/{repository} --token {shlex.quote(token)} --name {resource['runner_name']} --labels {shlex.quote(','.join(labels))} --work _work
nohup env -u SSH_AUTH_SOCK ./run.sh > /home/runner/job.log 2>&1 < /dev/null &
echo $! > /home/runner/runner.pid
'''
        result = self.ssh(resource, script, timeout=90, check=False)
        (self.directory(resource) / 'registration.log').write_text(result.stdout.replace(token, '[REDACTED]') + result.stderr.replace(token, '[REDACTED]'))
        if result.returncode:
            raise RuntimeError('Runner registration failed; retained redacted diagnostics')

    def runner_exited(self, resource):
        return self.ssh(resource, 'test -f /home/runner/runner.pid && ! kill -0 "$(cat /home/runner/runner.pid)" 2>/dev/null', timeout=10, check=False).returncode == 0

    def diagnostics(self, resource):
        directory = self.directory(resource)
        serial = self.slot(resource) / 'serial.log'
        if serial.exists():
            shutil.copyfile(serial, directory / 'serial.log')
        try:
            result = self.ssh(resource, 'cat /home/runner/job.log 2>/dev/null; tail -n 300 /home/runner/runner/_diag/*.log 2>/dev/null', timeout=15, check=False)
            if result.returncode == 0:
                (directory / 'guest-diagnostics.log').write_text(result.stdout + result.stderr)
                archive = self.ssh(resource, 'set -o pipefail; tar -czf - -C /home/runner runner/_diag job.log 2>/dev/null | base64 -w0', timeout=25, check=False)
                if archive.returncode == 0:
                    (directory / 'guest-diagnostics.tar.gz').write_bytes(base64.b64decode(archive.stdout, validate=True))
            else:
                with (directory / 'diagnostic-errors.log').open('a') as stream:
                    stream.write(result.stderr)
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        execute(['sudo', '-n', 'journalctl', '-u', f'runspindle-vm-{resource["slot"]}.service', '--no-pager', '-n', '200'], check=False)

    def destroy(self, resource):
        helper('stop', resource['slot'], resource['id'])
        serial = self.slot(resource) / 'serial.log'
        if serial.exists():
            shutil.copyfile(serial, self.directory(resource) / 'serial.log')
        for name in ('disk.qcow2', 'seed.iso', 'serial.log'):
            path = self.slot(resource) / name
            path.unlink(missing_ok=True)
        if any(self.slot(resource).iterdir()):
            raise RuntimeError('Unknown slot files retained; capacity remains occupied')
        directory = self.directory(resource)
        (directory / 'key').unlink(missing_ok=True)
        # No claim of cleanup until service, files and registrations are gone.
