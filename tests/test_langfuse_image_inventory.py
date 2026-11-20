import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import langfuse_image_inventory as inventory


def synthetic(
    architecture: str = "arm64", diff_ids: list[str] | None = None
) -> tuple[str, bytes, bytes]:
    config = json.dumps(
        {
            "os": "linux",
            "architecture": architecture,
            "rootfs": {
                "type": "layers",
                "diff_ids": [inventory.digest(b"uncompressed")]
                if diff_ids is None
                else diff_ids,
            },
            "config": {"Env": ["FIXTURE_SYNTHETIC_SECRET=canary-value"]},
        }
    ).encode()
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": inventory.IMAGE_MANIFEST,
            "config": {
                "digest": inventory.digest(config),
                "size": len(config),
                "mediaType": inventory.IMAGE_CONFIG,
            },
            "layers": [
                {
                    "digest": inventory.digest(b"compressed"),
                    "size": 42,
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                }
            ],
        }
    ).encode()
    return f"synthetic/site@{inventory.digest(manifest)}", manifest, config


class ImageInventoryTests(unittest.TestCase):
    def test_pinned_inventory_keeps_ordered_digests_without_config_values(self) -> None:
        reference, manifest, config = synthetic()
        result = inventory.inspect_image(reference, manifest, config)
        self.assertNotIn("canary-value", json.dumps(result))
        self.assertEqual(result["config"]["digest"], inventory.digest(config))
        self.assertEqual(result["layers"][0]["digest"], inventory.digest(b"compressed"))
        self.assertEqual(
            result["layers"][0]["uncompressed_digest"],
            inventory.digest(b"uncompressed"),
        )

    def test_tampered_manifest_or_config_fails_before_using_metadata(self) -> None:
        reference, manifest, config = synthetic()
        with self.assertRaisesRegex(ValueError, "manifest digest mismatch"):
            inventory.inspect_image(reference, manifest + b" ", config)
        with self.assertRaisesRegex(
            ValueError, "configuration size or digest mismatch"
        ):
            inventory.inspect_image(
                reference, manifest, config.replace(b"arm64", b"amd64")
            )

    def test_valid_digest_cannot_authorize_another_architecture(self) -> None:
        with self.assertRaisesRegex(ValueError, "configuration target mismatch"):
            inventory.inspect_image(*synthetic(architecture="amd64"))

    def test_layer_counts_and_hashes_must_agree(self) -> None:
        for diff_ids in (
            [],
            ["wrong"],
            [inventory.digest(b"one"), inventory.digest(b"two")],
        ):
            with (
                self.subTest(diff_ids=diff_ids),
                self.assertRaisesRegex(ValueError, "invalid OCI layer inventory"),
            ):
                inventory.inspect_image(*synthetic(diff_ids=diff_ids))

    def test_pinned_duplicate_members_are_rejected(self) -> None:
        _, manifest, config = synthetic()
        malformed = b'{"schemaVersion":2,' + manifest[1:]
        with self.assertRaisesRegex(ValueError, "duplicate manifest key"):
            inventory.inspect_image(
                f"synthetic/site@{inventory.digest(malformed)}", malformed, config
            )

    def test_all_roles_required_and_metadata_alone_cannot_qualify_execution(
        self,
    ) -> None:
        reference, manifest, config = synthetic()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            pin = root / "pins.json"
            pin.write_text(
                json.dumps(
                    {
                        "target": inventory.TARGET,
                        "source_revision": "a" * 40,
                        "images": {
                            **dict.fromkeys(inventory.ROLES, reference),
                            "minio": None,
                        },
                    }
                )
            )
            with patch.object(inventory, "PIN", pin):
                for role in inventory.ROLES:
                    (root / f"{role}.manifest.json").write_bytes(manifest)
                    (root / f"{role}.config.json").write_bytes(config)
                result = inventory.inventory(root)
                self.assertEqual(list(result["images"]), list(inventory.ROLES))
                for boundary in (
                    "layer_content_verification",
                    "package_inventory",
                    "service_execution",
                    "langfuse_readback",
                ):
                    self.assertEqual(result[boundary], "not_run")
                self.assertEqual(result["minio"], "source_fallback")
                (root / "worker.config.json").unlink()
                with self.assertRaises(FileNotFoundError):
                    inventory.inventory(root)


if __name__ == "__main__":
    unittest.main()
