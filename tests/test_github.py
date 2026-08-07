import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from morphe_builder.cli import _release_is_complete
from morphe_builder.github import GitHubClient, GitHubError
from morphe_builder.models import ReleaseAsset, ReleaseInfo


class GitHubTests(unittest.TestCase):
    def test_exact_asset_selection(self):
        release = ReleaseInfo(
            id=1,
            repo="owner/repo",
            tag="v1",
            name="v1",
            prerelease=False,
            draft=False,
            published_at=None,
            assets=(
                ReleaseAsset(1, "patches-1.mpp", "api", "browser", 10),
                ReleaseAsset(2, "source.zip", "api", "browser", 10),
            ),
        )
        selected = GitHubClient.select_asset(release, r"^patches-[^/]+\.mpp$")
        self.assertEqual(selected.name, "patches-1.mpp")

    def test_ambiguous_asset_selection_fails(self):
        release = ReleaseInfo(
            id=1,
            repo="owner/repo",
            tag="v1",
            name="v1",
            prerelease=False,
            draft=False,
            published_at=None,
            assets=(
                ReleaseAsset(1, "patches-1.mpp", "api", "browser", 10),
                ReleaseAsset(2, "patches-2.mpp", "api", "browser", 10),
            ),
        )
        with self.assertRaises(GitHubError):
            GitHubClient.select_asset(release, r"^patches-[^/]+\.mpp$")

    def test_watcher_requires_completion_assets(self):
        incomplete = ReleaseInfo(1, "o/r", "tag", "tag", False, False, None, (
            ReleaseAsset(1, "app.apk", "api", "browser", 1),
        ))
        complete = ReleaseInfo(1, "o/r", "tag", "tag", False, False, None, (
            ReleaseAsset(1, "app.apk", "api", "browser", 1),
            ReleaseAsset(2, "build-manifest.json", "api", "browser", 1),
            ReleaseAsset(3, "SHA256SUMS", "api", "browser", 1),
        ))
        self.assertFalse(_release_is_complete(incomplete))
        self.assertTrue(_release_is_complete(complete))

    def test_publish_resumes_an_incomplete_release(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            files = []
            for name in ("app.apk", "build-manifest.json", "SHA256SUMS"):
                path = root / name
                path.write_bytes(name.encode())
                files.append(path)
            incomplete = ReleaseInfo(1, "o/r", "tag", "tag", False, False, None, (
                ReleaseAsset(1, "build-manifest.json", "api", "browser", 1, "sha256:unused"),
            ))
            complete = ReleaseInfo(1, "o/r", "tag", "tag", False, False, None, tuple(
                ReleaseAsset(index, path.name, "api", "browser", path.stat().st_size, "sha256:unused")
                for index, path in enumerate(files, 1)
            ))
            client = GitHubClient.__new__(GitHubClient)
            client.repository = "o/r"
            with patch.object(client, "get_release_by_tag", side_effect=[incomplete, complete]), patch.object(
                client, "_asset_matches", return_value=True
            ), patch.object(client, "upload_asset") as upload:
                published = client.publish_release("tag", "name", "body", files)
            self.assertEqual(published, complete)
            self.assertEqual({call.args[1].name for call in upload.call_args_list}, {"app.apk", "SHA256SUMS"})


if __name__ == "__main__":
    unittest.main()
