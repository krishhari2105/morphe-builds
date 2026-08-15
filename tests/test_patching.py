import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from morphe_builder.config import load_config
from morphe_builder.models import ApkInfo, ReleaseAsset, ReleaseInfo
from morphe_builder.patching import PatchingError, ResolvedTools, list_versions, patch_unsigned


class FakeAndroid:
    def validate_apk(self, path, **kwargs):
        self.assert_expected = kwargs.get("expected_packages")
        if "app.morphe.android.youtube" not in self.assert_expected:
            raise RuntimeError("unexpected output package")
        return ApkInfo(
            path=path,
            package="app.morphe.android.youtube",
            version_name="20.12.46",
            version_code="1",
            native_code=("arm64-v8a",),
        )

    def check_alignment(self, path):
        pass

    def verify_signature(self, path, expected):
        return "abc123"


def release(repo, tag):
    return ReleaseInfo(1, repo, tag, tag, False, False, None, ())


class PatchingTests(unittest.TestCase):
    def tools(self, root):
        cli = root / "cli.jar"
        patches_file = root / "patches.mpp"
        cli.write_bytes(b"cli")
        patches_file.write_bytes(b"patches")
        return ResolvedTools(
            patches_release=release("MorpheApp/morphe-patches", "v1"),
            patches_asset=ReleaseAsset(1, "patches.mpp", "api", "browser", 7),
            patches_path=patches_file,
            patches_sha256="patch-sha",
            cli_release=release("MorpheApp/morphe-desktop", "v1"),
            cli_asset=ReleaseAsset(2, "cli.jar", "api", "browser", 3),
            cli_path=cli,
            cli_sha256="cli-sha",
        )

    def run_patch(self, strip):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.apkm"
            source.write_bytes(b"original-source")
            before = source.read_bytes()
            output = root / "output.apk"
            result_file = root / "result.json"

            def fake_run(command, **kwargs):
                output.write_bytes(b"patched")
                return SimpleNamespace(returncode=0)

            with patch("morphe_builder.patching.subprocess.run", side_effect=fake_run) as run:
                patch_unsigned(
                    load_config(),
                    load_config().sources["morphe"],
                    "youtube",
                    source,
                    output,
                    result_file,
                    self.tools(root),
                    FakeAndroid(),
                    version_name="20.12.46",
                    allowed_output_packages={"com.google.android.youtube", "app.morphe.android.youtube"},
                    strip_to_arm64=strip,
                )
            return run.call_args.args[0], before, source.read_bytes(), run.call_args.kwargs["env"]

    def test_multi_arch_uses_morphe_post_patch_strip_without_touching_source(self):
        command, before, after, _ = self.run_patch(True)
        self.assertIn("--striplibs=arm64-v8a", command)
        self.assertTrue(command[-1].endswith("source.apkm"))
        self.assertEqual(before, after)

    def test_arm64_only_omits_striplibs(self):
        command, _, _, _ = self.run_patch(False)
        self.assertNotIn("--striplibs=arm64-v8a", command)
        self.assertIn("--unsigned", command)

    def test_morphe_process_does_not_receive_tokens_or_signing_secrets(self):
        with patch.dict(
            os.environ,
            {
                "GITHUB_TOKEN": "write-token",
                "SIGNING_KEYSTORE_B64": "secret-key",
                "SIGNING_KEY_PASSWORD": "secret-password",
            },
            clear=False,
        ):
            _, _, _, child_env = self.run_patch(False)
        self.assertNotIn("GITHUB_TOKEN", child_env)
        self.assertNotIn("SIGNING_KEYSTORE_B64", child_env)
        self.assertNotIn("SIGNING_KEY_PASSWORD", child_env)

    def test_list_versions_combines_java_stderr_logging(self):
        with tempfile.TemporaryDirectory() as temp:
            tools = self.tools(Path(temp))
            process = SimpleNamespace(
                returncode=0,
                stdout="",
                stderr="INFO: Package name: com.google.android.youtube\n20.12.46\n",
            )
            with (
                patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}, clear=False),
                patch("morphe_builder.patching.subprocess.run", return_value=process) as run,
            ):
                parsed = list_versions(tools)
            self.assertEqual(parsed["com.google.android.youtube"], ["20.12.46"])
            self.assertNotIn("GITHUB_TOKEN", run.call_args.kwargs["env"])

    def test_detects_source_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.apk"
            source.write_bytes(b"original")
            output = root / "output.apk"

            def mutate_source(command, **kwargs):
                source.write_bytes(b"changed")
                output.write_bytes(b"patched")
                return SimpleNamespace(returncode=0)

            with (
                patch("morphe_builder.patching.subprocess.run", side_effect=mutate_source),
                self.assertRaisesRegex(PatchingError, "modified the source"),
            ):
                patch_unsigned(
                    load_config(),
                    load_config().sources["morphe"],
                    "youtube",
                    source,
                    output,
                    root / "result.json",
                    self.tools(root),
                    FakeAndroid(),
                    version_name="20.12.46",
                    allowed_output_packages={"com.google.android.youtube", "app.morphe.android.youtube"},
                    strip_to_arm64=False,
                )


if __name__ == "__main__":
    unittest.main()
