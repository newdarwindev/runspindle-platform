"""Closed compatibility is neither authority nor a passing job receipt."""

import dataclasses as dc
import unittest

from tools.runner_fleet.environment_applicability import (
    Blob,
    Observation,
    Profile,
    assigned_compatible,
    compatible,
)


class ApplicabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.setup = (
            Blob(".github/workflows/validation.yml", "100644", 5, "a" * 64),
            Blob("tools/setup.py", "100644", 6, "b" * 64),
            Blob("Cargo.lock", "100644", 7, "c" * 64),
        )
        self.app = Blob("src/main.rs", "100644", 8, "d" * 64)
        self.profile = Profile(
            "owner/repo",
            "e" * 64,
            "f" * 64,
            self.setup,
            (self.app.path,),
            ("build.rs",),
        )
        self.observed = Observation(
            "owner/repo", "1" * 40, "e" * 64, "f" * 64, self.setup + (self.app,)
        )

    def test_application_change_is_compatibility_only(self) -> None:
        changed = dc.replace(
            self.observed,
            source_sha="2" * 40,
            blobs=self.setup + (dc.replace(self.app, sha256="0" * 64),),
        )
        self.assertEqual(
            compatible(self.profile, changed), compatible(self.profile, self.observed)
        )
        self.assertEqual(
            assigned_compatible(self.profile, changed, changed),
            self.profile.fingerprint(),
        )
        self.assertFalse(hasattr(changed, "passed"))

    def test_every_setup_change_rejected(self) -> None:
        for index in range(len(self.setup)):
            original = self.observed.blobs[index]
            for field, changed in [
                ("sha256", dc.replace(original, sha256="0" * 64)),
                ("mode", dc.replace(original, mode="100755")),
                ("size", dc.replace(original, size=9)),
            ]:
                blobs = list(self.observed.blobs)
                blobs[index] = changed
                with (
                    self.subTest(index=index, field=field),
                    self.assertRaises(ValueError),
                ):
                    compatible(
                        self.profile, dc.replace(self.observed, blobs=tuple(blobs))
                    )

    def test_new_optional_nested_or_unknown_file_rejected(self) -> None:
        for path in [
            "build.rs",
            "workspace/new/Cargo.lock",
            ".github/actions/new/action.yml",
            "src/other.rs",
            ".cargo/config.toml",
        ]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                compatible(
                    self.profile,
                    dc.replace(
                        self.observed,
                        blobs=self.observed.blobs
                        + (Blob(path, "100644", 1, "0" * 64),),
                    ),
                )

    def test_missing_duplicate_symlink_submodule_and_executable_app_rejected(
        self,
    ) -> None:
        variants = [self.observed.blobs[1:], self.observed.blobs + (self.app,)]
        variants += [
            self.setup + (dc.replace(self.app, mode=mode),)
            for mode in ("120000", "160000", "100755")
        ]
        for blobs in variants:
            with self.subTest(blobs=blobs), self.assertRaises(ValueError):
                compatible(self.profile, dc.replace(self.observed, blobs=blobs))

    def test_environment_external_closure_and_revision_fail_closed(self) -> None:
        for changed in [
            dc.replace(self.observed, environment_sha256="0" * 64),
            dc.replace(self.observed, external_closure_sha256="0" * 64),
            dc.replace(self.observed, source_sha="main"),
            dc.replace(self.observed, repository="../repo"),
        ]:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                compatible(self.profile, changed)

    def test_actual_assignment_independently_checked(self) -> None:
        for assigned in [
            dc.replace(self.observed, source_sha="2" * 40),
            dc.replace(self.observed, repository="owner/other"),
            dc.replace(self.observed, external_closure_sha256="0" * 64),
            dc.replace(
                self.observed,
                blobs=self.setup + (dc.replace(self.app, sha256="0" * 64),),
            ),
        ]:
            with self.subTest(assigned=assigned), self.assertRaises(ValueError):
                assigned_compatible(self.profile, self.observed, assigned)

    def test_invalid_profile_paths_overlap_and_bounds(self) -> None:
        profiles = [
            dc.replace(self.profile, setup=()),
            dc.replace(self.profile, application_paths=("Cargo.lock",)),
            dc.replace(self.profile, absent_paths=(self.app.path,)),
            dc.replace(self.profile, absent_paths=("src",)),
            dc.replace(self.profile, application_paths=("a/../b",)),
            dc.replace(self.profile, application_paths=(self.app.path,) * 2),
        ]
        for profile in profiles:
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                profile.fingerprint()
        for size in [-1, True, 16 * 1024 * 1024 + 1]:
            with self.subTest(size=size), self.assertRaises(ValueError):
                compatible(
                    self.profile,
                    dc.replace(
                        self.observed,
                        blobs=self.setup + (dc.replace(self.app, size=size),),
                    ),
                )

    def test_profile_fingerprint_binds_every_declared_input(self) -> None:
        fingerprint = self.profile.fingerprint()
        for profile in [
            dc.replace(self.profile, environment_sha256="0" * 64),
            dc.replace(self.profile, external_closure_sha256="0" * 64),
            dc.replace(self.profile, absent_paths=("other.cfg",)),
            dc.replace(self.profile, application_paths=("src/other.rs",)),
            dc.replace(
                self.profile,
                setup=(dc.replace(self.setup[0], sha256="0" * 64),) + self.setup[1:],
            ),
        ]:
            self.assertNotEqual(fingerprint, profile.fingerprint())


if __name__ == "__main__":
    unittest.main()
