import gzip
import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from tools import langfuse_image_layers as layers


def archive(entries: list[tuple[str, bytes]]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for path, data in entries:
            member = tarfile.TarInfo(path)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    return raw.getvalue()


def sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class LayerInventoryTests(unittest.TestCase):
    def test_whiteouts_hide_lower_layers_and_keep_same_layer_package(self) -> None:
        replaced = "app/node_modules/a/package.json"
        removed = "app/node_modules/b/package.json"
        other = "other/node_modules/c/package.json"
        raw = archive(
            [
                (replaced, b"new"),
                ("app/node_modules/.wh..wh..opq", b""),
                ("other/node_modules/.wh.c", b""),
            ]
        )
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            visible = layers.overlay(
                tar, {replaced: b"old", removed: b"old", other: b"old"}
            )
        self.assertEqual(visible, {replaced: b"new"})

    def test_non_directory_replacement_masks_lower_descendants(self) -> None:
        raw = archive([("app", b"replacement")])
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            self.assertEqual(
                layers.overlay(tar, {"app/node_modules/a/package.json": b"old"}), {}
            )

    def test_duplicate_and_traversing_names_are_rejected(self) -> None:
        for entries in [[("../etc/escape", b"x")], [("same", b"x"), ("./same", b"y")]]:
            with (
                self.subTest(entries=entries),
                tarfile.open(fileobj=io.BytesIO(archive(entries))) as tar,
            ):
                with self.assertRaises(ValueError):
                    layers.overlay(tar, {})

    def test_pinned_hashes_cover_compressed_and_expanded_bytes(self) -> None:
        expanded = archive([(layers.DPKG, b"synthetic")]) + b"trailing padding"
        compressed = gzip.compress(expanded, mtime=0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            checksum = sha(compressed)
            path = root / checksum.split(":")[1]
            path.write_bytes(compressed)
            desc = {
                "media_type": layers.GZIP_LAYER,
                "digest": checksum,
                "bytes": len(compressed),
                "uncompressed_digest": sha(expanded),
            }
            retained, size = layers.layer(root, desc, {})
            self.assertEqual(retained, {layers.DPKG: b"synthetic"})
            self.assertEqual(size, len(expanded))
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                layers.layer(
                    root, dict(desc, uncompressed_digest=sha(b"different")), {}
                )
            wrong = sha(b"other layer")
            (root / wrong.split(":")[1]).write_bytes(compressed)
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                layers.layer(root, dict(desc, digest=wrong), {})

    def test_package_hardlink_uses_bytes_from_earlier_same_layer_member(self) -> None:
        alias = "app/node_modules/pkg/package.json"
        original = "app/store/preserved.json"
        metadata = b'{"name":"pkg","version":"1.0.0"}'
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            asset = tarfile.TarInfo(original)
            asset.size = len(metadata)
            tar.addfile(asset, io.BytesIO(metadata))
            link = tarfile.TarInfo(alias)
            link.type = tarfile.LNKTYPE
            link.linkname = original
            tar.addfile(link)
        expanded = buffer.getvalue()
        compressed = gzip.compress(expanded, mtime=0)
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve()
            checksum = sha(compressed)
            (directory / checksum.split(":")[1]).write_bytes(compressed)
            descriptor = {
                "media_type": layers.GZIP_LAYER,
                "digest": checksum,
                "bytes": len(compressed),
                "uncompressed_digest": sha(expanded),
            }
            self.assertEqual(
                layers.layer(directory, descriptor, {}),
                ({alias: metadata}, len(expanded)),
            )

    def test_installed_package_records_exclude_removed_records_and_config_values(
        self,
    ) -> None:
        database = (
            b"Package: present\nStatus: install ok installed\nVersion: 2:1.0-2\nArchitecture: arm64\n\n"
            b"Package: removed\nStatus: deinstall ok config-files\nVersion: 3\nArchitecture: arm64\n"
        )
        retained = {
            layers.DPKG: database,
            "app/node_modules/@synthetic/package/package.json": b'{"name":"@synthetic/package","version":"1.2.3","extra":"secret-canary"}',
        }
        self.assertEqual(
            layers.os_packages(retained)["packages"],
            [{"name": "present", "version": "2:1.0-2", "architecture": "arm64"}],
        )
        self.assertEqual(
            layers.node_packages(retained),
            {
                "packages": [
                    {"name": "@synthetic/package", "version": "1.2.3", "manifests": 1}
                ],
                "manifests_without_name_or_version": 0,
            },
        )
        self.assertEqual(
            layers.os_packages({layers.APK: b"P:apk-tools\nV:2.14.9-r0\nA:aarch64\n"})[
                "manager"
            ],
            "apk",
        )


if __name__ == "__main__":
    unittest.main()
