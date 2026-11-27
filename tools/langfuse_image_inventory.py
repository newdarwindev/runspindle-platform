#!/usr/bin/env python3
"""Verify captured OCI metadata for the five pinned Langfuse dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.runner_fleet.closure_gap import read_regular, unique  # noqa: E402

PIN = ROOT / "telemetry/langfuse-qualification.json"
ROLES = ("web", "worker", "postgres", "redis", "clickhouse")
TARGET = "linux/arm64"
MAX_METADATA = 65536
IMAGE_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
IMAGE_CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER_TYPES = {
    "application/vnd.oci.image.layer.v1.tar",
    "application/vnd.oci.image.layer.v1.tar+gzip",
    "application/vnd.oci.image.layer.v1.tar+zstd",
}
SHA256 = re.compile(r"sha256:[a-f0-9]{64}")
IMAGE_PIN = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*@(sha256:[a-f0-9]{64})")


def digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def object_value(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("expected OCI metadata object")
    return value


def read_json(raw: bytes) -> dict[str, Any]:
    return object_value(json.loads(raw, object_pairs_hook=unique))


def valid_digest(value: object) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def descriptor(value: object, media_types: set[str]) -> dict[str, Any]:
    row = object_value(value)
    digest_value, size, media_type = (
        row.get("digest"),
        row.get("size"),
        row.get("mediaType"),
    )
    if (
        not valid_digest(digest_value)
        or type(size) is not int
        or not 0 < size <= (1 << 63) - 1
        or not isinstance(media_type, str)
        or media_type not in media_types
    ):
        raise ValueError("invalid OCI descriptor")
    return {"digest": digest_value, "bytes": size, "media_type": media_type}


def inspect_image(
    reference: str, manifest_raw: bytes, config_raw: bytes
) -> dict[str, Any]:
    match = IMAGE_PIN.fullmatch(reference)
    if match is None or digest(manifest_raw) != match[1]:
        raise ValueError("pinned image manifest digest mismatch")
    manifest = read_json(manifest_raw)
    if (
        type(manifest.get("schemaVersion")) is not int
        or manifest["schemaVersion"] != 2
        or manifest.get("mediaType") != IMAGE_MANIFEST
    ):
        raise ValueError("expected pinned single-platform OCI image manifest")
    config_descriptor = descriptor(manifest.get("config"), {IMAGE_CONFIG})
    if config_descriptor["bytes"] != len(config_raw) or config_descriptor[
        "digest"
    ] != digest(config_raw):
        raise ValueError("OCI configuration size or digest mismatch")
    config = read_json(config_raw)
    if (config.get("os"), config.get("architecture")) != ("linux", "arm64"):
        raise ValueError("OCI configuration target mismatch")
    layers = manifest.get("layers")
    rootfs = object_value(config.get("rootfs"))
    diff_ids = rootfs.get("diff_ids")
    if (
        type(layers) is not list
        or not 1 <= len(layers) <= 128
        or rootfs.get("type") != "layers"
        or type(diff_ids) is not list
        or len(diff_ids) != len(layers)
        or not all(valid_digest(value) for value in diff_ids)
    ):
        raise ValueError("invalid OCI layer inventory")
    return {
        "reference": reference,
        "manifest_bytes": len(manifest_raw),
        "config": config_descriptor,
        "target": TARGET,
        "layers": [
            dict(descriptor(layer, LAYER_TYPES), uncompressed_digest=diff_id)
            for layer, diff_id in zip(layers, diff_ids, strict=True)
        ],
    }


def inventory(metadata: Path) -> dict[str, Any]:
    pins_raw = read_regular(PIN, MAX_METADATA)
    pins = read_json(pins_raw)
    images = object_value(pins.get("images"))
    revision = pins.get("source_revision")
    if (
        pins.get("target") != TARGET
        or not isinstance(revision, str)
        or re.fullmatch(r"[a-f0-9]{40}", revision) is None
        or set(images) != {*ROLES, "minio"}
        or images["minio"] is not None
    ):
        raise ValueError("unexpected Langfuse dependency pins")
    root = metadata.resolve(strict=True)
    rows = {}
    for role in ROLES:
        reference = images[role]
        if type(reference) is not str:
            raise ValueError("missing Langfuse dependency pin")
        rows[role] = inspect_image(
            reference,
            read_regular(root / f"{role}.manifest.json", MAX_METADATA),
            read_regular(root / f"{role}.config.json", MAX_METADATA),
        )
    return {
        "schema_version": 1,
        "evidence_class": "read_only_registry_metadata",
        "status": "pinned_metadata_verified",
        "target": TARGET,
        "langfuse_source_revision": revision,
        "images": rows,
        "minio": "source_fallback",
        "layer_content_verification": "not_run",
        "package_inventory": "not_run",
        "service_execution": "not_run",
        "langfuse_readback": "not_run",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = inventory(args.metadata)
    except (OSError, ValueError, RecursionError):
        print("invalid or incomplete Langfuse image metadata", file=sys.stderr)
        return 78
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
