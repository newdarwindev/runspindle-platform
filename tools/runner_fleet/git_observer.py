"""Bounded local clean-checkout observer. Not admission or atomic attestation.

Requires an exclusively owned, quiescent checkout and trusted installed Git.
No worktree execution, fetch, profile inference or external-closure resolution.
"""

from __future__ import annotations

import hashlib
import os
import re
import selectors
import stat
import subprocess
import time
from pathlib import Path

from .environment_applicability import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TREE_BYTES,
    Blob,
    Observation,
    _hex,
    _path,
)

MAX_OUTPUT = 4 * 1024 * 1024
MAX_SECONDS = 30.0


def _git(root: Path, args: list[str], limit: int, deadline: float) -> bytes:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    command = [
        "/usr/bin/git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "protocol.allow=never",
        "-c",
        "core.fsmonitor=false",
        "-C",
        str(root),
        *args,
    ]
    with subprocess.Popen(
        command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        assert process.stdout is not None and process.stderr is not None
        data = bytearray()
        errors = 0
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, True)
                selector.register(process.stderr, selectors.EVENT_READ, False)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError("Git observation deadline exceeded")
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        elif key.data:
                            data.extend(chunk)
                            if len(data) > limit:
                                raise ValueError("Git output exceeds bound")
                        else:
                            errors += len(chunk)
                            if errors > 65536:
                                raise ValueError("Git error output exceeds bound")
            if process.wait(timeout=max(0.001, deadline - time.monotonic())):
                raise ValueError("Local Git observation failed")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
    return bytes(data)


def _checkout(root: Path, deadline: float) -> tuple[Blob, ...]:
    result: list[Blob] = []
    pending = [root]
    entries = total = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                if directory == root and child.name == ".git":
                    continue
                entries += 1
                if entries > MAX_FILES * 2 or time.monotonic() >= deadline:
                    raise ValueError("Checkout traversal exceeds bound")
                path = Path(child.path)
                relative = _path(path.relative_to(root).as_posix())
                metadata = child.stat(follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(path)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("Unsupported checkout entry")
                if metadata.st_size > MAX_FILE_BYTES:
                    raise ValueError("Checkout file too large")
                total += metadata.st_size
                if total > MAX_TREE_BYTES or len(result) >= MAX_FILES:
                    raise ValueError("Checkout bytes or files exceed bound")
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as stream:
                    before = os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError("Checkout file changed type")
                    content = stream.read(MAX_FILE_BYTES + 1)
                    after = os.fstat(stream.fileno())
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                    before.st_mode,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                    after.st_mode,
                ) or len(content) != metadata.st_size:
                    raise ValueError("Checkout changed during observation")
                result.append(
                    Blob(
                        relative,
                        "100755" if metadata.st_mode & 0o111 else "100644",
                        len(content),
                        hashlib.sha256(content).hexdigest(),
                    )
                )
    return tuple(sorted(result, key=lambda blob: blob.path))


def observe_checkout(
    root: Path,
    repository: str,
    source_sha: str,
    environment_sha256: str,
    external_closure_sha256: str,
) -> Observation:
    """Observe all tracked bytes; reject all extra files, including ignored ones.

    Repository name and environment/external closure are operator assertions,
    not authenticated here. Quiescence is a prerequisite, not enforced locking.
    """
    if (
        not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository
        )
        or len(repository) > 256
    ):
        raise ValueError("Exact operator repository required")
    _hex(source_sha, 40)
    _hex(environment_sha256, 64)
    _hex(external_closure_sha256, 64)
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("Canonical checkout required")
    deadline = time.monotonic() + MAX_SECONDS

    def git(args: list[str], limit: int = MAX_OUTPUT) -> bytes:
        return _git(root, args, limit, deadline)

    if git(["rev-parse", "--show-toplevel"]).decode().strip() != str(root):
        raise ValueError("Exact repository root required")
    if git(["rev-parse", "HEAD"]).decode().strip() != source_sha:
        raise ValueError("Exact checked-out commit required")
    if git(["cat-file", "-t", source_sha]) != b"commit\n":
        raise ValueError("Exact commit object required")

    metadata_bytes = 0

    def object_bytes(kind: str, oid: str, limit: int) -> bytes:
        nonlocal metadata_bytes
        value = git(["cat-file", kind, oid], limit)
        if kind != "blob":
            metadata_bytes += len(value)
            if metadata_bytes > MAX_OUTPUT:
                raise ValueError("Git metadata exceeds bound")
        header = f"{kind} {len(value)}\0".encode()
        if hashlib.sha1(header + value).hexdigest() != oid:
            raise ValueError("Git object digest mismatch")
        return value

    commit = object_bytes("commit", source_sha, MAX_OUTPUT)
    tree_oid = commit.split(b"\n", 1)[0].removeprefix(b"tree ").decode("ascii")
    _hex(tree_oid, 40)
    object_bytes("tree", tree_oid, MAX_OUTPUT)
    tree = git(["ls-tree", "-r", "-t", "-z", "--full-tree", source_sha])
    rows = tree.split(b"\0")
    if rows[-1] != b"" or len(rows) - 1 > MAX_FILES * 2:
        raise ValueError("Tree inventory exceeds bound")
    blobs: list[Blob] = []
    expected_index = []
    total = 0
    for row in rows[:-1]:
        header, name = row.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split(" ")
        path = _path(name.decode("ascii"))
        _hex(oid, 40)
        if mode == "040000" and kind == "tree":
            object_bytes("tree", oid, MAX_OUTPUT)
            continue
        if len(blobs) >= MAX_FILES:
            raise ValueError("Git file count exceeds bound")
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError("Unsupported Git mode")
        size = int(git(["cat-file", "-s", oid], 64))
        total += size
        if not 0 <= size <= MAX_FILE_BYTES or total > MAX_TREE_BYTES:
            raise ValueError("Git blob or tree exceeds bound")
        content = object_bytes("blob", oid, size)
        if len(content) != size:
            raise ValueError("Git blob length mismatch")
        blobs.append(Blob(path, mode, size, hashlib.sha256(content).hexdigest()))
        expected_index.append(mode.encode() + b" " + oid.encode() + b" 0\t" + name)
    expected = tuple(sorted(blobs, key=lambda blob: blob.path))
    index = git(["ls-files", "--stage", "-z"])
    if sorted(index.split(b"\0")[:-1]) != sorted(expected_index):
        raise ValueError("Index differs from commit")
    if _checkout(root, deadline) != expected or _checkout(root, deadline) != expected:
        raise ValueError("Dirty, extra or missing checkout input")
    if (
        git(["rev-parse", "HEAD"]).decode().strip() != source_sha
        or git(["ls-files", "--stage", "-z"]) != index
    ):
        raise ValueError("Checkout identity changed")
    return Observation(
        repository, source_sha, environment_sha256, external_closure_sha256, expected
    )
