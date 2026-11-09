import os
import unittest
from unittest.mock import patch

import scripts.generate_catalog as generate_catalog


class GenerateCatalogTests(unittest.TestCase):
    def test_prefers_api_asset_url_over_browser_download_url(self):
        fake_releases = [
            {
                "tag_name": "build-morphe-v2.1.0-v2.1.1-apps-df3cf73a-ae53651632bf",
                "name": "Morphe stable: v2.1.0",
                "draft": False,
                "published_at": "2026-11-07T09:04:33Z",
                "html_url": "https://github.com/krishhari2105/morphe-builds/releases/tag/v2.1.0",
                "assets": [
                    {
                        "name": "build-manifest.json",
                        "url": "https://api.github.com/repos/krishhari2105/morphe-builds/releases/assets/720178419",
                        "browser_download_url": "https://github.com/krishhari2105/morphe-builds/releases/download/v2.1.0/build-manifest.json",
                    },
                    {
                        "name": "youtube-morphe-v21.30.216-arm64.apk",
                        "size": 123456,
                        "digest": "sha256:abc123",
                        "browser_download_url": "https://github.com/krishhari2105/morphe-builds/releases/download/v2.1.0/youtube-morphe-v21.30.216-arm64.apk",
                    },
                ],
            }
        ]

        fake_manifest = {
            "source": "morphe",
            "results": [
                {
                    "app": "youtube",
                    "status": "success",
                    "version": "21.30.216",
                    "version_code": "1561187872",
                    "details": {"final_filename": "youtube-morphe-v21.30.216-arm64.apk"},
                }
            ],
        }

        downloaded_urls = []

        def mock_request_json(url, token):
            return fake_releases

        def mock_download_json(url, token):
            downloaded_urls.append(url)
            return fake_manifest

        with (
            patch.dict(os.environ, {"GITHUB_TOKEN": "test-token", "GITHUB_REPOSITORY": "test/repo"}),
            patch.object(generate_catalog, "request_json", side_effect=mock_request_json),
            patch.object(generate_catalog, "download_json", side_effect=mock_download_json),
        ):
            exit_code = generate_catalog.main()

        self.assertEqual(exit_code, 0)
        self.assertIn(
            "https://api.github.com/repos/krishhari2105/morphe-builds/releases/assets/720178419",
            downloaded_urls,
        )


if __name__ == "__main__":
    unittest.main()
