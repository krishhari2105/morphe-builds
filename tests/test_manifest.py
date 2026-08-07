import tempfile
import unittest
from pathlib import Path

from morphe_builder.manifest import build_tag, canonical_json, plan_digest, write_checksums


class ManifestTests(unittest.TestCase):
    def test_digest_is_order_independent(self):
        self.assertEqual(plan_digest({"b": 2, "a": 1}), plan_digest({"a": 1, "b": 2}))
        self.assertEqual(canonical_json({"b": 2, "a": 1}), '{"a":1,"b":2}')

    def test_tag_contains_patch_cli_and_plan_identity(self):
        tag = build_tag("morphe", "v1.38.0", "v1.13.0", {"apps": ["youtube"]})
        self.assertRegex(tag, r"^build-morphe-v1.38.0-v1.13.0-apps-[0-9a-f]{8}-[0-9a-f]{12}$")

    def test_app_set_identity_ignores_order_but_not_subset(self):
        first = build_tag("morphe", "v1", "v1", {"apps": ["youtube", "reddit"]})
        reordered = build_tag("morphe", "v1", "v1", {"apps": ["reddit", "youtube"]})
        subset = build_tag("morphe", "v1", "v1", {"apps": ["youtube"]})
        self.assertEqual(first, reordered)
        self.assertNotEqual(first, subset)

    def test_checksums_are_written(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            apk = root / "app.apk"
            apk.write_bytes(b"apk")
            sums = root / "SHA256SUMS"
            write_checksums(sums, [apk])
            self.assertIn("app.apk", sums.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
