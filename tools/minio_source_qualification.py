#!/usr/bin/env python3
"""Read-only qualification of the selected official MinIO source/dependency cache.

Acquisition is separate. This helper never builds or starts an unqualified store.
Incomplete original notice evidence returns 78 and keeps Langfuse blocked.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

REVISION = "0d7408fc9969caf07de6a8c3a84f9fbb10a6739e"
RELEASE = "RELEASE.2025-04-22T22-12-26Z"
TOOLCHAIN = (
    "golang@sha256:79390b5e5af9ee6e7b1173ee3eac7fadf6751a545297672916b59bfa0ecf6f71"
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def qualify(source, prepared):
    source, prepared = Path(source), Path(prepared)

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=source, text=True).strip()

    if (
        git("rev-parse", "HEAD") != REVISION
        or git("rev-parse", RELEASE + "^{commit}") != REVISION
        or git("status", "--porcelain")
    ):
        raise ValueError("selected MinIO source revision/bytes mismatch")
    if git("diff", REVISION, "--"):
        raise ValueError("changed MinIO source")
    text = (prepared / "modules.json").read_text()
    decoder = json.JSONDecoder()
    rows, unresolved = [], []
    while text.strip():
        row, end = decoder.raw_decode(text.lstrip())
        text = text.lstrip()[end:]
        if "Error" in row or not row.get("Sum") or not row.get("GoModSum"):
            raise ValueError("incomplete dependency acquisition")
        base = Path(row["Dir"].replace("/prepared", str(prepared)))
        notices = {
            str(p.relative_to(base)): sha(p)
            for p in base.iterdir()
            if p.is_file()
            and p.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "UNLICENSE"))
        }
        inherited = None
        if not notices and row["Path"] in (
            "github.com/minio/csvparser",
            "github.com/minio/colorjson",
            "github.com/minio/filepath",
        ):
            headers = {
                p.name: sha(p)
                for p in base.glob("*.go")
                if "The Go Authors" in p.read_text()[:250]
                and "golang.org/LICENSE" in p.read_text()[:250]
            }
            if headers:
                inherited = {
                    "basis": "original source headers explicitly reference Go BSD license",
                    "source_files": headers,
                    "notice_sha256": sha(prepared / "Go-LICENSE"),
                }
        if not notices and not inherited:
            unresolved.append(
                {
                    "module": row["Path"],
                    "version": row["Version"],
                    "reason": "module README declares MIT, but original complete copyright/permission notice is absent; exact upstream tag LICENSE returned HTTP 404",
                    "readme_sha256": sha(base / "README.md"),
                }
            )
        archive = Path(row["Zip"].replace("/prepared", str(prepared)))
        gomod = Path(row["GoMod"].replace("/prepared", str(prepared)))
        rows.append(
            {
                "module": row["Path"],
                "version": row["Version"],
                "go_sum": row["Sum"],
                "go_mod_sum": row["GoModSum"],
                "archive_sha256": sha(archive),
                "go_mod_sha256": sha(gomod),
                "notices": notices,
                "inherited_notice": inherited,
            }
        )
    if len(rows) != 256:
        raise ValueError("incomplete module inventory")
    return {
        "schema_version": 1,
        "status": "blocked" if unresolved else "prepared_source_only",
        "evidence_class": "read_only_dependency_preparation",
        "release": RELEASE,
        "source_revision": REVISION,
        "source": "https://github.com/minio/minio/tree/" + REVISION,
        "go_mod_sha256": sha(source / "go.mod"),
        "go_sum_sha256": sha(source / "go.sum"),
        "license": "AGPL-3.0-only; original rights and notices retained",
        "source_license_sha256": sha(source / "LICENSE"),
        "credits_sha256": sha(source / "CREDITS"),
        "toolchain": TOOLCHAIN,
        "go_version": "1.24.2",
        "go_notice_sha256": sha(prepared / "Go-LICENSE"),
        "modules": rows,
        "unresolved_notices": unresolved,
        "build": "not_run",
        "service_execution": "not_run",
        "Langfuse_readback": "not_run",
        "no_substitute_backend": True,
        "no_new_credentials": True,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--prepared", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    report = qualify(args.source, args.prepared)
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "source_revision",
                    "unresolved_notices",
                    "build",
                    "service_execution",
                )
            }
        )
    )
    return 78 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
