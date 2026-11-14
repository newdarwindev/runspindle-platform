#!/usr/bin/env python3
"""Readiness and evidence commands for the isolated runner fleet.

Exit 2 means blocked, never successful deployment. No runner is registered by
the Mac preparation tools; Strix uses the disposable host manager.
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from runner_fleet.model import config, empty_report
from runner_fleet.preflight import inspect_host
from runner_fleet.evidence import collect


def write_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".fleet-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "manage",
            "preflight",
            "install",
            "status",
            "drain",
            "resume",
            "qualify",
            "uninstall",
            "image-status",
        ),
    )
    parser.add_argument("--host", choices=("strix", "macbook"), required=True)
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".local/state/runspindle-fleet"
    )
    parser.add_argument(
        "--revisions",
        type=Path,
        help="JSON map of repository short name to exact 40-character commit SHA",
    )
    parser.add_argument(
        "--receipts",
        type=Path,
        help="JSON map of owner/repo/lane to GitHub run ID; fetched and verified",
    )
    parser.add_argument("--ref", default="ci/strix-push-validation", help="Trusted qualification branch")
    parser.add_argument("--image", type=Path, default=Path('/var/lib/runspindle-fleet/images/qualified.qcow2'))
    parser.add_argument("--image-manifest", type=Path, default=Path('/var/lib/runspindle-fleet/images/qualified.json'))
    args = parser.parse_args(argv)
    cfg = config()
    if args.command == 'image-status':
        from runner_fleet.lifecycle import image_matrix
        result = image_matrix(cfg, Path(__file__).resolve().parents[1] / 'fleet/image-inputs.json',
                              args.image, args.image_manifest)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 2  # Byte identity cannot qualify any target lane.
    state = args.state_dir.expanduser().resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    control = state / (args.host + "-control.json")
    if args.host == "strix" and args.command in ("manage", "install", "preflight", "status", "drain", "resume", "uninstall"):
        from runner_fleet import service
        if args.command == "manage":
            from runner_fleet.manager import Manager
            Manager(cfg, state).run()
            return 0
        if args.command in ("preflight", "install", "uninstall"):
            result = getattr(service, args.command)(cfg, state)
        elif args.command == "status":
            result = service.observed(state)
        else:
            result = service.control(cfg, state, args.command)
    elif args.command == "manage":
        result = {"status": "blocked", "reason": "Mac managers remain pending"}
    elif args.command in ("drain", "resume"):
        # This is desired state only. Never advertise drained until a real
        # manager confirms that active resources and registrations are absent.
        result = {
            "host": args.host,
            "desired_state": "drained" if args.command == "drain" else "running",
            "observed_state": "not_installed",
        }
        write_json(control, result)
    elif args.command == "status":
        result = {
            "host": args.host,
            "status": "blocked",
            "manager": "not_implemented",
            "service": "not_installed",
            "control": json.loads(control.read_text()) if control.exists() else None,
        }
    elif args.command in ("preflight", "install"):
        result = inspect_host(cfg, args.host, state)
        write_json(state / (args.host + "-preflight.json"), result)
        if args.command == "install":
            result["status"] = "blocked"
            result["action"] = (
                "installation refused; disposable backends and isolation remain unqualified"
            )
    elif args.command == "uninstall":
        result = {
            "status": "blocked",
            "reason": "No fleet service has been installed by this tool; no external resources touched; logs retained",
        }
    else:
        revisions = json.loads(args.revisions.read_text()) if args.revisions else {}
        if not isinstance(revisions, dict) or any(
            repo not in cfg["repositories"]
            or not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
            for repo, sha in revisions.items()
        ):
            parser.error(
                "revisions must map allowlisted repository names to exact lowercase commit SHAs"
            )
        if args.receipts:
            if set(revisions) != set(cfg["repositories"]):
                parser.error(
                    "collecting receipts requires exact revisions for all eight repositories"
                )
            receipts = json.loads(args.receipts.read_text())
            valid_keys = {
                f'{cfg["owner"]}/{repo}/{lane}'
                for repo in cfg["repositories"]
                for lane in cfg["lanes"]
            }
            if not isinstance(receipts, dict) or not set(receipts) <= valid_keys:
                parser.error("receipts contain an unknown repository/lane")
            result = collect(cfg, revisions, receipts, state)
        elif args.host == "strix" and revisions:
            from runner_fleet.service import qualify
            result = qualify(cfg, revisions, state, args.ref)
        else:
            result = empty_report(cfg, revisions)
        write_json(state / "qualification.json", result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result.get("status") == "blocked" else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"status": "blocked", "reason": str(error)}))
        sys.exit(2)
