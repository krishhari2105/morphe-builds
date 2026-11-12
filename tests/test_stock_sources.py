import importlib.util
import unittest
from dataclasses import replace

from morphe_builder.config import load_config


class FakeHttp:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get_json(self, url, **kwargs):
        self.calls.append(url)
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return value


class StockSourceTests(unittest.TestCase):
    def resolver(self, provider, url, responses):
        self.assertIsNotNone(importlib.util.find_spec("morphe_builder.stock_sources"), "Stock source resolver missing")
        from morphe_builder.stock_sources import StockSources

        app = replace(load_config().apps["youtube"], download_sources={provider: (url,)})
        http = FakeHttp(responses)
        return StockSources(http), app, http

    def test_mirror_selects_exact_version_arm64_before_universal(self):
        endpoint = "https://api.github.com/repos/example/stock/releases/tags/com.google.android.youtube"
        assets = [
            {
                "name": "com.google.android.youtube-21.30.216-all.apkm",
                "browser_download_url": "https://github.com/example/stock/releases/download/pkg/all.apkm",
            },
            {
                "name": "com.google.android.youtube-21.30.216-arm64-v8a.apkm",
                "browser_download_url": "https://github.com/example/stock/releases/download/pkg/arm64.apkm",
            },
            {
                "name": "com.google.android.youtube-21.30.216-arm-v7a.apk",
                "browser_download_url": "https://github.com/example/stock/releases/download/pkg/arm.apk",
            },
            {
                "name": "com.google.android.youtube-21.31.1-all.apk",
                "browser_download_url": "https://github.com/example/stock/releases/download/pkg/newer.apk",
            },
            {
                "name": "com.other.app-21.30.216-all.apk",
                "browser_download_url": "https://github.com/example/stock/releases/download/pkg/wrong.apk",
            },
        ]
        resolver, app, _ = self.resolver(
            "github_mirror", "https://github.com/example/stock", {endpoint: {"assets": assets}}
        )
        found = list(resolver.iter_candidates("github_mirror", app, "21.30.216"))
        self.assertEqual([item.url.rsplit("/", 1)[-1] for item in found], ["arm64.apkm", "all.apkm"])
        self.assertEqual([item.version for item in found], ["21.30.216", "21.30.216"])

    def test_mirror_is_not_an_authority_for_latest(self):
        endpoint = "https://api.github.com/repos/example/stock/releases/tags/com.google.android.youtube"
        resolver, app, http = self.resolver(
            "github_mirror", "https://github.com/example/stock", {endpoint: {"assets": []}}
        )
        self.assertEqual(list(resolver.iter_candidates("github_mirror", app, None)), [])
        self.assertEqual(http.calls, [])

    def test_mirror_index_is_memoized_across_versions(self):
        endpoint = "https://api.github.com/repos/example/stock/releases/tags/com.google.android.youtube"
        resolver, app, http = self.resolver(
            "github_mirror", "https://github.com/example/stock", {endpoint: {"assets": []}}
        )
        list(resolver.iter_candidates("github_mirror", app, "21.30.216"))
        list(resolver.iter_candidates("github_mirror", app, "21.23.492"))
        self.assertEqual(http.calls, [endpoint])

    def test_failed_endpoint_is_not_retried_for_every_version(self):
        endpoint = "https://api.github.com/repos/example/stock/releases/tags/com.google.android.youtube"
        resolver, app, http = self.resolver(
            "github_mirror", "https://github.com/example/stock", {endpoint: RuntimeError("HTTP 403")}
        )
        for version in ("21.30.216", "21.23.492"):
            with self.assertRaisesRegex(RuntimeError, "403"):
                list(resolver.iter_candidates("github_mirror", app, version))
        self.assertEqual(http.calls, [endpoint])

    def test_archive_uses_metadata_and_rejects_history_and_wrong_version(self):
        files = [
            {"name": "apks/com.google.android.youtube/com.google.android.youtube-21.30.216-all.apk"},
            {"name": "apks/com.google.android.youtube/com.google.android.youtube-21.31.1-all.apk"},
            {"name": "history/files/apks/com.google.android.youtube/com.google.android.youtube-21.30.216-all.apk.~1~"},
        ]
        resolver, app, _ = self.resolver(
            "archive",
            "https://archive.org/download/stock/apks/{package}",
            {"https://archive.org/metadata/stock": {"files": files}},
        )
        found = list(resolver.iter_candidates("archive", app, "21.30.216"))
        self.assertEqual(
            [item.url for item in found],
            [
                "https://archive.org/download/stock/apks/com.google.android.youtube/com.google.android.youtube-21.30.216-all.apk"
            ],
        )

    def test_direct_url_template_keeps_exact_requested_version(self):
        resolver, app, _ = self.resolver("direct", "https://example.org/{package}-{version}.apk", {})
        found = list(resolver.iter_candidates("direct", app, "21.30.216"))
        self.assertEqual(found[0].url, "https://example.org/com.google.android.youtube-21.30.216.apk")
        self.assertEqual(found[0].version, "21.30.216")
        self.assertEqual(list(resolver.iter_candidates("direct", app, None)), [])

    def test_official_github_discovery_keeps_stable_tag_without_supported_binary(self):
        releases = [
            {"tag_name": "v22.0", "assets": []},
            {
                "tag_name": "v21.0",
                "assets": [
                    {
                        "name": "app.apk",
                        "browser_download_url": "https://github.com/example/app/releases/download/stable/app.apk",
                    }
                ],
            },
            {"tag_name": "v23.0-beta", "prerelease": True, "assets": []},
        ]
        resolver, app, _ = self.resolver(
            "github", "https://github.com/example/app", {"https://api.github.com/repos/example/app/releases": releases}
        )
        self.assertEqual(resolver.discover_versions("github", app), ["22.0", "21.0"])

    def test_official_github_skips_prereleases_and_drafts(self):
        releases = [
            {
                "tag_name": "v22.0-beta",
                "prerelease": True,
                "assets": [
                    {
                        "name": "app.apk",
                        "browser_download_url": "https://github.com/example/app/releases/download/beta/app.apk",
                    }
                ],
            },
            {"tag_name": "v23.0", "draft": True, "assets": []},
            {
                "tag_name": "v21.30.216",
                "prerelease": False,
                "draft": False,
                "assets": [
                    {
                        "name": "app.apk",
                        "browser_download_url": "https://github.com/example/app/releases/download/stable/app.apk",
                    }
                ],
            },
        ]
        resolver, app, _ = self.resolver(
            "github", "https://github.com/example/app", {"https://api.github.com/repos/example/app/releases": releases}
        )
        found = list(resolver.iter_candidates("github", app, None))
        self.assertEqual([item.version for item in found], ["21.30.216"])

    def test_mirror_rejects_non_https_asset_and_preserves_digest(self):
        endpoint = "https://api.github.com/repos/example/stock/releases/tags/com.google.android.youtube"
        assets = [
            {
                "name": "com.google.android.youtube-21.30.216-all.apk",
                "browser_download_url": "http://example.org/app.apk",
            },
            {
                "name": "com.google.android.youtube-21.30.216-arm64-v8a.apk",
                "browser_download_url": "https://github.com/example/stock/releases/download/pkg/app.apk",
                "digest": "sha256:" + "a" * 64,
            },
        ]
        resolver, app, _ = self.resolver(
            "github_mirror", "https://github.com/example/stock", {endpoint: {"assets": assets}}
        )
        found = list(resolver.iter_candidates("github_mirror", app, "21.30.216"))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].sha256, "a" * 64)


if __name__ == "__main__":
    unittest.main()
