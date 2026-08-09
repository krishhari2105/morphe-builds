import unittest

from morphe_builder.config import load_config
from morphe_builder.pipeline import Builder, PipelineError


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.builder = Builder.__new__(Builder)
        self.builder.config = self.config

    def test_candidate_versions_are_newest_first(self):
        app = self.config.apps["youtube"]
        self.assertEqual(
            self.builder._candidate_versions(app, ["19.1.0", "20.12.46", "v20.5.1"], None),
            ["20.12.46", "20.5.1", "19.1.0"],
        )

    def test_any_is_used_only_without_concrete_versions(self):
        app = self.config.apps["youtube"]
        self.assertEqual(self.builder._candidate_versions(app, ["Any"], None), [None])
        self.assertEqual(
            self.builder._candidate_versions(app, ["Any", "20.12.46"], None),
            ["20.12.46"],
        )

    def test_override_remains_exact(self):
        app = self.config.apps["youtube"]
        self.assertEqual(self.builder._candidate_versions(app, ["20.12.46"], "auto"), ["20.12.46"])
        self.assertEqual(self.builder._candidate_versions(app, ["20.12.46"], "v19.2.3"), ["19.2.3"])

    def test_source_app_scopes_match_requested_matrix(self):
        expected = {
            "morphe": {"youtube", "yt-music", "reddit"},
            "morphe-dev": {"youtube", "yt-music", "reddit"},
            "piko": {"twitter"},
            "piko-dev": {"twitter"},
            "de-revanced": {"gphotos"},
            "hoo-dles": {"proton-vpn"},
        }
        for source_key, apps in expected.items():
            self.assertEqual(set(self.config.sources[source_key].apps), apps)
            self.assertEqual(
                self.builder._select_apps(self.config.sources[source_key], ["all"]),
                [app for app in self.config.apps if app in apps],
            )

    def test_single_base_url_is_inferred_for_one_app(self):
        self.assertEqual(
            self.builder._merge_base_url(["gphotos"], {}, "https://example.com/photos.apk"),
            {"gphotos": "https://example.com/photos.apk"},
        )

    def test_base_urls_json_overrides_single_url(self):
        self.assertEqual(
            self.builder._merge_base_url(
                ["yt-music"],
                {"yt-music": "https://example.com/explicit.apk"},
                "https://example.com/simple.apk",
            ),
            {"yt-music": "https://example.com/explicit.apk"},
        )

    def test_single_base_url_rejects_multiple_apps(self):
        with self.assertRaisesRegex(PipelineError, "exactly one selected app"):
            self.builder._merge_base_url(
                ["youtube", "yt-music", "reddit"], {}, "https://example.com/base.apk"
            )

    def test_incompatible_source_app_is_rejected(self):
        with self.assertRaisesRegex(PipelineError, "not configured"):
            self.builder._select_apps(self.config.sources["piko"], ["youtube"])


if __name__ == "__main__":
    unittest.main()
