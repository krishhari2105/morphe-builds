import tempfile
import unittest
import zipfile
from pathlib import Path

from morphe_builder.bundles import BundleError, inspect_bundle
from morphe_builder.models import ApkInfo


class FakeAndroid:
    def __init__(self, mapping):
        self.mapping = mapping

    def inspect_apk(self, path):
        for marker, metadata in self.mapping.items():
            if marker in path.name:
                return ApkInfo(path=path, **metadata)
        raise AssertionError(f"Unexpected APK: {path.name}")


def apk(package="com.google.android.youtube", version="20.12.46", code="1", split=None, native=()):
    return {
        "package": package,
        "version_name": version,
        "version_code": code,
        "split": split,
        "native_code": native,
    }


class BundleTests(unittest.TestCase):
    def make_bundle(self, root, names):
        path = root / "source.apkm"
        with zipfile.ZipFile(path, "w") as archive:
            for name in names:
                archive.writestr(name, b"dummy-apk")
        return path

    def test_multi_arch_bundle_is_not_modified_and_requests_post_patch_strip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.make_bundle(root, ["base.apk", "split_config.arm64_v8a.apk", "split_config.x86.apk"])
            before = bundle.read_bytes()
            android = FakeAndroid(
                {
                    "base.apk": apk(),
                    "arm64": apk(split="config.arm64_v8a", native=("arm64-v8a",)),
                    "x86.apk": apk(split="config.x86", native=("x86",)),
                }
            )
            info = inspect_bundle(bundle, android, expected_package="com.google.android.youtube", expected_version="20.12.46")
            self.assertTrue(info.needs_arm64_strip)
            self.assertEqual(set(info.native_abis), {"arm64-v8a", "x86"})
            self.assertEqual(bundle.read_bytes(), before)

    def test_arm64_only_bundle_does_not_request_strip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.make_bundle(root, ["base.apk", "split_config.arm64_v8a.apk"])
            android = FakeAndroid(
                {
                    "base.apk": apk(),
                    "arm64": apk(split="config.arm64_v8a", native=("arm64-v8a",)),
                }
            )
            info = inspect_bundle(bundle, android, expected_package="com.google.android.youtube")
            self.assertFalse(info.needs_arm64_strip)

    def test_rejects_zip_slip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.make_bundle(root, ["../base.apk"])
            with self.assertRaises(BundleError):
                inspect_bundle(bundle, FakeAndroid({}), expected_package="com.google.android.youtube")

    def test_rejects_bundle_without_arm64_support(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.make_bundle(root, ["base.apk", "split_config.x86.apk"])
            android = FakeAndroid(
                {
                    "base.apk": apk(),
                    "x86": apk(split="config.x86", native=("x86",)),
                }
            )
            with self.assertRaisesRegex(BundleError, "no arm64-v8a"):
                inspect_bundle(bundle, android, expected_package="com.google.android.youtube")


if __name__ == "__main__":
    unittest.main()
