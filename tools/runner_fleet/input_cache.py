"""Digest-addressed immutable input archives, never test-result/build-target cache.

Every import and materialization hashes actual bytes. Cache corruption is an
error, never an excuse to skip verification or silently use another version.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

MAX_BYTES = 8 * 1024**3
MAX_CACHE_BYTES = 16 * 1024**3
MAX_CACHE_ENTRIES = 4096
MIN_FREE_BYTES = 30 * 1024**3
FICLONE = 0x40049409


def _key(expected: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Exact SHA256 cache key required")


def _directory(path: Path) -> None:
    if path.resolve(strict=True) != path:
        raise ValueError("Canonical cache/materialization directory required")
    info = path.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        raise ValueError("Unsafe cache/materialization directory")


def _open(path: Path) -> BinaryIO:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 <= info.st_size <= MAX_BYTES:
            raise ValueError("Input must be bounded regular file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _hash(stream: BinaryIO) -> str:
    stream.seek(0)
    digest = hashlib.sha256()
    size = 0
    for block in iter(lambda: stream.read(1024**2), b""):
        size += len(block)
        if size > MAX_BYTES:
            raise ValueError("Input exceeded cache bound")
        digest.update(block)
    return digest.hexdigest()


class InputCache:
    def __init__(self, root: Path):
        self.root = root.absolute()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _directory(self.root)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        _directory(self.root)
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _entry(self, expected: str) -> Path:
        _key(expected)
        return self.root / expected

    def _verified(self, expected: str) -> Path:
        path = self._entry(expected)
        with _open(path) as stream:
            info = os.fstat(stream.fileno())
            if info.st_uid != os.getuid() or info.st_mode & 0o222 or info.st_nlink != 1:
                raise ValueError("Cache entry is not isolated read-only input")
            if _hash(stream) != expected:
                raise ValueError("Cached input digest mismatch")
        return path

    def has(self, expected: str) -> bool:
        with self._locked():
            path = self._entry(expected)
            if not path.exists() and not path.is_symlink():
                return False
            self._verified(expected)
            return True

    def _copy(self, source: Path, destination: Path, expected: str, size: int) -> str:
        _directory(destination.parent)
        space = os.statvfs(destination.parent)
        if space.f_bavail * space.f_frsize - size < MIN_FREE_BYTES:
            raise ValueError("Materialization disk reserve reached")
        fd, temporary = tempfile.mkstemp(prefix=".input-", dir=destination.parent)
        temp = Path(temporary)
        mode = "copy"
        try:
            with os.fdopen(fd, "w+b") as output, _open(source) as original:
                if os.fstat(original.fileno()).st_size != size:
                    raise ValueError("Input size changed after capacity reservation")
                try:
                    fcntl.ioctl(output.fileno(), FICLONE, original.fileno())
                    mode = "reflink"
                except OSError as error:
                    if error.errno not in (
                        errno.EOPNOTSUPP,
                        errno.ENOTTY,
                        errno.EXDEV,
                        errno.EINVAL,
                    ):
                        raise
                    copied = 0
                    for block in iter(lambda: original.read(1024**2), b""):
                        copied += len(block)
                        if copied > size:
                            raise ValueError("Input grew beyond reserved capacity")
                        output.write(block)
                    output.flush()
                if (
                    os.fstat(output.fileno()).st_size != size
                    or os.fstat(original.fileno()).st_size != size
                ):
                    raise ValueError("Input size changed during materialization")
                if _hash(output) != expected:
                    raise ValueError("Materialized input digest mismatch")
                os.fchmod(output.fileno(), 0o444)
                os.fsync(output.fileno())
            # No overwrite and no input hardlink: this temporary is an independent
            # copy/COW inode. Publication is atomic, including competing importers.
            os.link(temp, destination, follow_symlinks=False)
            temp.unlink()
            directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return mode
        finally:
            if temp.exists():
                temp.unlink()

    def _capacity(self, incoming: int) -> None:
        total = count = 0
        with os.scandir(self.root) as entries:
            for entry in entries:
                count += 1
                if count > MAX_CACHE_ENTRIES:
                    raise ValueError("Input cache entry capacity reached")
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Unexpected input cache entry")
                total += info.st_size
        space = os.statvfs(self.root)
        if (
            count >= MAX_CACHE_ENTRIES
            or total + incoming > MAX_CACHE_BYTES
            or space.f_bavail * space.f_frsize - incoming < MIN_FREE_BYTES
        ):
            raise ValueError("Input cache capacity/reserve reached; no eviction")

    def put(self, source: Path, expected: str) -> str:
        _key(expected)
        # Always validate the requested source too; a warm cache cannot conceal
        # corruption in the caller's supplied/staged artifact.
        with _open(source) as original:
            if _hash(original) != expected:
                raise ValueError("Staged input digest mismatch")
        with self._locked():
            path = self._entry(expected)
            if path.exists() or path.is_symlink():
                self._verified(expected)
                return "existing"
            size = source.stat().st_size
            self._capacity(size)
            mode = self._copy(source, path, expected, size)
            self._verified(expected)
            return mode

    def materialize(self, expected: str, destination: Path) -> str:
        with self._locked():
            source = self._verified(expected)
            _directory(destination.parent)
            if destination.exists() or destination.is_symlink():
                raise ValueError("Materialization destination already exists")
            return self._copy(source, destination, expected, source.stat().st_size)
