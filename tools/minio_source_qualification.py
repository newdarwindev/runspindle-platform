#!/usr/bin/env python3
"""Read-only qualification of the selected official MinIO source/dependency cache.

Acquisition is separate. This helper never builds or starts an unqualified store.
Incomplete original notice evidence returns 78 and keeps Langfuse blocked.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

REVISION = "0d7408fc9969caf07de6a8c3a84f9fbb10a6739e"
RELEASE = "RELEASE.2025-04-22T22-12-26Z"
TOOLCHAIN = (
    "golang@sha256:79390b5e5af9ee6e7b1173ee3eac7fadf6751a545297672916b59bfa0ecf6f71"
)
LOCALEREADER = "github.com/mattn/go-localereader"
LOCALEREADER_RELEASE = "6338b4c69507fb1f9edba3db33882a8e9ab9bfa8"
LOCALEREADER_NOTICE_REVISION = "2491eb6c1c75720122ef321ed7acc3a8d9de95b1"
LOCALEREADER_NOTICE = Path("telemetry/notices/go-localereader-MIT-LICENSE")
LOCALEREADER_NOTICE_SHA256 = (
    "dceede03550621e65ec7b79fcba87cf6dbb9d31132a8ae71a8fe8ceec0af7da3"
)
LOCALEREADER_README_SHA256 = (
    "0c5c52517f13becd7a1e1234f1e8a5ba370f5a5078cee9ac058c2d534e267fcb"
)
LOCALEREADER_PIN = {
    "module": LOCALEREADER,
    "version": "v0.0.1",
    "go_sum": "h1:ygSAOl7ZXTx4RdPYinUpg6W99U8jWvWi9Ye2JC/oIi4=",
    "go_mod_sum": "h1:8fBrzywKY7BI3czFoHkuzRoWE9C+EiG4R1k4Cjx5p88=",
    "archive_sha256": "aa67306797b071ce93188fe2834f63ffd7963faf623d49229d891ef52e595b35",
    "go_mod_sha256": "f7e77590fe4e1f139df43931a85df4205026ea7110d9e0428c11653030fa5f64",
}
ROOT = Path(__file__).resolve().parents[1]


def sha(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def localereader_notice(entry: dict[str, Any], readme: Path) -> dict[str, str] | None:
    notice = ROOT / LOCALEREADER_NOTICE
    if (
        any(entry.get(key) != value for key, value in LOCALEREADER_PIN.items())
        or not readme.is_file()
        or sha(readme) != LOCALEREADER_README_SHA256
        or not notice.is_file()
        or sha(notice) != LOCALEREADER_NOTICE_SHA256
    ):
        return None
    return {
        "basis": "upstream maintainer merged the complete MIT notice with no changes to the tagged source files",
        "release_revision": LOCALEREADER_RELEASE,
        "notice_revision": LOCALEREADER_NOTICE_REVISION,
        "notice_path": str(LOCALEREADER_NOTICE),
        "notice_sha256": LOCALEREADER_NOTICE_SHA256,
    }


def qualify(source: str | Path, prepared: str | Path) -> dict[str, Any]:
    source, prepared = Path(source), Path(prepared)

    def git(*args: str) -> str:
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
        inherited: dict[str, Any] | None = None
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
        archive = Path(row["Zip"].replace("/prepared", str(prepared)))
        gomod = Path(row["GoMod"].replace("/prepared", str(prepared)))
        entry = {
            "module": row["Path"],
            "version": row["Version"],
            "go_sum": row["Sum"],
            "go_mod_sum": row["GoModSum"],
            "archive_sha256": sha(archive),
            "go_mod_sha256": sha(gomod),
        }
        if row["Path"] == LOCALEREADER:
            # This release has no in-archive notice. An extra file in the
            # extracted cache cannot substitute for the reviewed upstream bytes.
            notices = {}
            inherited = localereader_notice(entry, base / "README.md")
        if not notices and not inherited:
            unresolved.append(
                {
                    "module": row["Path"],
                    "version": row["Version"],
                    "reason": "missing or mismatched original module notice",
                    "readme_sha256": sha(base / "README.md")
                    if (base / "README.md").is_file()
                    else None,
                }
            )
        rows.append(
            {
                **entry,
                "notices": notices,
                "inherited_notice": inherited,
            }
        )
    if len(rows) != 256 or sum(row["module"] == LOCALEREADER for row in rows) != 1:
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


def main() -> int:
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
