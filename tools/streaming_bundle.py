"""Versioned, bounded streaming development artifacts; no service activation.

Wire format: magic, uint64 big endian canonical manifest length, manifest, then
regular file bytes in manifest order. There are no archive paths or link records
outside the manifest and no compression/decompression expansion.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct

from tools.development_bundle import (
    VERSION as METADATA_VERSION,
    DisposableTarget,
    UNSUPPORTED,
    actual_target,
    build as metadata_build,
    canonical,
    check,
    path_name,
    sha,
)

VERSION = "runspindle.streaming-development-bundle.v1"
MAGIC = b"RUNSPINDLE-STREAM\x00\x01"
MAX_MANIFEST = 32 * 1024 * 1024
MAX_BYTES = 4 * 1024**3
MAX_FILES = 60000
CHUNK = 1024 * 1024


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(CHUNK):
            h.update(block)
    return h.hexdigest()


def stream_path(name):
    check(type(name) is str and 1 <= len(name) <= 512, "unsafe bundle path")
    path = PurePosixPath(name)
    check(
        str(path) == name
        and not path.is_absolute()
        and ".." not in path.parts
        and all(32 <= ord(c) <= 126 and c not in "\\:" for c in name),
        "unsafe bundle path",
    )
    return name


def _unique(pairs):
    value = {}
    for key, item in pairs:
        check(key not in value, "duplicate manifest field")
        value[key] = item
    return value


def validate_manifest(manifest, *, component_commits, target="linux/arm64"):
    check(
        type(manifest) is dict
        and set(manifest)
        == {
            "version",
            "kind",
            "target",
            "composition_commit",
            "component_commits",
            "dependencies",
            "toolchains",
            "licenses",
            "files",
            "unsupported",
            "entrypoints",
        },
        "unsupported streaming manifest fields",
    )
    check(manifest["version"] == VERSION, "unsupported streaming bundle version")
    check(manifest["target"] == target == "linux/arm64", "wrong target architecture")
    check(
        manifest["component_commits"] == component_commits, "composition pin mismatch"
    )
    check(
        manifest["kind"] == "development_only"
        and manifest["unsupported"] == UNSUPPORTED,
        "unsupported product release claim",
    )
    files = manifest["files"]
    check(type(files) is dict and 1 <= len(files) <= MAX_FILES, "bundle file limit")
    total = 0
    for name, item in files.items():
        stream_path(name)
        check(
            type(item) is dict and set(item) == {"size", "sha256", "mode"},
            "invalid file record",
        )
        check(
            type(item["size"]) is int and 0 <= item["size"] <= MAX_BYTES,
            "invalid file size",
        )
        check(
            type(item["sha256"]) is str
            and re.fullmatch("[0-9a-f]{64}", item["sha256"]),
            "invalid file digest",
        )
        check(
            type(item["mode"]) is int and item["mode"] in (0o644, 0o755),
            "unsafe file mode",
        )
        total += item["size"]
        # A file cannot also be a directory for another file.
        check(
            not any(
                str(parent) in files
                for parent in Path(name).parents
                if str(parent) != "."
            ),
            "file/directory path conflict",
        )
    check(total <= MAX_BYTES, "bundle payload size limit")
    licenses = manifest["licenses"]
    check(
        type(licenses) is dict
        and licenses
        and all(
            name in files and files[name]["sha256"] == digest
            for name, digest in licenses.items()
        ),
        "license byte mismatch",
    )
    # Reuse v1 metadata validation for revisions and digest inventories without
    # weakening its limits or accepting executable payloads in that format.
    metadata_build(
        METADATA_VERSION,
        manifest["composition_commit"],
        component_commits,
        {"notice": b"validation"},
        dependencies=manifest["dependencies"],
        toolchains=manifest["toolchains"],
        licenses={"notice": sha(b"validation")},
    )
    entries = manifest["entrypoints"]
    check(
        type(entries) is dict
        and entries
        and all(
            type(label) is str
            and path_name(label)
            and name in files
            and files[name]["mode"] == 0o755
            for label, name in entries.items()
        ),
        "missing executable entrypoint",
    )
    return manifest


def _header(stream, *, component_commits, target):
    check(stream.read(len(MAGIC)) == MAGIC, "unsupported streaming bundle version")
    raw_size = stream.read(8)
    check(len(raw_size) == 8, "incomplete bundle header")
    size = struct.unpack(">Q", raw_size)[0]
    check(0 < size <= MAX_MANIFEST, "manifest size limit")
    data = stream.read(size)
    check(len(data) == size, "incomplete bundle manifest")
    manifest = json.loads(data, object_pairs_hook=_unique)
    check(canonical(manifest) == data, "noncanonical streaming manifest")
    validate_manifest(manifest, component_commits=component_commits, target=target)
    return manifest, sha(data)


def inspect(bundle, *, component_commits, target="linux/arm64", destination=None):
    """Stream verification; optional extraction only to an owned empty directory."""
    root = Path(destination) if destination is not None else None
    if root is not None:
        check(
            root.is_dir() and not root.is_symlink() and not any(root.iterdir()),
            "extraction target must be empty",
        )
    with Path(bundle).open("rb") as stream:
        manifest, digest = _header(
            stream, component_commits=component_commits, target=target
        )
        for name, item in sorted(manifest["files"].items()):
            output = None
            if root is not None:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                output = path.open("xb")
            try:
                h = hashlib.sha256()
                remaining = item["size"]
                while remaining:
                    block = stream.read(min(CHUNK, remaining))
                    check(bool(block), "incomplete bundle payload")
                    h.update(block)
                    remaining -= len(block)
                    if output:
                        output.write(block)
                check(h.hexdigest() == item["sha256"], "bundle checksum mismatch")
                if output:
                    output.flush()
                    os.fsync(output.fileno())
                    os.fchmod(output.fileno(), item["mode"])
            finally:
                if output:
                    output.close()
        check(not stream.read(1), "unexpected trailing bundle payload")
    return manifest, digest


def build(
    destination,
    composition_commit,
    component_commits,
    files,
    *,
    dependencies,
    toolchains,
    licenses,
    entrypoints,
    executable=(),
):
    """Build from regular files; source mutations invalidate the candidate output."""
    records = {}
    executable = set(executable)
    check(executable <= set(files), "unknown executable file")
    for name, source in sorted(files.items()):
        stream_path(name)
        source = Path(source)
        check(
            source.is_file() and not source.is_symlink(), "regular source file required"
        )
        records[name] = {
            "size": source.stat().st_size,
            "sha256": file_sha(source),
            "mode": 0o755 if name in executable else 0o644,
        }
    manifest = dict(
        version=VERSION,
        kind="development_only",
        target="linux/arm64",
        composition_commit=composition_commit,
        component_commits=component_commits,
        dependencies=dependencies,
        toolchains=toolchains,
        licenses=licenses,
        files=records,
        unsupported=UNSUPPORTED,
        entrypoints=entrypoints,
    )
    validate_manifest(manifest, component_commits=component_commits)
    data = canonical(manifest)
    check(len(data) <= MAX_MANIFEST, "manifest size limit")
    destination = Path(destination)
    check(not destination.exists(), "bundle destination already exists")
    try:
        with destination.open("xb") as output:
            output.write(MAGIC + struct.pack(">Q", len(data)) + data)
            for name in sorted(files):
                with Path(files[name]).open("rb") as source:
                    shutil.copyfileobj(source, output, CHUNK)
            output.flush()
            os.fsync(output.fileno())
        inspect(destination, component_commits=component_commits)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return manifest, sha(data)


def verify_tree(folder, manifest):
    folder = Path(folder)
    check(folder.is_dir() and not folder.is_symlink(), "installed version unavailable")
    check(not any(p.is_symlink() for p in folder.rglob("*")), "unsafe installed link")
    actual = {
        str(p.relative_to(folder)): {
            "sha256": file_sha(p),
            "size": p.stat().st_size,
            "mode": p.stat().st_mode & 0o7777,
        }
        for p in folder.rglob("*")
        if p.is_file()
    }
    check(actual == manifest["files"], "installed byte or mode mismatch")


def _sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class StreamingTarget(DisposableTarget):
    """Separate stream API; reuses the v1 journal schema, storing manifests only.

    The immutable archive is retained beside the journal, never in a SQLite BLOB.
    Journal ownership covers staging/readback; generations fence competing owners.
    """

    def _archive(self, digest, pins):
        check(
            type(digest) is str and re.fullmatch("[0-9a-f]{64}", digest),
            "invalid installation digest",
        )
        check(
            not any(p.is_symlink() for p in self.root.rglob("*")),
            "unsafe installation target",
        )
        archive = self.root / "archives" / digest
        check(
            archive.is_file() and not archive.is_symlink(),
            "retained payload unavailable",
        )
        manifest, found = inspect(
            archive, component_commits=pins, target=actual_target()
        )
        row = self.db.execute(
            "SELECT bundle FROM installs WHERE digest=?", (digest,)
        ).fetchone()
        check(
            found == digest and row and row[0] == canonical(manifest),
            "retained payload identity mismatch",
        )
        return archive, manifest

    def install(
        self,
        api_version,
        bundle,
        *,
        component_commits,
        expected_generation,
        checkpoint=lambda _: None,
    ):
        check(api_version == VERSION, "unsupported streaming installation version")
        manifest, digest = inspect(
            bundle, component_commits=component_commits, target=actual_target()
        )
        check(
            type(expected_generation) is int and expected_generation >= 0,
            "invalid generation",
        )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            state = self.state()
            check(
                state["generation"] == expected_generation and state["pending"] is None,
                "installation ownership conflict",
            )
            check(
                not self.db.execute(
                    "SELECT 1 FROM installs WHERE digest=?", (digest,)
                ).fetchone(),
                "already retained installation",
            )
            check(
                self.db.execute("SELECT count(*) FROM installs").fetchone()[0] < 8,
                "retained installation capacity",
            )
            archives = self.root / "archives"
            archives.mkdir(exist_ok=True)
            check(
                not any(p.is_symlink() for p in self.root.rglob("*")),
                "unsafe installation target",
            )
            saved = archives / digest
            # A crash during acquisition leaves an unreferenced file, safe to replace.
            temporary = archives / (digest + ".next")
            with Path(bundle).open("rb") as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output, CHUNK)
                output.flush()
                os.fsync(output.fileno())
            observed, found = inspect(
                temporary, component_commits=component_commits, target=actual_target()
            )
            check(
                found == digest and observed == manifest,
                "payload changed during acquisition",
            )
            temporary.replace(saved)
            _sync_dir(archives)
            self.db.execute(
                "INSERT INTO installs VALUES(?,?,?,'stream_intent')",
                (digest, canonical(manifest), state["active"]),
            )
            self.db.execute(
                "UPDATE state SET generation=generation+1,pending=? WHERE id=1",
                (digest,),
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        checkpoint("after_install_intent")
        return self.recover(
            api_version, component_commits=component_commits, checkpoint=checkpoint
        )

    def verify_installed(self, component_commits):
        state = self.state()
        digest = self._observed()
        check(
            digest is not None and digest in (state["active"], state["pending"]),
            "conflicting installation readback",
        )
        _, manifest = self._archive(digest, component_commits)
        verify_tree(self.root / "versions" / digest, manifest)
        return {
            "manifest_sha256": digest,
            "files": len(manifest["files"]),
            "unsupported": manifest["unsupported"],
        }

    def recover(self, api_version, *, component_commits, checkpoint=lambda _: None):
        check(api_version == VERSION, "unsupported streaming recovery version")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            state = self.state()
            pending = state["pending"]
            if pending is not None:
                check(
                    self._observed() in (state["active"], pending),
                    "conflicting installation readback",
                )
                archive, manifest = self._archive(pending, component_commits)
                folder = self.root / "versions" / pending
                verified = (
                    self.db.execute(
                        "SELECT state FROM installs WHERE digest=?", (pending,)
                    ).fetchone()[0]
                    == "stream_verified"
                )
                check(
                    not any(p.is_symlink() for p in self.root.rglob("*")),
                    "unsafe installation target",
                )
                if not verified and self._observed() != pending:
                    stage = self.root / "stream-stage"
                    if stage.exists():
                        shutil.rmtree(stage)
                    stage.mkdir()
                    inspect(
                        archive,
                        component_commits=component_commits,
                        target=actual_target(),
                        destination=stage,
                    )
                    checkpoint("after_staged_payload")
                    verify_tree(stage, manifest)
                    folder.parent.mkdir(exist_ok=True)
                    # Existing uncommitted version must also have exact bytes.
                    if folder.exists():
                        verify_tree(folder, manifest)
                        shutil.rmtree(stage)
                    else:
                        stage.replace(folder)
                        _sync_dir(folder.parent)
                verify_tree(folder, manifest)
                temporary = self.root / "active.next"
                with temporary.open("wb") as output:
                    output.write(canonical({"manifest_sha256": pending}))
                    output.flush()
                    os.fsync(output.fileno())
                temporary.replace(self.root / "active.json")
                _sync_dir(self.root)
                checkpoint("after_installed_pointer")
                self.db.execute(
                    "UPDATE installs SET state='stream_verified' WHERE digest=?",
                    (pending,),
                )
                self.db.execute(
                    "UPDATE state SET active=?,pending=NULL WHERE id=1", (pending,)
                )
            result = self.verify_installed(component_commits)
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def rollback(
        self,
        api_version,
        *,
        component_commits,
        expected_generation,
        checkpoint=lambda _: None,
    ):
        check(api_version == VERSION, "unsupported streaming rollback version")
        check(
            type(expected_generation) is int and expected_generation >= 0,
            "invalid generation",
        )
        self.db.execute("BEGIN IMMEDIATE")
        try:
            state = self.state()
            check(
                state["generation"] == expected_generation
                and state["pending"] is None
                and state["active"],
                "rollback ownership conflict",
            )
            self.verify_installed(component_commits)
            row = self.db.execute(
                "SELECT predecessor FROM installs WHERE digest=?", (state["active"],)
            ).fetchone()
            check(row and row[0], "predecessor unavailable")
            predecessor = row[0]
            _, manifest = self._archive(predecessor, component_commits)
            check(
                self.db.execute(
                    "SELECT state FROM installs WHERE digest=?", (predecessor,)
                ).fetchone()[0]
                == "stream_verified",
                "predecessor unavailable",
            )
            verify_tree(self.root / "versions" / predecessor, manifest)
            self.db.execute(
                "UPDATE state SET generation=generation+1,pending=? WHERE id=1",
                (predecessor,),
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        checkpoint("after_rollback_intent")
        return self.recover(
            api_version, component_commits=component_commits, checkpoint=checkpoint
        )
