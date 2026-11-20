import base64
import hashlib
import shutil
import unittest
from unittest.mock import patch

from tools.runner_fleet.noble_resolver_evidence import verify


# Static synthetic public key and signature; no real archive data or key.
KEYRING = base64.b64decode(
    "xv8AAAENBGjneAABCADToJXdysPhw8Ld5AHZAA1neU1wOU93GUuld/ENQvl1ZLoNthFgAxXJ"
    "8zevoB+icFJIJip/vYuu3HJBZahlHBAqaDTmsos2Y8HedV2pa9L06YAofAeYAoyWxT6I3rfc"
    "XOXfPBCAow0bzDD8XtKYThFAanXP5KrF4LLu395WIw4JV5BHVe1K25UXlCJ1LTqF0EHQ1OZv"
    "P+yHArMQwyHF3d2kvkzOJSYlu6Kck3e4oAgJvM/urE/N/LNk0+fHDJ2MSVrB19b9/De+4N3x"
    "aZkofElwsfK/o6EoOKJs13kOwOKl5bqN4JLPd/ued8wXBc53K2Vs1A5dudnJVMMviLdC7CY3"
    "ABEBAAHN/wAAADRTeW50aGV0aWMgcmVzb2x2ZXIgZml4dHVyZSA8Zml4dHVyZUBleGFtcGxl"
    "LmludmFsaWQ+wv8AAAEzBBMBCAAdBQJo53gAFiEEt+i55S0O9w8IyJrkImClNRMMQtMACgkQ"
    "ImClNRMMQtP+eQgAi3MEB/V1p2vNI2EL2GlbJl2i7CZDBRnSyeortMbffJ6dhXHMGT9jFFyz"
    "upC8lMeySfh+Id1XszDs/RELHul9xEXgaxicCpia95AUo8w/I2Z0hphQT1QvuDzKG028cq6H"
    "SFr1uG5nkMXL2C3q4ukoZNJEbVRDFeQIVeKsm4zG84gzbyffbIGEY89b63RW2RKnh3ZK9WYw"
    "d/KXNEv1/hKwGv5KnD7HHyZdrH3rM6bvsyRxYyEU9Qr4WPyNGaS4pIWhV6q7wDK5ssj7dKWn"
    "wAuO8YYfe7DiTuAfUAcqB+y1HYeu+vRFnems1QV5WX0M/iBUKihS4Cl3boAQYmom61PJ9Q=="
)
INRELEASE = base64.b64decode(
    "LS0tLS1CRUdJTiBQR1AgU0lHTkVEIE1FU1NBR0UtLS0tLQpIYXNoOiBTSEEyNTYKCkNvZGVu"
    "YW1lOiBub2JsZQotLS0tLUJFR0lOIFBHUCBTSUdOQVRVUkUtLS0tLQoKd3Y4QUFBRXpCQUVC"
    "Q0FBZEJRSm81M2dBRmlFRXQraTU1UzBPOXc4SXlKcmtJbUNsTlJNTVF0TUFDZ2tRSW1DbApO"
    "Uk1NUXRPKzR3Z0F2NDBUT01yMGxxM0dNQjNkcHNseDQrUUxNVUpETnZsSzQ4bDJaRy9zcjN6"
    "NFIvcTNoaHNUCmVpU2FRRlY4WlVML1JYcUcydHNMeXdHNURCeWhGYmREa1cySUp0Ym9WNjMw"
    "S3Vob1QxR0JQWjBCaE1rb20wSEMKNUM1RW52RWJ3Q00wZGpTbEdDdzY2TUFSMEM5cDJ5S29l"
    "cVlQV1F0L3lOcmR0SkxScXh0QlV3THZhZ2YwaUE1KwpPZ2FLTzNzaVR0QzNxMFYrV3hMRkRy"
    "ZW4wSGRIbjJVWnlFdHJUWjlxR0l5WWpsZU9TOUNaa0VnUWlxdUNJZ3pUCmxsYVFUTjRsTTdo"
    "dXFodkZMcGs0eTEyTC92Zjlpb3U2UTY1Q2RBZVlwUWZiRldNV3l1ajJxcjdONGJBRWw3Ym8K"
    "dEkrNTJVM0tlS0xNL2V2TXBjTXdGbWJvZGkyMmkwd1ZOQT09Cj1wZTZjCi0tLS0tRU5EIFBH"
    "UCBTSUdOQVRVUkUtLS0tLQo="
)
SIGNING_FINGERPRINT = "B7E8B9E52D0EF70F08C89AE42260A535130C42D3"


class ResolverEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.inputs = {
            "reviewed_image": "sha256:" + "1" * 64,
            "observed_image": "sha256:" + "1" * 64,
            "reviewed_keyring_sha256": hashlib.sha256(b"ring").hexdigest(),
            "reviewed_signing_fingerprint": "A" * 40,
            "keyring": b"ring",
            "inrelease": b"-----BEGIN PGP SIGNED MESSAGE-----\n",
            "resolver_log": b"fixture retained",
            "resolver_exit": 0,
        }

    def test_rejects_before_verifier(self):
        cases = [
            {"observed_image": "ubuntu:noble"},
            {"keyring": b"changed"},
            {"reviewed_signing_fingerprint": "A" * 16},
            {"resolver_log": b"apt-clean: Permission denied"},
            {"resolver_log": b"Read-only file system"},
            {"resolver_log": b""},
            {"resolver_exit": 1},
            {"resolver_exit": False},
            {"inrelease": b"not clearsigned"},
        ]
        with patch("tools.runner_fleet.noble_resolver_evidence.subprocess.run") as gpg:
            for change in cases:
                with self.subTest(change=change), self.assertRaises(ValueError):
                    verify(**(self.inputs | change))
            gpg.assert_not_called()

    @unittest.skipUnless(shutil.which("gpg"), "GnuPG unavailable")
    def test_real_synthetic_signature_and_negative_cases(self):
        valid = self.inputs | {
            "keyring": KEYRING,
            "reviewed_keyring_sha256": hashlib.sha256(KEYRING).hexdigest(),
            "reviewed_signing_fingerprint": SIGNING_FINGERPRINT,
            "inrelease": INRELEASE,
        }
        receipt = verify(**valid)
        self.assertFalse(receipt["package_closure_verified"])
        self.assertFalse(receipt["activation_allowed"])
        for change in (
            {"reviewed_signing_fingerprint": "0" * 40},
            {"inrelease": INRELEASE.replace(b"noble", b"other")},
            {"inrelease": INRELEASE + b"unsigned trailing data\n"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify(**(valid | change))
