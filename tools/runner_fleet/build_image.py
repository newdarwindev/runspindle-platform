#!/usr/bin/env python3
"""Build and seal a pinned image in a reserved, isolated preparation guest."""
import json
import shlex
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runner_fleet.backend import Backend, ROOT, digest, execute, helper
from runner_fleet.model import config, ROOT as SOURCE
from runner_fleet.staging import verify as verify_staged
from runner_fleet.identity import binding
from runner_fleet.state import State


def main():
    state = State(Path.home() / '.local/state/runspindle-fleet')
    pins = json.loads((SOURCE / 'fleet/image-inputs.json').read_text())
    source_sha = subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], text=True).strip()
    if subprocess.check_output(['git', '-C', str(SOURCE), 'status', '--porcelain', '--untracked-files=all'], text=True).strip():
        raise SystemExit('Image preparation requires a clean exact source checkout')
    source_binding = binding(SOURCE)
    download = state.directory / 'downloads/ubuntu.img'
    download.parent.mkdir(mode=0o700, exist_ok=True)
    with state.lock():
        if state.active():
            raise SystemExit('Existing reservations must be reconciled first')
        if not download.exists():
            execute(['curl', '-fL', '--retry', '2', pins['ubuntu']['url'], '-o', str(download)], timeout=600)
        if digest(download) != pins['ubuntu']['sha256']:
            raise SystemExit('Pinned Ubuntu checksum mismatch; no guest booted')
        verify_staged(SOURCE, download.parent)
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
            staged = [
                'requirements-quality.txt',
                'requirements-documents.txt',
                'quality-node/package.json',
                'quality-node/package-lock.json',
                'quality-node-modules.tar.gz',
                'actions-runner-linux-x64-2.338.2.tar.gz',
                'rust-1.94.1-x86_64-unknown-linux-gnu.tar.xz',
                'node.tar.xz',
                pins['temporal']['filename'],
                'LICENSE-CLI',
                'core-source.tar.gz',
                'host-source.tar.gz',
                'cargo-closure.tar.gz',
                'builder-closure.tar.gz',
            ]
            staged.extend('quality-wheels/' + row['filename'] for row in
                          json.loads((SOURCE / pins['quality']['closure']).read_text())['wheels'])
            staged.extend('document-wheels/' + row['filename'] for row in
                          json.loads((SOURCE / pins['documents']['closure']).read_text())['wheels'])
            for relative in staged:
                backend.copy_prepared(resource, download.parent / relative, relative)
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
            candidate = ROOT / 'images' / f'candidate-{source_sha[:12]}.qcow2'
            if candidate.exists():
                raise RuntimeError('Exact-source candidate image already exists')
            execute(['sudo', '-n', 'qemu-img', 'convert', '-O', 'qcow2', str(backend.slot(resource) / 'disk.qcow2'), str(candidate)], timeout=600)
            execute(['sudo', '-n', 'chown', 'root:root', str(candidate)])
            execute(['sudo', '-n', 'chmod', '0444', str(candidate)])
            if candidate.stat().st_uid != 0 or candidate.stat().st_mode & 0o222:
                raise RuntimeError('Candidate image was not sealed read-only')
            manifest = {'sha256': digest(candidate), 'candidate_path': str(candidate),
                        'inputs': pins, 'guest': manifest,
                        'build_log_sha256': digest(log), 'source_sha': source_sha,
                        **source_binding}
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
