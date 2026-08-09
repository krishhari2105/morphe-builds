import unittest
from types import SimpleNamespace

from morphe_builder.apkmirror import ApkMirrorRateLimited, ApkMirrorResolver, Link, _challenge
from morphe_builder.config import load_config


class ApkMirrorTests(unittest.TestCase):
    def test_extracts_absolute_apkmirror_links(self):
        html = '<a id="download-link" href="/wp-content/themes/APKMirror/download.php?id=1">Download</a>'
        links = ApkMirrorResolver._links(html, "https://www.apkmirror.com/apk/example/")
        self.assertEqual(links[0].element_id, "download-link")
        self.assertTrue(links[0].href.startswith("https://www.apkmirror.com/"))

    def test_constructs_release_url_for_versions_not_on_first_listing_page(self):
        app = load_config().apps["youtube"]
        self.assertEqual(
            ApkMirrorResolver._constructed_release_url(app, "21.04.223"),
            "https://www.apkmirror.com/apk/google-inc/youtube/youtube-21-04-223-release/",
        )

    def test_constructs_modern_twitter_release_urls(self):
        app = load_config().apps["twitter"]
        urls = ApkMirrorResolver._constructed_release_urls(app, "12.7.1-release.0")
        self.assertEqual(
            urls[0],
            "https://www.apkmirror.com/apk/x-corp/twitter/x-12-7-1-release-0-release/",
        )
        self.assertIn(
            "https://www.apkmirror.com/apk/x-corp/twitter/x-12-7-1-release-0-release/",
            urls,
        )

    def test_fallback_tries_modern_twitter_release_url(self):
        app = load_config().apps["twitter"]
        resolver = ApkMirrorResolver.__new__(ApkMirrorResolver)
        resolver._html = lambda url, referer=None: ("", url)
        attempted = []

        def resolve_from_release(app, version, link, listing):
            attempted.append(link.href)
            if "/x-12-7-1-release-0-release/" in link.href:
                return [("https://download.example/twitter.apk", link.href)]
            raise RuntimeError("not found")

        resolver._resolve_from_release = resolve_from_release
        candidates = resolver.resolve_candidates(app, "12.7.1-release.0")
        self.assertEqual(candidates[0][0], "https://download.example/twitter.apk")
        self.assertEqual(attempted[0], "https://www.apkmirror.com/apk/x-corp/twitter/x-12-7-1-release-0-release/")

    def test_latest_candidates_are_sorted_and_skip_prereleases(self):
        app = load_config().apps["youtube"]
        resolver = ApkMirrorResolver.__new__(ApkMirrorResolver)
        resolver._html = lambda url, referer=None: (
            '<a href="/apk/google-inc/youtube/youtube-19-1-0-release/">19.1.0</a>'
            '<a href="/apk/google-inc/youtube/youtube-20-12-46-release/">20.12.46</a>'
            '<a href="/apk/google-inc/youtube/youtube-21-0-0-beta-release/">21.0.0 beta</a>'
            '<a href="/apk/google-inc/chrome/google-chrome-151-0-7922-71-release/">Chrome 151.0.7922.71</a>',
            url,
        )
        resolver._resolve_from_release = lambda app, version, link, listing: [
            (f"https://download.example/{version}.apk", link.href)
        ]
        candidates = resolver.resolve_latest_candidates(app)
        self.assertEqual([item[2] for item in candidates], ["20.12.46", "19.1.0"])

    def test_prefers_arm64_variant(self):
        app = load_config().apps["youtube"]
        arm64 = Link("https://www.apkmirror.com/a-arm64-android-apk-download/", "arm64-v8a")
        x86 = Link("https://www.apkmirror.com/a-x86-android-apk-download/", "x86")
        self.assertGreater(
            ApkMirrorResolver._variant_score(arm64, app),
            ApkMirrorResolver._variant_score(x86, app),
        )

    def test_follows_intermediate_html_to_binary_download(self):
        class Response:
            def __init__(self, url, content_type, text=""):
                self.url = url
                self.headers = {"Content-Type": content_type}
                self.text = text
                self.status_code = 200

            def raise_for_status(self):
                pass

            def close(self):
                pass

        responses = [
            Response(
                "https://www.apkmirror.com/intermediate/",
                "text/html",
                '<a id="download-link" href="https://download.apkmirror.com/final.apk">here</a>',
            ),
            Response("https://download.apkmirror.com/final.apk", "application/octet-stream"),
        ]
        session = SimpleNamespace(get=lambda *args, **kwargs: responses.pop(0))
        resolver = ApkMirrorResolver.__new__(ApkMirrorResolver)
        resolver.http = SimpleNamespace(session=session, timeout=(20, 120))
        resolver.min_request_interval = 0
        resolver._last_request_at = None
        self.assertEqual(
            resolver._follow_download_pages(
                "https://www.apkmirror.com/start/", "https://www.apkmirror.com/variant/"
            ),
            "https://download.apkmirror.com/final.apk",
        )

    def test_rate_limit_stops_variant_iteration(self):
        app = load_config().apps["gphotos"]
        resolver = ApkMirrorResolver.__new__(ApkMirrorResolver)
        resolver._html = lambda url, referer=None: (_ for _ in ()).throw(ApkMirrorRateLimited("120"))
        release = Link("https://www.apkmirror.com/release/", "Google Photos")
        with self.assertRaises(ApkMirrorRateLimited) as raised:
            resolver._resolve_from_release(app, "latest", release, app.apkmirror_url)
        self.assertEqual(raised.exception.retry_after, "120")

    def test_detects_challenge_pages(self):
        self.assertTrue(_challenge("<html><title>Just a moment...</title><div class='cf-chl-test'>"))


if __name__ == "__main__":
    unittest.main()
