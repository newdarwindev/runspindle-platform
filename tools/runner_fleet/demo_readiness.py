"""Offline policy/config proposal only; never changes fleet files or enrolls jobs."""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from . import model

REPOSITORY = "runspindle-demo-suite"
CANDIDATE_REPOSITORIES = model.REPOSITORIES + (REPOSITORY,)
REQUIRED_TOOLS = ("python3", "node", "npm", "git", "gh", "ffmpeg", "ffprobe")


def validate_candidate(
    config: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    """Validate the exact paired proposal, without widening the active policy."""
    expected = {
        "owner": "newdarwindev",
        "repositories": list(CANDIDATE_REPOSITORIES),
        "activation_allowed": False,
    }
    if policy != expected or policy.get("activation_allowed") is not False:
        raise ValueError("Unexpected demo candidate policy")
    model.validate_config(config, candidate_policy=policy)
    lane = config.get("lanes", {}).get("strix-linux-x64")
    if not lane or lane.get("architecture") != "X64" or lane.get("host") != "strix":
        raise ValueError("Expected isolated Strix x64 lane")
    if not {"self-hosted", "linux", "x64", "runspindle-isolated", "strix"} <= {
        label.lower() for label in lane.get("labels", [])
    }:
        raise ValueError("Expected explicit isolated Linux x64 labels")
    return config


def prepare(
    config: dict[str, Any],
    tools: Mapping[str, bool],
    browser_revision: str | None,
    playwright_version: str | None,
) -> dict[str, Any]:
    model.validate_config(config)
    candidate = deepcopy(config)
    candidate["repositories"] = list(CANDIDATE_REPOSITORIES)
    policy = {
        "owner": "newdarwindev",
        "repositories": list(CANDIDATE_REPOSITORIES),
        "activation_allowed": False,
    }
    validate_candidate(candidate, policy)
    blockers = [
        "missing tool: " + tool for tool in REQUIRED_TOOLS if not tools.get(tool)
    ]
    if browser_revision != "1194":
        blockers.append("Playwright Chromium/headless-shell revision 1194 unqualified")
    if playwright_version != "1.56.1":
        blockers.append("locked Playwright 1.56.1 unavailable")
    return {
        "scope": "offline proposal only",
        "activation_allowed": False,
        "candidate_config": candidate,
        "candidate_policy": policy,
        "prerequisite_blockers": blockers,
        "mandatory_handoffs": [
            "Owner reviews matching policy and configuration repository tuples",
            "Owner qualifies exact guest image and sandboxed browser/media dependency closure",
            "Owner separately approves any necessary network/security change",
            "Owner personally enables persistent demo fleet enrollment; this tool cannot do so",
        ],
        "qualification": [
            "exact parent/submodule pins",
            "Python contract tests",
            "desktop/mobile branding",
            "seven normal recordings",
            "fault/recovery matrix",
            "evaluation rollback",
            "package hashes and gallery playback",
            "ephemeral runner teardown evidence",
        ],
    }
