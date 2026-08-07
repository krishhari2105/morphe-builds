import unittest

from morphe_builder.apkmirror import ApkMirrorResolver, Link, _challenge
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

    def test_prefers_arm64_variant(self):
        app = load_config().apps["youtube"]
        arm64 = Link("https://www.apkmirror.com/a-arm64-android-apk-download/", "arm64-v8a")
        x86 = Link("https://www.apkmirror.com/a-x86-android-apk-download/", "x86")
        self.assertGreater(
            ApkMirrorResolver._variant_score(arm64, app),
            ApkMirrorResolver._variant_score(x86, app),
        )

    def test_detects_challenge_pages(self):
        self.assertTrue(_challenge("<html><title>Just a moment...</title><div class='cf-chl-test'>"))


if __name__ == "__main__":
    unittest.main()
