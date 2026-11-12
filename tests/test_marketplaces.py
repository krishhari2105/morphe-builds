"""Provider fixtures model public markup, never a challenge-solving service."""

import base64
import json
import unittest
from dataclasses import replace

import requests

from morphe_builder.http import HttpClient
from morphe_builder.marketplaces import MarketplaceSources
from morphe_builder.models import AppConfig

APP = AppConfig(
    "youtube", "YouTube", "com.google.android.youtube", "", ("apk", "xapk", "apks"), ("arm64-v8a", "universal")
)
UP = "https://youtube.en.uptodown.com/android"
PURE = "https://apkpure.com/youtube/com.google.android.youtube"
COMBO = "https://apkcombo.com/youtube/com.google.android.youtube"


def application(version):
    return (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "SoftwareApplication",
                "name": "YouTube",
                "softwareVersion": version,
            }
        )
        + "</script>"
    )


def pure_link(abi="arm64-v8a", kind="APK", package=APP.package):
    return f"https://d.apkpure.com/b/{kind}/{package}?versionCode=123&nc={abi}"


def pure_page(version, *links):
    return application(version) + "".join(f'<a href="{url}">Download</a>' for url in links)


def up_root(version="21.1.0"):
    return application(version) + '<h1 id="detail-app-name" data-code="16906">YouTube</h1>'


def up_item(version, file_id):
    return {
        "version": version,
        "fileID": file_id,
        "kindFile": "apk",
        "versionURL": {
            "url": UP,
            "extraURL": "download",
            "versionID": file_id,
        },
    }


def up_download(version="21.1.0", token="signed/file.apk", abi="arm64-v8a"):
    return (
        application(version)
        + f'''<table><tr><th>Architecture</th><td>{abi}</td></tr></table>
        <button id="detail-download-button" data-url="{token}" data-only-xapk="0">Download</button>'''
    )


class FakeSession:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url not in self.pages:
            raise AssertionError(f"Unexpected page visit: {url}")
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        status, body, headers = value if isinstance(value, tuple) else (200, value, {})
        if isinstance(body, dict):
            body = json.dumps(body)
            headers = {"Content-Type": "application/json", **headers}
        response = requests.Response()
        response.url = url
        response.status_code = status
        response.headers.update(headers)
        response.encoding = "utf-8"
        response._content = body.encode()
        response._content_consumed = True
        return response


