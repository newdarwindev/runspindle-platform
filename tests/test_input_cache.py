"""Actual temporary files: no network, guest, service or validation-result cache."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.runner_fleet import cached_staging, input_cache
from tools.runner_fleet.input_cache import InputCache


def import_worker(root: str, source: str, expected: str) -> None:
    with patch.object(input_cache, "MIN_FREE_BYTES", 0):
        InputCache(Path(root)).put(Path(source), expected)


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        reserve = patch.object(input_cache, "MIN_FREE_BYTES", 0)
        reserve.start()
        self.addCleanup(reserve.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.source.write_bytes(b"synthetic immutable input\n" * 4096)
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.cache = InputCache(self.root / "cache")

    def test_cold_warm_independent_readonly_materialization(self) -> None:
        self.assertIn(self.cache.put(self.source, self.digest), ("copy", "reflink"))
        self.assertEqual(self.cache.put(self.source, self.digest), "existing")
        paths = [self.root / "first", self.root / "second"]
        for path in paths:
            self.cache.materialize(self.digest, path)
            self.assertEqual(path.read_bytes(), self.source.read_bytes())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)
            self.assertEqual(path.stat().st_nlink, 1)
        self.assertNotEqual(paths[0].stat().st_ino, paths[1].stat().st_ino)
        paths[0].chmod(0o600)
        paths[0].write_bytes(b"job-local mutation")
        self.assertEqual(paths[1].read_bytes(), self.source.read_bytes())
        self.assertTrue(self.cache.has(self.digest))

    def test_wrong_source_poisoned_cache_and_existing_target_deny(self) -> None:
        with self.assertRaises(ValueError):
            self.cache.put(self.source, "0" * 64)
        self.cache.put(self.source, self.digest)
        self.source.write_bytes(b"changed")
        with self.assertRaises(ValueError):
            self.cache.put(self.source, self.digest)
        with self.assertRaises(ValueError):
            self.cache.materialize(self.digest, self.source)
        entry = self.cache.root / self.digest
        entry.chmod(0o600)
        entry.write_bytes(b"poisoned")
        entry.chmod(0o444)
        with self.assertRaises(ValueError):
            self.cache.materialize(self.digest, self.root / "never")
        self.assertFalse((self.root / "never").exists())

    def test_symlink_fifo_and_unsafe_root_deny(self) -> None:
        link = self.root / "link"
        link.symlink_to(self.source)
        with self.assertRaises(OSError):
            self.cache.put(link, self.digest)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            self.cache.put(fifo, self.digest)
        bad = self.root / "bad"
        bad.mkdir()
        bad.chmod(0o777)
        with self.assertRaises(ValueError):
            InputCache(bad)
        (self.cache.root / self.digest).symlink_to(self.source)
        with self.assertRaises(OSError):
            self.cache.has(self.digest)

    def test_competing_importers_publish_one_complete_input(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        workers = [
            ctx.Process(
                target=import_worker,
                args=(str(self.cache.root), str(self.source), self.digest),
            )
            for _ in range(2)
        ]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual([p.name for p in self.cache.root.iterdir()], [self.digest])
            self.assertTrue(self.cache.has(self.digest))
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.kill()
                worker.join(5)

    def test_restore_missing_does_not_mask_present_corruption(self) -> None:
        self.cache.put(self.source, self.digest)
        downloads = self.root / "downloads"
        downloads.mkdir(mode=0o700)
        with patch.object(
            cached_staging, "inventory", return_value={"nested/input": self.digest}
        ):
            result = cached_staging.restore_missing(self.root, downloads, self.cache)
            self.assertEqual(result, {"restored_inputs": 1, "missing_inputs": 0})
            restored = downloads / "nested/input"
            restored.chmod(0o600)
            restored.write_bytes(b"bad")
            with self.assertRaises(ValueError):
                cached_staging.restore_missing(self.root, downloads, self.cache)

    def test_capacity_denial_does_not_evict_or_materialize(self) -> None:
        from tools.runner_fleet import input_cache

        with patch.object(input_cache, "MAX_CACHE_BYTES", 1):
            with self.assertRaises(ValueError):
                self.cache.put(self.source, self.digest)
        self.assertEqual(list(self.cache.root.iterdir()), [])

    def test_disk_reserve_denial_is_not_eviction(self) -> None:
        with patch.object(input_cache, "MIN_FREE_BYTES", 2**63):
            with self.assertRaises(ValueError):
                self.cache.put(self.source, self.digest)
        self.assertEqual(list(self.cache.root.iterdir()), [])

    def test_exact_entry_limit_denies_next_import(self) -> None:
        self.cache.put(self.source, self.digest)
        other = self.root / "other-source"
        other.write_bytes(b"other pinned input")
        key = hashlib.sha256(other.read_bytes()).hexdigest()
        with patch.object(input_cache, "MAX_CACHE_ENTRIES", 1):
            with self.assertRaises(ValueError):
                self.cache.put(other, key)
        self.assertEqual([p.name for p in self.cache.root.iterdir()], [self.digest])
        self.assertTrue(self.cache.has(self.digest))

    def test_materialization_preserves_destination_disk_reserve(self) -> None:
        self.cache.put(self.source, self.digest)
        output = self.root / "output"
        with patch.object(input_cache, "MIN_FREE_BYTES", 2**63):
            with self.assertRaises(ValueError):
                self.cache.materialize(self.digest, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".input-*")), [])
        self.assertTrue(self.cache.has(self.digest))

    def test_growth_after_reservation_denies_before_publication(self) -> None:
        original = self.cache._capacity

        def grow(size: int) -> None:
            original(size)
            with self.source.open("ab") as stream:
                stream.write(b"growth")

        with patch.object(self.cache, "_capacity", side_effect=grow):
            with self.assertRaises(ValueError):
                self.cache.put(self.source, self.digest)
        self.assertEqual(list(self.cache.root.iterdir()), [])
