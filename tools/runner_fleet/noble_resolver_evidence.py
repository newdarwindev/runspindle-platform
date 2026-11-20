"""Offline verification of captured Noble resolver provenance, not package closure."""

import hashlib
import re
import subprocess
import tempfile
from pathlib import Path

_DENIAL = re.compile(
    rb"permission denied|access denied|operation not permitted|read-only file system",
    re.IGNORECASE,
)
_BAD_SIGNATURE = {
    "BADSIG", "ERRSIG", "EXPKEYSIG", "EXPSIG", "KEYEXPIRED", "KEYREVOKED",
    "NODATA", "NO_PUBKEY", "REVKEYSIG", "SIGEXPIRED",
}


def verify(
    *,
    reviewed_image: str,
    observed_image: str,
    reviewed_keyring_sha256: str,
    reviewed_signing_fingerprint: str,
    keyring: bytes,
    inrelease: bytes,
    resolver_log: bytes,
    resolver_exit: int,
) -> dict[str, str | bool]:
    """The reviewed pins and completeness of the capture are caller trust inputs.

    Fingerprint is the exact signing key or subkey fingerprint, not a short ID
    or a primary-key wildcard. No package metadata or install is qualified here.
    """
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", reviewed_image):
        raise ValueError("missing reviewed resolver image digest")
    if observed_image != reviewed_image:
        raise ValueError("resolver image digest mismatch")
    if not re.fullmatch(r"[0-9a-f]{64}", reviewed_keyring_sha256):
        raise ValueError("missing independent keyring hash")
    if hashlib.sha256(keyring).hexdigest() != reviewed_keyring_sha256:
        raise ValueError("keyring hash mismatch")
    if not re.fullmatch(r"(?:[0-9A-F]{40}|[0-9A-F]{64})", reviewed_signing_fingerprint):
        raise ValueError("missing full reviewed signature fingerprint")
    if type(resolver_exit) is not int or resolver_exit != 0 or not resolver_log.strip():
        raise ValueError("missing or unsuccessful resolver capture")
    if _DENIAL.search(resolver_log):
        raise ValueError("resolver reported access denial despite exit status")
    lines = inrelease.rstrip().splitlines()
    begin = b"-----BEGIN PGP SIGNED MESSAGE-----"
    signature = b"-----BEGIN PGP SIGNATURE-----"
    end = b"-----END PGP SIGNATURE-----"
    if (
        not lines or lines[0] != begin or lines[-1] != end
        or any(lines.count(marker) != 1 for marker in (begin, signature, end))
    ):
        raise ValueError("expected one clearsigned InRelease without trailing data")

    with tempfile.TemporaryDirectory(prefix="noble-verify-") as work:
        root = Path(work)
        ring = root / "reviewed.gpg"
        document = root / "InRelease"
        ring.write_bytes(keyring)
        document.write_bytes(inrelease)
        try:
            result = subprocess.run(
                [
                    "gpg", "--no-options", "--homedir", work, "--batch",
                    "--no-default-keyring", "--keyring", str(ring),
                    "--disable-dirmngr", "--no-auto-key-retrieve",
                    "--status-fd", "1", "--verify", str(document),
                ],
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ValueError("offline signature verifier unavailable") from error
    events = [
        line[len(b"[GNUPG:] "):].split()
        for line in result.stdout.splitlines()
        if line.startswith(b"[GNUPG:] ")
    ]
    if result.returncode != 0 or any(
        event and event[0].decode("ascii", "replace") in _BAD_SIGNATURE
        for event in events
    ):
        raise ValueError("InRelease signature verification failed")
    valid = [event[1:2] for event in events if event and event[0] == b"VALIDSIG"]
    if valid != [[reviewed_signing_fingerprint.encode("ascii")]]:
        raise ValueError("InRelease did not have exactly one reviewed signer")
    return {
        "resolver_image": reviewed_image,
        "keyring_sha256": reviewed_keyring_sha256,
        "signing_fingerprint": reviewed_signing_fingerprint,
        "inrelease_sha256": hashlib.sha256(inrelease).hexdigest(),
        "resolver_log_sha256": hashlib.sha256(resolver_log).hexdigest(),
        "package_closure_verified": False,
        "activation_allowed": False,
    }
