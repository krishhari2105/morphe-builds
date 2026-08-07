import unittest

from morphe_builder.versions import VersionParseError, parse_compatible_versions, version_key


OUTPUT = """
INFO: Package name: com.google.android.youtube
20.12.46 (58 patches)
20.10.38
INFO: Package name: com.twitter.android
12.7.1-release.0
INFO: Package name: com.spotify.music
Any
"""


class VersionTests(unittest.TestCase):
    def test_parses_numeric_text_and_any_versions(self):
        parsed = parse_compatible_versions(OUTPUT)
        self.assertEqual(parsed["com.google.android.youtube"], ["20.12.46", "20.10.38"])
        self.assertEqual(parsed["com.twitter.android"], ["12.7.1-release.0"])
        self.assertEqual(parsed["com.spotify.music"], ["Any"])

    def test_rejects_unrecognized_output(self):
        with self.assertRaises(VersionParseError):
            parse_compatible_versions("no package metadata here")

    def test_mixed_versions_sort_deterministically(self):
        values = ["1.9.0", "1.10.0", "1.10.0-dev.2"]
        self.assertEqual(sorted(values, key=version_key, reverse=True)[0], "1.10.0-dev.2")


if __name__ == "__main__":
    unittest.main()
