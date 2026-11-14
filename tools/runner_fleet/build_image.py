#!/usr/bin/env python3
"""Build and seal a pinned image in a reserved, isolated preparation guest."""
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runner_fleet.backend import Backend, ROOT, digest, execute, helper
from runner_fleet.model import config, ROOT as SOURCE
from runner_fleet.state import State


def main():
    state = State(Path.home() / '.local/state/runspindle-fleet')
    pins = json.loads((SOURCE / 'fleet/image-inputs.json').read_text())
    download = state.directory / 'downloads/ubuntu.img'
    download.parent.mkdir(mode=0o700, exist_ok=True)
    with state.lock():
        if state.active():
            raise SystemExit('Existing reservations must be reconciled first')
        if not download.exists():
            execute(['curl', '-fL', '--retry', '2', pins['ubuntu']['url'], '-o', str(download)], timeout=600)
        if digest(download) != pins['ubuntu']['sha256']:
            raise SystemExit('Pinned Ubuntu checksum mismatch; no guest booted')
        execute(['sudo', '-n', 'install', '-m', '0444', str(download), str(ROOT / 'images/ubuntu-base.qcow2')])
        resource = state.reserve({'repository': 'runspindle-platform', 'id': -2})
        backend = Backend(state, ROOT / 'images/ubuntu-base.qcow2')
        try:
            backend.create(resource, prepare=True)
            deadline = time.monotonic() + 240
            while not backend.ready(resource):
                if time.monotonic() > deadline:
                    raise RuntimeError('Preparation boot timeout')
                time.sleep(2)
            backend.isolation(resource, check_tooling=False)
            closure = pins['offline_rust_dependencies']
            exports = ''.join(f'export {name}={shlex.quote(closure[key])}\n' for name, key in
                              (('RUNSPINDLE_CORE_SHA', 'core'), ('RUNSPINDLE_HOST_SHA', 'host')))
            result = backend.ssh(resource, exports + (SOURCE / 'tools/runner_fleet/prepare_image.sh').read_text(), timeout=3600, check=False)
            log = backend.directory(resource) / 'image-build.log'
            log.write_text(result.stdout + result.stderr)
            manifests = [line.removeprefix('RUNSPINDLE_IMAGE=') for line in result.stdout.splitlines() if line.startswith('RUNSPINDLE_IMAGE=')]
            if len(manifests) != 1 or result.returncode not in (0, 255):
                raise RuntimeError('Image build did not emit one terminal package/license manifest')
            manifest = json.loads(manifests[0])
            deadline = time.monotonic() + 60
            while helper('observe', resource['slot']).get('ActiveState') in ('active', 'activating'):
                if time.monotonic() > deadline:
                    raise RuntimeError('Image preparation did not shut down')
                time.sleep(2)
            helper('stop', resource['slot'], resource['id'])
            candidate = state.directory / 'candidate.qcow2'
            execute(['sudo', '-n', 'qemu-img', 'convert', '-O', 'qcow2', str(backend.slot(resource) / 'disk.qcow2'), str(candidate)], timeout=600)
            execute(['sudo', '-n', 'chown', f'{os.getuid()}:{os.getgid()}', str(candidate)])
            manifest = {'sha256': digest(candidate), 'inputs': pins, 'guest': manifest, 'build_log_sha256': digest(log)}
            path = state.directory / 'candidate.json'
            path.write_text(json.dumps(manifest, indent=2))
            backend.destroy(resource)
            state.update(resource, 'destroyed', cleanup_verified=True, image_sha256=manifest['sha256'])
            print(json.dumps({'status': 'candidate', 'image_sha256': manifest['sha256'], 'qualification': 'pending'}))
        except BaseException as error:
            state.update(resource, 'uncertain', error=str(error))
            raise


if __name__ == '__main__':
    main()
