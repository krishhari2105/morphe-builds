import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from morphe_builder.acquisition import BaseCache, download_apkmirror_latest_candidates
from morphe_builder.config import load_config
from morphe_builder.manifest import sha256_file
from morphe_builder.models import DownloadResult


class AcquisitionTests(unittest.TestCase):
    def test_latest_candidate_downloads_are_lazy(self):
        app = load_config().apps["yt-music"]

        class Resolver:
            def __init__(self):
                self.resolved = 0
                self.downloaded = 0

            def iter_latest_candidates(self, app):
                for index in range(3):
                    self.resolved += 1
                    yield (
                        f"https://download.apkmirror.com/{index}.apk",
                        f"https://www.apkmirror.com/variant/{index}",
                        "9.26.51",
                    )

            def download_candidate(self, url, referer, destination):
                self.downloaded += 1
                destination.write_bytes(b"apk")
                return DownloadResult(destination, 3, "digest", url)

        resolver = Resolver()
        with tempfile.TemporaryDirectory() as temp:
            downloads = download_apkmirror_latest_candidates(resolver, app, Path(temp))
            first = next(downloads)
            self.assertEqual(first[2], "9.26.51")
            self.assertEqual(resolver.resolved, 1)
            self.assertEqual(resolver.downloaded, 1)

    def test_latest_cache_uses_version_code_not_manifest_filename(self):
        app = load_config().apps["yt-music"]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            older_file = root / "yt-music-z-old.apk"
            newer_file = root / "yt-music-a-new.apk"
            older_file.write_bytes(b"older")
            newer_file.write_bytes(b"newer")
            for name, file, version_name, version_code in (
                ("yt-music-z-old.json", older_file, "9.15.51", "100"),
                ("yt-music-a-new.json", newer_file, "10.0.0", "200"),
            ):
                (root / name).write_text(
                    json.dumps(
                        {
                            "app": app.key,
                            "package": app.package,
                            "version_name": version_name,
                            "version_code": version_code,
                            "sha256": sha256_file(file),
                            "file": file.name,
                            "source_page": None,
                        }
                    ),
                    encoding="utf-8",
                )
            latest = BaseCache(root).find_latest(app)
            self.assertIsNotNone(latest)
            self.assertEqual(latest.version_name, "10.0.0")

    def test_exact_cache_lookup_does_not_match_another_version(self):
        app = load_config().apps["youtube"]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "youtube.apk"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("AndroidManifest.xml", b"manifest")
            cache = BaseCache(root)
            cache.store(
                app,
                source,
                version_name="20.12.46",
                version_code="123",
                source_page="https://example.invalid/release",
            )
            self.assertIsNone(cache.find(app, "20.12.45"))
            self.assertIsNotNone(cache.find(app, "20.12.46"))


if __name__ == "__main__":
    unittest.main()
