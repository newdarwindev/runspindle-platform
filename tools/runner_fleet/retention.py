"""Retain diagnostic captures without replacing earlier evidence."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from pathlib import Path
from typing import BinaryIO


def _capture(directory: Path, name: str) -> tuple[Path, BinaryIO]:
    if Path(name).name != name or name in ("", ".", ".."):
        raise ValueError("Invalid diagnostic capture name")
    path = directory / name
    while True:
        try:
            descriptor = os.open(
                path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            return path, os.fdopen(descriptor, "wb")
        except FileExistsError:
            path = directory / f"{name}.capture-{uuid.uuid4().hex}"


def capture_bytes(directory: Path, name: str, payload: bytes) -> Path:
    path, output = _capture(directory, name)
    with output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(payload).digest():
        raise RuntimeError("Diagnostic capture verification failed; evidence retained")
    return path


def capture_file(source: Path, directory: Path, name: str) -> Path:
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as original:
        info = os.fstat(original.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Diagnostic source is not a regular file")
        path, output = _capture(directory, name)
        expected = hashlib.sha256()
        remaining = info.st_size
        with output:
            # A running log may grow. Capture its observed prefix; final stopped
            # logs are separately checked in full before relocating the original.
            while remaining:
                chunk = original.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise RuntimeError("Diagnostic source shortened; capture retained")
                output.write(chunk)
                expected.update(chunk)
                remaining -= len(chunk)
            output.flush()
            os.fsync(output.fileno())
    observed = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            observed.update(chunk)
    if observed.digest() != expected.digest():
        raise RuntimeError("Diagnostic capture verification failed; evidence retained")
    return path
