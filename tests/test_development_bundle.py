import base64
import json
import subprocess
import sys
import unittest

from tools.development_bundle import (
    VERSION,
    COMPONENTS,
    UNSUPPORTED,
    DisposableTarget,
    actual_target,
    build,
    canonical,
    sha,
    verify,
)

PINS = {name: sha(name.encode())[:40] for name in COMPONENTS}


def bundle(content=b"first"):
    files = {
        "component/example.txt": content,
        "notices/rights.txt": b"Synthetic fixture; original rights retained",
    }
    return build(
        VERSION,
        "a" * 40,
        PINS,
        files,
        dependencies={"synthetic-contract": "sha256:" + sha(b"dependency")},
        toolchains={"python": "sha256:" + sha(b"toolchain")},
        licenses={"notices/rights.txt": sha(files["notices/rights.txt"])},
    )


class BundleTests(unittest.TestCase):
    def test_exact_pins_checksums_target_and_complete_license_bytes(self):
        content = bundle()
        manifest, files, digest = verify(content, component_commits=PINS)
        self.assertEqual(manifest["unsupported"], UNSUPPORTED)
        self.assertEqual(files["component/example.txt"], b"first")
        self.assertEqual(sha(canonical(manifest)), digest)
        for changed in ({**PINS, "core": "b" * 40}, {}):
            with self.assertRaises(ValueError):
                verify(content, component_commits=changed)
        with self.assertRaises(ValueError):
            verify(content, component_commits=PINS, target="darwin/arm64")
        value = json.loads(content)
        value["files"]["component/example.txt"] = base64.b64encode(b"changed").decode()
        with self.assertRaises(ValueError):
            verify(canonical(value), component_commits=PINS)
        value = json.loads(content)
        value["manifest"]["version"] = "future"
        value["manifest_sha256"] = sha(canonical(value["manifest"]))
        with self.assertRaises(ValueError):
            verify(canonical(value), component_commits=PINS)

    def test_traversal_floating_dependency_unavailable_rights_reject_before_target(
        self,
    ):
        for files, deps, licenses in (
            ({"../outside": b"x"}, {"x": "sha256:" + "a" * 64}, {"missing": "a" * 64}),
            ({"file": b"x"}, {"x": "latest"}, {"file": sha(b"x")}),
            ({"file": b"x"}, {"x": "sha256:" + "a" * 64}, {"file": "b" * 64}),
        ):
            with self.assertRaises(ValueError):
                build(
                    VERSION,
                    "a" * 40,
                    PINS,
                    files,
                    dependencies=deps,
                    toolchains={"x": "sha256:" + "a" * 64},
                    licenses=licenses,
                )


@unittest.skipUnless(
    actual_target() == "linux/arm64",
    "separate disposable Linux arm64 installation lane required",
)
class InstalledTests(unittest.TestCase):
    def setUp(self):
        self.target = DisposableTarget.create()
        self.addCleanup(self.target.cleanup)

    def test_real_installed_bytes_and_empty_history_concurrent_generation(self):
        with self.assertRaises(ValueError):
            self.target.rollback(VERSION, component_commits=PINS, expected_generation=0)
        installed = self.target.install(
            VERSION, bundle(), component_commits=PINS, expected_generation=0
        )
        self.assertEqual(
            installed["manifest_sha256"],
            json.loads((self.target.root / "active.json").read_bytes())[
                "manifest_sha256"
            ],
        )
        folder = self.target.root / "versions" / installed["manifest_sha256"]
        self.assertEqual((folder / "component/example.txt").read_bytes(), b"first")
        with self.assertRaises(ValueError):
            self.target.install(
                VERSION, bundle(b"other"), component_commits=PINS, expected_generation=0
            )
        with self.assertRaises(ValueError):
            self.target.rollback(VERSION, component_commits=PINS, expected_generation=1)
        (folder / "component/example.txt").write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "installed byte mismatch"):
            self.target.verify_installed(PINS)

    def test_fresh_process_interrupted_upgrade_pointer_and_rollback(self):
        self.target.install(
            VERSION, bundle(), component_commits=PINS, expected_generation=0
        )
        for boundary in (
            "after_install_intent",
            "after_first_installed_file",
            "after_installed_pointer",
        ):
            prior = self.target.state()["active"]
            generation = self.target.state()["generation"]
            content = bundle(boundary.encode())
            (self.target.root / "next.bundle").write_bytes(content)
            code = (
                "import sys,os; sys.path.insert(0,'tests'); from test_development_bundle import PINS; from tools.development_bundle import DisposableTarget,VERSION; t=DisposableTarget(sys.argv[1]); t.install(VERSION,(t.root/'next.bundle').read_bytes(),component_commits=PINS,expected_generation="
                + str(generation)
                + ",checkpoint=lambda p:os._exit(101) if p=='"
                + boundary
                + "' else None)"
            )
            result = subprocess.run(
                [sys.executable, "-c", code, str(self.target.root)],
                timeout=20,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 101, result.stderr)
            self.assertIsNotNone(self.target.state()["pending"])
            # Read actual pointer independently before coordinator recovery.
            pointer = json.loads((self.target.root / "active.json").read_bytes())[
                "manifest_sha256"
            ]
            expected = verify(content, component_commits=PINS)[2]
            self.assertEqual(
                pointer, expected if boundary == "after_installed_pointer" else prior
            )
            code = "import sys,json; sys.path.insert(0,'tests'); from test_development_bundle import PINS; from tools.development_bundle import DisposableTarget,VERSION; t=DisposableTarget(sys.argv[1]); print(json.dumps(t.recover(VERSION,component_commits=PINS))); t.close()"
            recovery = subprocess.run(
                [sys.executable, "-c", code, str(self.target.root)],
                timeout=20,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(json.loads(recovery.stdout)["manifest_sha256"], expected)
        current = self.target.state()["active"]
        generation = self.target.state()["generation"]
        rolled = self.target.rollback(
            VERSION, component_commits=PINS, expected_generation=generation
        )
        self.assertNotEqual(rolled["manifest_sha256"], current)
        with self.assertRaises(ValueError):
            self.target.rollback(
                VERSION, component_commits=PINS, expected_generation=generation
            )
        self.assertEqual(self.target.verify_installed(PINS), rolled)

    def test_unavailable_rollback_bytes_preserves_installed_pointer(self):
        first = self.target.install(
            VERSION, bundle(), component_commits=PINS, expected_generation=0
        )
        second = self.target.install(
            VERSION, bundle(b"second"), component_commits=PINS, expected_generation=1
        )
        old_file = (
            self.target.root
            / "versions"
            / first["manifest_sha256"]
            / "component/example.txt"
        )
        old_file.unlink()
        with self.assertRaisesRegex(ValueError, "rollback bytes unavailable"):
            self.target.rollback(VERSION, component_commits=PINS, expected_generation=2)
        self.assertEqual(self.target.verify_installed(PINS), second)
        self.assertIsNone(self.target.state()["pending"])


if __name__ == "__main__":
    unittest.main()
