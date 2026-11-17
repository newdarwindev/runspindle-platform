"""Fixed Platform validation replay payload for the bounded no-NIC probe."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path
from typing import Any

WORKFLOW_SHA256 = "c41f2e2057821d6d3b76067ef3e76ed5eaa7f64029c1ecf72ed9ab2ddedc7b64"
MAX_BUNDLE = 8 * 1024**2
COMMANDS = [["python3", "tools/check_scaffold.py"], ["python3", "tools/validate.py"]]


def load_bundle(path: Path, expected: str, source: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or not re.fullmatch(
        r"[0-9a-f]{40}", source
    ):
        raise ValueError("Exact bundle and source digests required")
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Regular bundle required")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Regular bundle required")
        data = stream.read(MAX_BUNDLE + 1)
    if (
        not data
        or len(data) > MAX_BUNDLE
        or hashlib.sha256(data).hexdigest() != expected
    ):
        raise ValueError("Bundle size or digest mismatch")
    if not data.startswith(b"# v2 git bundle\n") and not data.startswith(
        b"# v3 git bundle\n"
    ):
        raise ValueError("Git bundle required")
    return data


def guest_config(nonce: str, bundle: bytes, source: str) -> bytes:
    script = (
        """import hashlib, json, os, subprocess
source = SOURCE
bundle_digest = DIGEST
result = {"source_sha": source, "commands": [], "setup_ok": False}
env = dict(os.environ, PATH="/opt/runspindle-quality/bin:/opt/rust/bin:/usr/bin:/bin", PYTHONDONTWRITEBYTECODE="1", GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0="/dev/null")
with open('/dev/ttyS0', 'w') as serial:
    try:
        with open('/run/source.bundle', 'rb') as stream:
            if hashlib.sha256(stream.read()).hexdigest() != bundle_digest:
                raise ValueError('bundle digest')
        subprocess.run(['/usr/bin/git', 'init', '/run/platform'], stdout=serial, stderr=serial, check=True, timeout=15, env=env)
        subprocess.run(['/usr/bin/git', '-C', '/run/platform', 'fetch', '/run/source.bundle', 'refs/remotes/origin/main:refs/remotes/origin/main'], stdout=serial, stderr=serial, check=True, timeout=30, env=env)
        subprocess.run(['/usr/bin/git', '-C', '/run/platform', 'checkout', '--detach', source], stdout=serial, stderr=serial, check=True, timeout=15, env=env)
        actual = subprocess.check_output(['/usr/bin/git', '-C', '/run/platform', 'rev-parse', 'HEAD'], text=True, timeout=5, env=env).strip()
        if actual != source:
            raise ValueError('source mismatch')
        with open('/run/platform/.github/workflows/platform-ci.yml', 'rb') as stream:
            workflow_digest = hashlib.sha256(stream.read()).hexdigest()
        if workflow_digest != 'c41f2e2057821d6d3b76067ef3e76ed5eaa7f64029c1ecf72ed9ab2ddedc7b64':
            raise ValueError('workflow mismatch')
        result['workflow_sha256'] = workflow_digest
        result['setup_ok'] = True
        for command in COMMANDS:
            try:
                p = subprocess.run(command, cwd='/run/platform', stdout=serial, stderr=serial, env=env, timeout=120)
                result['commands'].append({'argv': command, 'returncode': p.returncode})
                if p.returncode != 0:
                    break
            except subprocess.TimeoutExpired:
                result['commands'].append({'argv': command, 'returncode': -1})
                break
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result['error'] = type(error).__name__
    serial.write('MARKER' + json.dumps(result, sort_keys=True) + '\\n')
    serial.flush()
subprocess.run(['/sbin/poweroff'], check=False)
""".replace("SOURCE", repr(source))
        .replace("DIGEST", repr(hashlib.sha256(bundle).hexdigest()))
        .replace("COMMANDS", repr(COMMANDS))
        .replace("MARKER", "RUNSPINDLE-REPLAY-" + nonce + ":")
    )
    config = {
        "write_files": [
            {
                "path": path,
                "permissions": "0600",
                "encoding": "b64",
                "content": base64.b64encode(data).decode(),
            }
            for path, data in [
                ("/run/source.bundle", bundle),
                ("/run/replay.py", script.encode()),
            ]
        ],
        "runcmd": [["/usr/bin/python3", "/run/replay.py"]],
    }
    return b"#cloud-config\n" + json.dumps(config).encode() + b"\n"


def parse_result(
    path: Path, nonce: str, source: str, deadline: float | None = None
) -> dict[str, Any]:
    from .offline_probe import FILE_LIMIT

    if path.stat().st_size > FILE_LIMIT:
        raise ValueError("Replay output exceeds bound")
    marker = ("RUNSPINDLE-REPLAY-" + nonce + ":").encode()
    found = None
    deadline = deadline if deadline is not None else time.monotonic() + 30
    with path.open("rb") as stream:
        while line := stream.readline(65537):
            if time.monotonic() >= deadline:
                raise TimeoutError("Replay parsing deadline")
            if len(line) > 65536:
                raise ValueError("Replay line exceeds bound")
            if line.startswith(marker):
                if found is not None:
                    raise ValueError("Repeated replay result")
                found = json.loads(line[len(marker) :])
    if (
        not isinstance(found, dict)
        or found.get("source_sha") != source
        or found.get("workflow_sha256") != WORKFLOW_SHA256
        or found.get("setup_ok") is not True
    ):
        raise ValueError("Missing or invalid replay setup")
    expected = [{"argv": argv, "returncode": 0} for argv in COMMANDS]
    commands = found.get("commands")
    if commands != expected or any(
        type(item.get("returncode")) is not int for item in commands
    ):
        raise ValueError("Platform validation failed; complete guest output retained")
    return found
