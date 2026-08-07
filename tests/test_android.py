import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from morphe_builder.android import AndroidTools


class AndroidTests(unittest.TestCase):
    def test_align_and_sign_keeps_passwords_out_of_command_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            unsigned = root / "unsigned.apk"
            unsigned.write_bytes(b"unsigned")
            output = root / "signed.apk"
            tools = AndroidTools.__new__(AndroidTools)
            tools.zipalign = Path("zipalign")
            tools.apksigner = Path("apksigner")
            calls = []

            def fake_run(command, **kwargs):
                calls.append((command, kwargs))
                if str(command[0]).endswith("zipalign"):
                    Path(command[-1]).write_bytes(b"aligned")
                else:
                    Path(command[command.index("--out") + 1]).write_bytes(b"signed")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("morphe_builder.android.subprocess.run", side_effect=fake_run):
                tools.align_and_sign(
                    unsigned,
                    output,
                    keystore_path=root / "key.bks",
                    alias="Morphe",
                    keystore_type="BKS",
                    keystore_password="store-secret",
                    key_password="key-secret",
                )
            sign_command, sign_kwargs = calls[1]
            self.assertNotIn("store-secret", sign_command)
            self.assertNotIn("key-secret", sign_command)
            self.assertEqual(sign_command[sign_command.index("--ks-type") + 1], "BKS")
            self.assertEqual(sign_kwargs["env"]["MORPHE_APKSIGNER_STORE_PASS"], "store-secret")
            self.assertEqual(sign_kwargs["env"]["MORPHE_APKSIGNER_KEY_PASS"], "key-secret")
            self.assertTrue(output.is_file())

    def test_inspect_apk_parses_attributes_in_any_order(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "split.apk"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("AndroidManifest.xml", b"binary")
            tools = AndroidTools.__new__(AndroidTools)
            tools.aapt = Path("aapt")
            output = (
                "package: name='com.example' versionCode='42' compileSdkVersion='35' "
                "versionName='1.2.3' split='config.arm64_v8a'\n"
                "native-code: 'arm64-v8a'\n"
            )
            with patch("morphe_builder.android.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout=output, stderr=""
            )):
                info = tools.inspect_apk(path)
            self.assertEqual(info.package, "com.example")
            self.assertEqual(info.version_code, "42")
            self.assertEqual(info.version_name, "1.2.3")
            self.assertEqual(info.split, "config.arm64_v8a")
            self.assertEqual(info.native_code, ("arm64-v8a",))


if __name__ == "__main__":
    unittest.main()