class MarketplaceTests(unittest.TestCase):
    def resolver(self, pages):
        http = HttpClient(token="github-secret", connect_timeout=60, read_timeout=120)
        http.session.close()
        http.session = FakeSession(pages)
        return MarketplaceSources(http), http.session

    def test_latest_version_discovery_does_not_require_challenge_gated_binary_link(self):
        resolver, session = self.resolver(
            {
                UP: up_root(),
                UP + "/apps/16906/versions/1": {"data": [up_item("21.1.0", "123")]},
            }
        )
        self.assertTrue(hasattr(resolver, "discover_versions"), "Independent version discovery missing")
        self.assertEqual(resolver.discover_versions("uptodown", UP, APP), ["21.1.0"])
        self.assertNotIn(UP + "/download/123", [url for url, _ in session.calls])

    def test_uptodown_resolves_observed_public_button_token(self):
        resolver, session = self.resolver(
            {
                UP: up_root(),
                UP + "/apps/16906/versions/1": {"data": [up_item("21.1.0", "123")]},
                UP + "/download/123": up_download(),
            }
        )
        self.assertEqual(
            list(resolver.iter_candidates("uptodown", UP, APP, "21.1.0")),
            [
                ("https://dw.uptodown.com/dwn/signed/file.apk", UP + "/download/123", "21.1.0"),
            ],
        )
        for _, request in session.calls:
            self.assertIn("text/html", request["headers"]["Accept"])
            self.assertIsNone(request["headers"].get("Authorization"))
            self.assertFalse(request["allow_redirects"])
            self.assertLessEqual(max(request["timeout"]), 30)

    def test_uptodown_discovery_sorts_stable_versions_not_api_order(self):
        items = [
            up_item("21.1.0", "1"),
            up_item("22.0-beta", "2"),
            up_item("21.3.0", "3"),
            up_item("21.2.0-SECONDARY", "4"),
        ]
        resolver, _ = self.resolver(
            {
                UP: up_root("22.0-beta"),
                UP + "/apps/16906/versions/1": {"data": items},
                UP + "/download/1": up_download("21.1.0"),
                UP + "/download/3": up_download("21.3.0"),
            }
        )
        candidates = list(resolver.iter_candidates("uptodown", UP, APP, None))
        self.assertEqual([item[2] for item in candidates], ["21.3.0", "21.1.0"])

    def test_uptodown_turnstile_download_is_not_bypassed(self):
        resolver, session = self.resolver(
            {
                UP: up_root(),
                UP + "/apps/16906/versions/1": {"data": [up_item("21.1.0", "123")]},
                UP + "/download/123": application("21.1.0")
                + '<button id="detail-download-button" data-app-id="16906" data-file-id="123"></button><div id="download-turnstile-widget"></div>',
            }
        )
        with self.assertRaisesRegex(RuntimeError, "(?i)(blocked|challenge|turnstile)"):
            list(resolver.iter_candidates("uptodown", UP, APP, "21.1.0"))
        self.assertEqual(len(session.calls), 3)

    def test_uptodown_exact_search_has_bounded_pagination(self):
        resolver, session = self.resolver(
            {
                UP: up_root(),
                **{UP + f"/apps/16906/versions/{page}": {"data": [up_item("20.0", str(page))]} for page in range(1, 4)},
            }
        )
        with self.assertRaisesRegex(RuntimeError, "21.1.0"):
            list(resolver.iter_candidates("uptodown", UP, APP, "21.1.0"))
        self.assertEqual(len(session.calls), 4)

    def test_apkpure_prefers_arm64_and_excludes_wrong_package_and_abi(self):
        good, universal = pure_link(), pure_link("arm64-v8a%2Carmeabi-v7a", "XAPK")
        resolver, _ = self.resolver(
            {
                PURE + "/downloading/21.1.0": pure_page(
                    "21.1.0",
                    pure_link("armeabi-v7a"),
                    universal,
                    pure_link(package="org.adware"),
                    good,
                )
            }
        )
        self.assertEqual(
            list(resolver.iter_candidates("apkpure", PURE, APP, "21.1.0")),
            [
                (good, PURE + "/downloading/21.1.0", "21.1.0"),
                (universal, PURE + "/downloading/21.1.0", "21.1.0"),
            ],
        )

    def test_apkpure_exact_version_does_not_accept_prefix_or_latest(self):
        resolver, _ = self.resolver({PURE + "/downloading/21.1": pure_page("21.10", pure_link())})
        with self.assertRaisesRegex(RuntimeError, "(?i)(mismatch|21.1)"):
            list(resolver.iter_candidates("apkpure", PURE, APP, "21.1"))

    def test_apkpure_discovery_uses_version_listing_and_sorts_before_fetching(self):
        resolver, _ = self.resolver(
            {
                PURE + "/versions": '<a href="' + PURE + '/downloading/21.1.0">21.1.0</a>'
                '<a href="' + PURE + '/downloading/21.3.0">21.3.0</a>'
                '<a href="' + PURE + '/downloading/22.0-beta">22.0-beta</a>'
                '<a href="https://apkpure.com/advert/org.ads/downloading/99.0">99.0</a>',
                PURE + "/downloading/21.1.0": pure_page("21.1.0", pure_link()),
                PURE + "/downloading/21.3.0": pure_page("21.3.0", pure_link()),
            }
        )
        self.assertEqual([c[2] for c in resolver.iter_candidates("apkpure", PURE, APP, None)], ["21.3.0", "21.1.0"])

    def test_apkcombo_decodes_observed_download_wrapper_and_prefers_arm64(self):
        target = f"https://download.pureapk.com/b/XAPK/{APP.package}?versionCode=123&nc=arm64-v8a"
        token = base64.urlsafe_b64encode(target.encode()).decode()
        page = (
            application("21.1.0")
            + '<code>armeabi-v7a</code><a href="https://download.apkcombo.com/wrong.xapk">Download</a>'
        )
        page += f'<code>arm64-v8a</code><a href="https://apkcombo.com/d?u={token}">Download XAPK</a>'
        resolver, _ = self.resolver({COMBO + "/download/phone-21.1.0-apk": page})
        self.assertEqual(
            next(resolver.iter_candidates("apkcombo", COMBO, APP, "21.1.0")),
            (
                target,
                COMBO + "/download/phone-21.1.0-apk",
                "21.1.0",
            ),
        )

    def test_apkcombo_discovers_only_same_app_stable_versions(self):
        resolver, _ = self.resolver(
            {
                COMBO + "/download/apk": '<a href="' + COMBO + '/download/phone-21.3.0-apk">21.3.0</a>'
                '<a href="' + COMBO + '/download/phone-22.0-beta-apk">22.0 beta</a>'
                '<a href="https://apkcombo.com/other/org.adware/download/phone-99.0-apk">99.0</a>',
                COMBO + "/download/phone-21.3.0-apk": application("21.3.0")
                + '<code>arm64-v8a</code><a href="https://download.apkcombo.com/yt.apk">Download APK</a>',
            }
        )
        self.assertEqual(
            next(resolver.iter_candidates("apkcombo", COMBO, APP, None)),
            (
                "https://download.apkcombo.com/yt.apk",
                COMBO + "/download/phone-21.3.0-apk",
                "21.3.0",
            ),
        )

    def test_supported_formats_are_enforced(self):
        resolver, _ = self.resolver({PURE + "/downloading/21.1.0": pure_page("21.1.0", pure_link(kind="XAPK"))})
        with self.assertRaisesRegex(RuntimeError, "(?i)(candidate|format|download)"):
            list(resolver.iter_candidates("apkpure", PURE, replace(APP, formats=("apk",)), "21.1.0"))

    def test_untrusted_or_insecure_links_never_escape(self):
        for url in [
            "http://d.apkpure.com/b/APK/" + APP.package,
            "https://d.apkpure.com.evil.test/a.apk",
            "https://d.apkpure.com@evil.test/a.apk",
            "https://d.apkpure.com:444/b/APK/" + APP.package,
            "https://ads.example/a.apk",
        ]:
            with self.subTest(url=url):
                resolver, _ = self.resolver({PURE + "/downloading/21.1.0": pure_page("21.1.0", url)})
                with self.assertRaises(RuntimeError):
                    list(resolver.iter_candidates("apkpure", PURE, APP, "21.1.0"))

    def test_wrapper_cannot_smuggle_arbitrary_download_host(self):
        token = base64.urlsafe_b64encode(b"https://evil.test/payload.apk").decode()
        resolver, _ = self.resolver(
            {
                COMBO + "/download/phone-21.1.0-apk": application("21.1.0")
                + f'<code>arm64-v8a</code><a href="/d?u={token}">Download</a>',
                COMBO + "/download/phone-21.1.0-xapk": (404, "", {}),
                COMBO + "/download/phone-21.1.0-apks": (404, "", {}),
            }
        )
        with self.assertRaises(RuntimeError):
            list(resolver.iter_candidates("apkcombo", COMBO, APP, "21.1.0"))

    def test_rate_limit_stops_without_visiting_alternate_pages(self):
        resolver, session = self.resolver(
            {PURE + "/downloading/21.1.0": (429, "Too many requests", {"Retry-After": "600"})}
        )
        with self.assertRaisesRegex(RuntimeError, "429"):
            list(resolver.iter_candidates("apkpure", PURE, APP, "21.1.0"))
        self.assertEqual(len(session.calls), 1)

    def test_challenge_is_reported_not_parsed_as_versions(self):
        resolver, _ = self.resolver(
            {PURE + "/versions": '<title>Just a moment...</title><div id="cf-chl-widget">Verify you are human</div>'}
        )
        with self.assertRaisesRegex(RuntimeError, "(?i)(blocked|challenge)"):
            list(resolver.iter_candidates("apkpure", PURE, APP, None))

    def test_unsafe_redirect_is_rejected_before_following(self):
        resolver, session = self.resolver({PURE + "/versions": (302, "", {"Location": "https://evil.test/private"})})
        with self.assertRaisesRegex(RuntimeError, "(?i)(unsafe|untrusted|redirect)"):
            list(resolver.iter_candidates("apkpure", PURE, APP, None))
        self.assertEqual(len(session.calls), 1)

    def test_network_failure_is_clear_runtime_error(self):
        resolver, _ = self.resolver({PURE + "/versions": requests.Timeout("timed out")})
        with self.assertRaisesRegex(RuntimeError, "(?i)(apkpure|timed out)"):
            list(resolver.iter_candidates("apkpure", PURE, APP, None))

    def test_prerelease_exact_request_is_rejected_before_network(self):
        resolver, session = self.resolver({})
        for version in ["21.0-beta", "21.0rc1", "21.0-alpha", "21.0-dev", "21.0-SECONDARY"]:
            with self.subTest(version=version), self.assertRaisesRegex(RuntimeError, "(?i)stable"):
                list(resolver.iter_candidates("apkpure", PURE, APP, version))
        self.assertEqual(session.calls, [])

    def test_invalid_provider_or_canonical_url_is_rejected_before_network(self):
        resolver, session = self.resolver({})
        for provider, url in [
            ("unknown", PURE),
            ("apkpure", "http://apkpure.com/youtube/" + APP.package),
            ("apkpure", "https://evil.test/youtube/" + APP.package),
            ("apkpure", "https://apkpure.com/ads/org.adware"),
        ]:
            with self.subTest(provider=provider, url=url), self.assertRaises(RuntimeError):
                list(resolver.iter_candidates(provider, url, APP, None))
        self.assertEqual(session.calls, [])

    def test_http_blocks_are_memoized_across_apps_and_versions(self):
        for status in (403, 429):
            with self.subTest(status=status):
                resolver, session = self.resolver({PURE + "/downloading/21.1.0": (status, "Blocked", {})})
                for app_url, value in [
                    (PURE, "21.1.0"),
                    (PURE, "21.2.0"),
                    ("https://apkpure.com/music/com.google.android.apps.youtube.music", "8.0"),
                ]:
                    app = APP if app_url == PURE else replace(APP, package="com.google.android.apps.youtube.music")
                    with self.assertRaisesRegex(RuntimeError, str(status)):
                        list(resolver.iter_candidates("apkpure", app_url, app, value))
                self.assertEqual(len(session.calls), 1)

    def test_missing_pages_are_memoized_without_blocking_other_versions(self):
        resolver, session = self.resolver(
            {
                PURE + "/downloading/21.1.0": (404, "Not found", {}),
                PURE + "/downloading/21.2.0": pure_page("21.2.0", pure_link()),
            }
        )
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "404"):
                list(resolver.iter_candidates("apkpure", PURE, APP, "21.1.0"))
        self.assertEqual(next(resolver.iter_candidates("apkpure", PURE, APP, "21.2.0"))[2], "21.2.0")
        self.assertEqual(len(session.calls), 2)

    def test_apkcombo_group_label_cannot_borrow_abi_from_neighbour(self):
        resolver, _ = self.resolver(
            {
                COMBO + "/download/phone-21.1.0-apk": application("21.1.0")
                + '<div><code>armeabi-v7a</code><a href="https://download.apkcombo.com/wrong.apk">APK</a>'
                + '<code>arm64-v8a</code><a href="https://download.apkcombo.com/right.apk">APK</a></div>'
            }
        )
        self.assertEqual(
            [c[0] for c in resolver.iter_candidates("apkcombo", COMBO, APP, "21.1.0")],
            ["https://download.apkcombo.com/right.apk"],
        )

    def test_uptodown_rejects_page_with_wrong_package(self):
        resolver, _ = self.resolver(
            {
                UP: up_root(),
                UP + "/apps/16906/versions/1": {"data": [up_item("21.1.0", "123")]},
                UP + "/download/123": up_download()
                + '<a href="https://play.google.com/store/apps/details?id=org.adware">Google Play</a>',
            }
        )
        with self.assertRaisesRegex(RuntimeError, "(?i)package"):
            list(resolver.iter_candidates("uptodown", UP, APP, "21.1.0"))

    def test_apkpure_does_not_emit_unpinned_latest_download(self):
        resolver, _ = self.resolver(
            {
                PURE + "/downloading/21.1.0": pure_page(
                    "21.1.0", "https://d.apkpure.com/b/APK/com.google.android.youtube?version=latest&nc=arm64-v8a"
                )
            }
        )
        with self.assertRaises(RuntimeError):
            list(resolver.iter_candidates("apkpure", PURE, APP, "21.1.0"))

    def test_uptodown_public_variant_listing_prefers_arm64(self):
        initial = (
            up_download(abi="armeabi-v7a")
            + '<h1 id="detail-app-name" data-code="16906"></h1><button data-version="14488049">Variants</button>'
        )
        resolver, _ = self.resolver(
            {
                UP: up_root(),
                UP + "/apps/16906/versions/1": {"data": [up_item("21.1.0", "123")]},
                UP + "/download/123": initial,
                "https://youtube.en.uptodown.com/app/16906/version/14488049/files": {
                    "content": '<p>armeabi-v7a</p><div class="variant"><img data-file-id="123"><span title="apk">APK</span></div>'
                    '<p>arm64-v8a</p><div class="variant"><img data-file-id="456"><span title="apk">APK</span></div>'
                },
                UP + "/download/456": up_download(token="arm64/file.apk"),
            }
        )
        self.assertEqual(
            next(resolver.iter_candidates("uptodown", UP, APP, "21.1.0")),
            (
                "https://dw.uptodown.com/dwn/arm64/file.apk",
                UP + "/download/456",
                "21.1.0",
            ),
        )

    def test_apkpure_h2_version_metadata_is_accepted(self):
        resolver, _ = self.resolver(
            {
                PURE + "/downloading/21.1.0": "<h2>Download YouTube APK 21.1.0</h2>"
                + f'<a id="download_link" href="{pure_link()}">Download</a>'
            }
        )
        self.assertEqual(next(resolver.iter_candidates("apkpure", PURE, APP, "21.1.0"))[2], "21.1.0")

    def test_discovery_excludes_beta_label_even_if_version_number_has_no_suffix(self):
        resolver, _ = self.resolver(
            {
                PURE
                + "/versions": f'<a href="{PURE}/downloading/22.0">22.0 beta</a><a href="{PURE}/downloading/21.1.0">21.1.0</a>',
                PURE + "/downloading/21.1.0": pure_page("21.1.0", pure_link()),
            }
        )
        self.assertEqual(next(resolver.iter_candidates("apkpure", PURE, APP, None))[2], "21.1.0")

    def test_discovery_limits_release_page_visits(self):
        listing = "".join(f'<a href="{PURE}/downloading/{v}.0">{v}.0</a>' for v in range(1, 101))
        resolver, session = self.resolver(
            {
                PURE + "/versions": listing,
                **{PURE + f"/downloading/{v}.0": pure_page(f"{v}.0", pure_link()) for v in range(96, 101)},
            }
        )
        self.assertEqual(
            [c[2] for c in resolver.iter_candidates("apkpure", PURE, APP, None)],
            ["100.0", "99.0", "98.0", "97.0", "96.0"],
        )
        self.assertEqual(len(session.calls), 6)

    def test_apkcombo_does_not_hide_missing_latest_with_an_older_release(self):
        resolver, _ = self.resolver(
            {
                COMBO + "/download/apk": f'<a href="{COMBO}/download/phone-21.2.0-apk">21.2.0</a>'
                f'<a href="{COMBO}/download/phone-21.1.0-apk">21.1.0</a>',
                COMBO + "/download/phone-21.2.0-apk": application("21.2.0"),
            }
        )
        with self.assertRaisesRegex(RuntimeError, "21.2.0"):
            next(resolver.iter_candidates("apkcombo", COMBO, APP, None))

    def test_oversized_html_is_rejected(self):
        resolver, _ = self.resolver({PURE + "/versions": "x" * (2_000_000 + 1)})
        with self.assertRaisesRegex(RuntimeError, "(?i)(size|large|limit)"):
            list(resolver.iter_candidates("apkpure", PURE, APP, None))


if __name__ == "__main__":
    unittest.main()
