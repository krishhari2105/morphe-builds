import tempfile
import unittest
import zipfile
from pathlib import Path

from morphe_builder import bundles

CERT = "a" * 64
OTHER_CERT = "b" * 64


class FakeSigner:
    def verify_signature(self, path, expected_fingerprint=None):
        payload = path.read_bytes().decode()
        if payload == "unsigned":
            raise RuntimeError("Signature verification failed")
        return OTHER_CERT if payload == "wrong-publisher" else CERT


class SourceVerificationTests(unittest.TestCase):
    def verify(self, path, pins=(CERT,)):
        self.assertTrue(hasattr(bundles, "verify_source_signatures"), "Input signature verification missing")
        return bundles.verify_source_signatures(path, FakeSigner(), pins)

    def test_accepts_trusted_signed_apk(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "base.apk"
            path.write_text("signed", encoding="utf-8")
            self.assertEqual(self.verify(path), CERT)

    def test_rejects_valid_signature_from_wrong_publisher(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "base.apk"
            path.write_text("wrong-publisher", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "publisher|certificate"):
                self.verify(path)

    def test_requires_independently_trusted_pins(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "base.apk"
            path.write_text("signed", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "trusted|fingerprint"):
                self.verify(path, ())

    def test_accepts_official_signing_key_rotation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "base.apk"
            path.write_text("wrong-publisher", encoding="utf-8")
            self.assertEqual(self.verify(path, (CERT, OTHER_CERT)), OTHER_CERT)

    def test_rejects_unsigned_split_even_with_trusted_base(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.apkm"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("base.apk", "signed")
                archive.writestr("split_config.arm64_v8a.apk", "unsigned")
            with self.assertRaisesRegex(RuntimeError, "Signature verification"):
                self.verify(path)

    def test_rejects_mixed_signers_in_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.apkm"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("base.apk", "signed")
                archive.writestr("split_config.arm64_v8a.apk", "wrong-publisher")
            with self.assertRaisesRegex(RuntimeError, "signer|certificate"):
                self.verify(path, (CERT, OTHER_CERT))

    def test_accepts_matching_signers_in_bundle(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.apkm"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("base.apk", "signed")
                archive.writestr("split_config.arm64_v8a.apk", "signed")
            self.assertEqual(self.verify(path), CERT)


if __name__ == "__main__":
    unittest.main()
