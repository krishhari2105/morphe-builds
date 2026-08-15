import tempfile
import unittest
from pathlib import Path

from morphe_builder.apkmirror import ApkMirrorRateLimited, ApkMirrorResolver, Link, _challenge
from morphe_builder.config import load_config
from morphe_builder.http import HttpClient


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

        def iter_from_release(app, version, link, listing):
            attempted.append(link.href)
            if "/x-12-7-1-release-0-release/" in link.href:
                return iter([("https://download.example/twitter.apk", link.href)])
            raise RuntimeError("not found")

        resolver._iter_from_release = iter_from_release
        candidates = resolver.resolve_candidates(app, "12.7.1-release.0")
        self.assertEqual(candidates[0][0], "https://download.example/twitter.apk")
        self.assertEqual(attempted[0], "https://www.apkmirror.com/apk/x-corp/twitter/x-12-7-1-release-0-release/")

    def test_latest_resolution_stops_after_first_candidate(self):
        app = load_config().apps["youtube"]
        resolver = ApkMirrorResolver.__new__(ApkMirrorResolver)
        resolver._html = lambda url, referer=None: (
            '<a href="/apk/google-inc/youtube/youtube-19-1-0-release/">19.1.0</a>'
            '<a href="/apk/google-inc/youtube/youtube-20-12-46-release/">20.12.46</a>'
            '<a href="/apk/google-inc/youtube/youtube-21-0-0-beta-release/">21.0.0 beta</a>'
            '<a href="/apk/google-inc/chrome/google-chrome-151-0-7922-71-release/">Chrome 151.0.7922.71</a>',
            url,
        )
        attempted = []

        def iter_from_release(app, version, link, listing):
            attempted.append(version)
            return iter([(f"https://download.example/{version}.apk", link.href)])

        resolver._iter_from_release = iter_from_release
        candidate = resolver.resolve_latest(app)
        self.assertEqual(candidate[2], "20.12.46")
        self.assertEqual(attempted, ["20.12.46"])

    def test_prefers_arm64_variant(self):
        app = load_config().apps["youtube"]
        arm64 = Link("https://www.apkmirror.com/a-arm64-android-apk-download/", "arm64-v8a")
        x86 = Link("https://www.apkmirror.com/a-x86-android-apk-download/", "x86")
        self.assertGreater(
            ApkMirrorResolver._variant_score(arm64, app),
            ApkMirrorResolver._variant_score(x86, app),
        )

    def test_release_resolution_limits_variant_pages(self):
        app = load_config().apps["gphotos"]
        resolver = ApkMirrorResolver.__new__(ApkMirrorResolver)
        resolver.max_variant_attempts = 3
        release = Link("https://www.apkmirror.com/release/", "Google Photos")
        variant_links = "".join(
            f'<a href="{release.href}variant-{index}-android-apk-download/">arm64-v8a</a>' for index in range(6)
        )
        visited = []

        def html(url, referer=None):
            visited.append(url)
            if url == release.href:
                return variant_links, url
            index = url.split("variant-")[1].split("-")[0]
            return f'<a id="download-link" href="https://download.apkmirror.com/{index}.apk">Download</a>', url

        resolver._html = html
        candidates = resolver._resolve_from_release(app, "latest", release, app.apkmirror_url)
        self.assertEqual(len(candidates), 3)
        self.assertEqual(len(visited), 4)

    def test_duplicate_unlabelled_variants_discover_arm64_before_download(self):
        app = load_config().apps["yt-music"]
        resolver = ApkMirrorResolver.__new__(ApkMirrorResolver)
        resolver.max_variant_attempts = 3
        release = Link(
            "https://www.apkmirror.com/apk/google-inc/youtube-music/youtube-music-9-15-51-release/",
            "YouTube Music 9.15.51",
        )
        armv7 = release.href + "youtube-music-9-15-51-7-android-apk-download/"
        arm64 = release.href + "youtube-music-9-15-51-4-android-apk-download/"
        release_html = (
            f'<a href="{armv7}">9.15.51</a>'
            f'<a href="{armv7}"></a>'
            f'<a href="{armv7}">9.15.51</a>'
            f'<a href="{arm64}">9.15.51</a>'
        )
        armv7_page = (
            f'<a href="{armv7}">(arm-v7a) (nodpi) (Android 8.0+) APK</a>'
            f'<a href="{arm64}">(arm64-v8a) (nodpi) (Android 8.0+) APK</a>'
            '<a id="download-link" href="https://download.apkmirror.com/armv7.apk">Download</a>'
        )
        arm64_page = (
            f'<a href="{arm64}">(arm64-v8a) (nodpi) (Android 8.0+) APK</a>'
            '<a id="download-link" href="https://download.apkmirror.com/arm64.apk">Download</a>'
        )
        visited = []

        def html(url, referer=None):
            visited.append(url)
            if url == release.href:
                return release_html, url
            if url == armv7:
                return armv7_page, url
            if url == arm64:
                return arm64_page, url
            raise AssertionError(f"Unexpected URL: {url}")

        resolver._html = html
        candidates = resolver._resolve_from_release(app, "9.15.51", release, app.apkmirror_url)
        self.assertEqual(candidates, [("https://download.apkmirror.com/arm64.apk", arm64)])
        self.assertEqual(visited, [release.href, armv7, arm64])

    def test_download_flow_does_not_probe_terminal_binary_twice(self):
        class Response:
            def __init__(self, url, content_type, text="", body=b""):
                self.url = url
                self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
                self.text = text
                self.body = body
                self.status_code = 200
                self.is_redirect = False
                self.is_permanent_redirect = False

            def raise_for_status(self):
                pass

            def close(self):
                pass

            def iter_content(self, chunk_size):
                yield self.body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        class Session:
            def __init__(self, responses):
                self.responses = responses
                self.calls = []
                self.headers = {}

            def get(self, url, **kwargs):
                self.calls.append(url)
                return self.responses.pop(0)

        responses = [
            Response(
                "https://www.apkmirror.com/intermediate/",
                "text/html",
                '<a id="download-link" href="https://download.apkmirror.com/final.apk">here</a>',
            ),
            Response("https://download.apkmirror.com/final.apk", "application/octet-stream", body=b"apk"),
        ]
        http = HttpClient()
        http.session = Session(responses)
        resolver = ApkMirrorResolver(http, min_request_interval=0)
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "base.apk"
            result = resolver.download_candidate(
                "https://www.apkmirror.com/start/",
                "https://www.apkmirror.com/variant/",
                destination,
            )
            self.assertEqual(destination.read_bytes(), b"apk")
            self.assertEqual(result.final_url, "https://download.apkmirror.com/final.apk")
        self.assertEqual(
            http.session.calls,
            [
                "https://www.apkmirror.com/start/",
                "https://download.apkmirror.com/final.apk",
            ],
        )

    def test_resolver_uses_browser_user_agent_with_injected_client(self):
        http = HttpClient()
        ApkMirrorResolver(http, min_request_interval=0)
        self.assertIn("Mozilla/5.0", http.session.headers["User-Agent"])

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
