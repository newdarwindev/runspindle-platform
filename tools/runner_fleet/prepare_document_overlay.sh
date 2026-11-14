#!/bin/bash
# Only inside an owned disposable overlay. Never download or repair prerequisites.
set -euo pipefail
export PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1
export http_proxy='' https_proxy='' HTTP_PROXY='' HTTPS_PROXY='' ALL_PROXY='' all_proxy=''
python3 - <<'PY'
import hashlib
import importlib.util
import json
import pathlib
import platform
if platform.python_implementation() != 'CPython' or platform.python_version() != '3.12.3':
    raise SystemExit('Existing CPython 3.12.3 is required')
if importlib.util.find_spec('venv') is None or importlib.util.find_spec('ensurepip') is None:
    raise SystemExit('Existing venv and bundled pip tooling are required')
if pathlib.Path('/opt/runspindle-documents').exists():
    raise SystemExit('Base already contains a document environment; no overwrite')
expected = json.loads(pathlib.Path('/home/runner/preparation/base-guest.json').read_text())
if json.loads(pathlib.Path('/etc/runspindle-image.json').read_text()) != expected:
    raise SystemExit('Actual base guest manifest differs from pinned base')
context = json.loads(pathlib.Path('/home/runner/preparation/overlay-context.json').read_text())
source = pathlib.Path('/opt/runspindle-image-tools')
for name, expected_hash in context['preparation_source_sha256'].items():
    path = source / name
    if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
        raise SystemExit('Copied preparation source differs from approved controller')
PY
export PYTHONPATH=/opt/runspindle-image-tools/tools
python3 -m runner_fleet.documents --source /opt/runspindle-image-tools --downloads /home/runner/preparation
sudo python3 -m venv /opt/runspindle-documents
sudo env PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 /opt/runspindle-documents/bin/python -m pip install --no-index --no-deps --only-binary=:all: --require-hashes --find-links=/home/runner/preparation/document-wheels -r /home/runner/preparation/requirements-documents.txt
sudo env PYTHONPATH=/opt/runspindle-image-tools/tools /opt/runspindle-documents/bin/python -m runner_fleet.documents --source /opt/runspindle-image-tools --downloads /home/runner/preparation --installed | sudo tee /opt/runspindle-documents/verified.json >/dev/null
python3 - <<'PY' | sudo tee /etc/runspindle-image.json >/dev/null
import json
import pathlib
p = pathlib.Path('/home/runner/preparation')
base = json.loads((p / 'base-guest.json').read_text())
context = json.loads((p / 'overlay-context.json').read_text())
print(json.dumps({
    'schema_version': 1, 'manifest_kind': 'document-overlay-v1',
    'identity': 'document-overlay-' + context['operation_id'],
    'base_guest': base, **context,
    'documents': json.loads(pathlib.Path('/opt/runspindle-documents/verified.json').read_text()),
}, sort_keys=True))
PY
# Require an acknowledged completed recipe before a separate shutdown command.
printf 'RUNSPINDLE_DOCUMENT_OVERLAY='; cat /etc/runspindle-image.json
