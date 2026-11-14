"""Checksummed development bundle and disposable-only installation journal.

No services, host enrollment, production target, backup or deployment adapter.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import sqlite3
import tempfile

VERSION = "runspindle.maintained-evidence.v1"
COMPONENTS = {"core", "host", "browser", "console", "refinery", "platform", "tether"}
MAX_BYTES = 16 * 1024 * 1024
MAX_FILES = 128
UNSUPPORTED = [
    "full_product",
    "native",
    "Tether",
    "browser",
    "console",
    "provider",
    "production_services",
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def check(condition, message):
    if not condition:
        raise ValueError(message)


def path_name(name):
    path = PurePosixPath(name)
    check(
        type(name) is str
        and 1 <= len(name) <= 256
        and str(path) == name
        and not path.is_absolute()
        and ".." not in path.parts
        and not any(p.startswith(".") for p in path.parts)
        and re.fullmatch(r"[A-Za-z0-9_./-]+", name),
        "unsafe bundle path",
    )
    return name


def build(
    api_version,
    composition_commit,
    component_commits,
    files,
    *,
    dependencies,
    toolchains,
    licenses,
):
    check(api_version == VERSION, "unsupported bundle interface")
    check(
        re.fullmatch(r"[0-9a-f]{40}", composition_commit) is not None,
        "invalid composition revision",
    )
    check(
        set(component_commits) == COMPONENTS
        and all(
            re.fullmatch(r"[0-9a-f]{40}", value) for value in component_commits.values()
        ),
        "incomplete component pins",
    )
    check(
        type(files) is dict
        and 1 <= len(files) <= MAX_FILES
        and sum(len(v) for v in files.values()) <= MAX_BYTES,
        "bundle size limit",
    )
    for name, content in files.items():
        path_name(name)
        check(
            type(content) is bytes and len(content) <= 1_048_576,
            "bounded immutable bundle bytes required",
        )
    check(
        bool(dependencies) and bool(toolchains) and bool(licenses),
        "dependency/toolchain/rights inventory required",
    )
    for inventory in (dependencies, toolchains):
        check(
            type(inventory) is dict and len(inventory) <= 128,
            "bounded dependency inventory required",
        )
        for name, value in inventory.items():
            path_name(name)
            check(
                type(value) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", value),
                "floating or missing dependency digest",
            )
    check(
        type(licenses) is dict
        and all(
            name in files and sha(files[name]) == digest
            for name, digest in licenses.items()
        ),
        "license byte mismatch",
    )
    manifest = {
        "version": VERSION,
        "kind": "development_only",
        "target": "linux/arm64",
        "composition_commit": composition_commit,
        "component_commits": component_commits,
        "dependencies": dependencies,
        "toolchains": toolchains,
        "licenses": licenses,
        "files": {name: sha(content) for name, content in sorted(files.items())},
        "unsupported": UNSUPPORTED,
    }
    return canonical(
        {
            "manifest": manifest,
            "manifest_sha256": sha(canonical(manifest)),
            "files": {
                name: base64.b64encode(content).decode("ascii")
                for name, content in sorted(files.items())
            },
        }
    )


def verify(bundle, *, component_commits, target="linux/arm64"):
    check(type(bundle) is bytes and len(bundle) <= 2 * MAX_BYTES, "unbounded bundle")
    value = json.loads(bundle)
    check(
        type(value) is dict and set(value) == {"manifest", "manifest_sha256", "files"},
        "unsupported bundle fields",
    )
    manifest = value["manifest"]
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
        },
        "unsupported manifest fields",
    )
    check(manifest["target"] == target == "linux/arm64", "wrong target architecture")
    check(
        manifest["component_commits"] == component_commits, "composition pin mismatch"
    )
    check(
        manifest["unsupported"] == UNSUPPORTED
        and manifest["kind"] == "development_only",
        "unsupported product release claim",
    )
    files = {
        path_name(name): base64.b64decode(content, validate=True)
        for name, content in value["files"].items()
    }
    check(
        value["manifest_sha256"] == sha(canonical(manifest)),
        "manifest checksum mismatch",
    )
    check(
        manifest["files"] == {name: sha(data) for name, data in files.items()},
        "bundle checksum mismatch",
    )
    rebuilt = build(
        manifest["version"],
        manifest["composition_commit"],
        component_commits,
        files,
        dependencies=manifest["dependencies"],
        toolchains=manifest["toolchains"],
        licenses=manifest["licenses"],
    )
    check(rebuilt == bundle, "noncanonical bundle")
    return manifest, files, value["manifest_sha256"]


def actual_target():
    return (
        "linux/arm64"
        if platform.system() == "Linux" and platform.machine() == "aarch64"
        else "unsupported"
    )


class DisposableTarget:
    JOURNAL_STATES = ("intent", "verified")

    def __init__(self, path):
        root = Path(path)
        check(
            not root.is_symlink()
            and root.resolve().parent == Path(tempfile.gettempdir()).resolve()
            and root.name.startswith("runspindle-dev-target-"),
            "target is not a disposable temporary root",
        )
        self.root = root.resolve()
        check(
            (self.root / "disposable.json").is_file()
            and not (self.root / "disposable.json").is_symlink(),
            "missing disposable target marker",
        )
        marker = json.loads((self.root / "disposable.json").read_bytes())
        check(
            marker == {"version": VERSION, "target": actual_target()},
            "disposable target identity mismatch",
        )
        check(actual_target() == "linux/arm64", "unsupported execution target")
        check(
            not any(p.is_symlink() for p in self.root.rglob("*")),
            "symlink in disposable target",
        )
        self.db = sqlite3.connect(
            self.root / "journal.sqlite", isolation_level=None, timeout=5
        )
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA max_page_count=32768")
            self.db.execute("BEGIN IMMEDIATE")
            schema = self.db.execute("PRAGMA user_version").fetchone()[0]
            check(schema in (0, 1), "unsupported installation journal schema")
            if schema == 0:
                check(
                    self.db.execute(
                        "SELECT count(*) FROM sqlite_master WHERE type='table'"
                    ).fetchone()[0]
                    == 0,
                    "unversioned nonempty installation journal",
                )
                self.db.execute(
                    "CREATE TABLE state(id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER, active TEXT, pending TEXT)"
                )
                self.db.execute("INSERT INTO state VALUES(1,0,NULL,NULL)")
                self.db.execute(
                    "CREATE TABLE installs(digest TEXT PRIMARY KEY, bundle BLOB NOT NULL, predecessor TEXT, state TEXT NOT NULL)"
                )
                self.db.execute(
                    "CREATE TRIGGER bundle_immutable BEFORE UPDATE OF digest,bundle,predecessor ON installs BEGIN SELECT RAISE(ABORT,'immutable bundle'); END"
                )
                self.db.execute(
                    "CREATE TRIGGER bundle_retained BEFORE DELETE ON installs BEGIN SELECT RAISE(ABORT,'retained bundle'); END"
                )
                self.db.execute("PRAGMA user_version=1")
            self._check_journal_kind()
            self.db.commit()
        except BaseException:
            self.db.rollback()
            self.db.close()
            raise

    @classmethod
    def create(cls):
        check(actual_target() == "linux/arm64", "unsupported execution target")
        path = Path(tempfile.mkdtemp(prefix="runspindle-dev-target-"))
        try:
            (path / "disposable.json").write_bytes(
                canonical({"version": VERSION, "target": actual_target()})
            )
            return cls(path)
        except BaseException:
            shutil.rmtree(path)
            raise

    def close(self):
        self.db.close()

    def cleanup(self):
        self.close()
        shutil.rmtree(self.root)

    def _check_journal_kind(self):
        placeholders = ",".join("?" for _ in self.JOURNAL_STATES)
        check(
            self.db.execute(
                "SELECT count(*) FROM installs WHERE state NOT IN ("
                + placeholders
                + ")",
                self.JOURNAL_STATES,
            ).fetchone()[0]
            == 0,
            "unsupported installation journal payload kind",
        )

    def state(self):
        self._check_journal_kind()
        generation, active, pending = self.db.execute(
            "SELECT generation,active,pending FROM state WHERE id=1"
        ).fetchone()
        return {"generation": generation, "active": active, "pending": pending}

    def install(
        self,
        api_version,
        bundle,
        *,
        component_commits,
        expected_generation,
        checkpoint=lambda _: None,
    ):
        check(api_version == VERSION, "unsupported installation version")
        manifest, files, digest = verify(
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
                self.db.execute("SELECT count(*) FROM installs").fetchone()[0] < 1000,
                "installation journal capacity",
            )
            prior = self.db.execute(
                "SELECT bundle FROM installs WHERE digest=?", (digest,)
            ).fetchone()
            check(
                self.db.execute(
                    "SELECT coalesce(sum(length(bundle)),0) FROM installs"
                ).fetchone()[0]
                + (0 if prior else len(bundle))
                <= 67108864,
                "bundle storage capacity",
            )
            if prior:
                check(prior[0] == bundle, "immutable install bundle conflict")
            else:
                self.db.execute(
                    "INSERT INTO installs VALUES(?,?,?,'intent')",
                    (digest, bundle, state["active"]),
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

    def _observed(self):
        pointer = self.root / "active.json"
        if not pointer.exists():
            return None
        check(not pointer.is_symlink(), "unsafe active pointer")
        value = json.loads(pointer.read_bytes())
        check(
            type(value) is dict
            and set(value) == {"manifest_sha256"}
            and re.fullmatch(r"[0-9a-f]{64}", value["manifest_sha256"]),
            "invalid active pointer",
        )
        return value["manifest_sha256"]

    def verify_installed(self, component_commits):
        digest = self._observed()
        check(digest is not None, "no installed development bundle")
        row = self.db.execute(
            "SELECT bundle FROM installs WHERE digest=?", (digest,)
        ).fetchone()
        check(row is not None, "unrecognized installed version")
        manifest, files, expected = verify(
            row[0], component_commits=component_commits, target=actual_target()
        )
        folder = self.root / "versions" / digest
        actual = {
            str(p.relative_to(folder)): sha(p.read_bytes())
            for p in folder.rglob("*")
            if p.is_file()
        }
        check(
            not any(p.is_symlink() for p in folder.rglob("*"))
            and actual == manifest["files"],
            "installed byte mismatch",
        )
        check(expected == digest, "installed manifest identity mismatch")
        return {
            "manifest_sha256": digest,
            "files": len(files),
            "unsupported": manifest["unsupported"],
        }

    def recover(self, api_version, *, component_commits, checkpoint=lambda _: None):
        check(api_version == VERSION, "unsupported recovery version")
        # Own the journal across filesystem reconciliation, fencing concurrent recovery.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            state = self.state()
            pending = state["pending"]
            if pending is None:
                result = self.verify_installed(component_commits)
            else:
                row = self.db.execute(
                    "SELECT bundle,predecessor FROM installs WHERE digest=?", (pending,)
                ).fetchone()
                manifest, files, digest = verify(
                    row[0], component_commits=component_commits, target=actual_target()
                )
                observed = self._observed()
                check(
                    observed in (state["active"], pending),
                    "conflicting installation readback",
                )
                if observed != pending:
                    folder = self.root / "versions" / digest
                    folder.mkdir(parents=True, exist_ok=True)
                    for index, (name, data) in enumerate(sorted(files.items())):
                        path = folder / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        check(
                            not any(p.is_symlink() for p in self.root.rglob("*")),
                            "unsafe staged target",
                        )
                        with path.open("wb") as stream:
                            stream.write(data)
                            stream.flush()
                            os.fsync(stream.fileno())
                        if index == 0:
                            checkpoint("after_first_installed_file")
                    check(
                        {
                            str(p.relative_to(folder)): sha(p.read_bytes())
                            for p in folder.rglob("*")
                            if p.is_file()
                        }
                        == manifest["files"],
                        "staging byte mismatch",
                    )
                    temporary = self.root / "active.next"
                    with temporary.open("wb") as stream:
                        stream.write(canonical({"manifest_sha256": digest}))
                        stream.flush()
                        os.fsync(stream.fileno())
                    temporary.replace(self.root / "active.json")
                    checkpoint("after_installed_pointer")
                result = self.verify_installed(component_commits)
                self.db.execute(
                    "UPDATE installs SET state='verified' WHERE digest=?", (pending,)
                )
                self.db.execute(
                    "UPDATE state SET active=?,pending=NULL WHERE id=1", (pending,)
                )
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def rollback(self, api_version, *, component_commits, expected_generation):
        check(api_version == VERSION, "unsupported rollback version")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            state = self.state()
            check(
                state["generation"] == expected_generation
                and state["pending"] is None
                and state["active"] is not None,
                "rollback ownership conflict",
            )
            self.verify_installed(component_commits)
            row = self.db.execute(
                "SELECT predecessor FROM installs WHERE digest=?", (state["active"],)
            ).fetchone()
            check(row and row[0], "no installed predecessor")
            predecessor = row[0]
            # Verify predecessor files before replacing the active pointer.
            stored = self.db.execute(
                "SELECT bundle FROM installs WHERE digest=? AND state='verified'",
                (predecessor,),
            ).fetchone()
            check(stored is not None, "predecessor unavailable")
            manifest, files, _ = verify(
                stored[0], component_commits=component_commits, target=actual_target()
            )
            folder = self.root / "versions" / predecessor
            check(
                {
                    str(p.relative_to(folder)): sha(p.read_bytes())
                    for p in folder.rglob("*")
                    if p.is_file()
                }
                == manifest["files"],
                "rollback bytes unavailable",
            )
            # Journal first; recovery can finish the exact pointer change after interruption.
            self.db.execute(
                "UPDATE state SET generation=generation+1,pending=? WHERE id=1",
                (predecessor,),
            )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return self.recover(api_version, component_commits=component_commits)
