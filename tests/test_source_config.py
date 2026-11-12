import json
import shutil
import tempfile
import unittest
from pathlib import Path

from morphe_builder.config import CONFIG_DIR, ConfigError, load_config


class SourceConfigTests(unittest.TestCase):
    def test_every_app_has_stock_backups_and_publisher_pins(self):
        config = load_config()
        for app in config.apps.values():
            with self.subTest(app=app.key):
                self.assertTrue(app.download_sources.get("github_mirror"))
                self.assertTrue(app.download_sources.get("uptodown"))
                self.assertTrue(app.download_sources.get("apkpure"))
                self.assertTrue(app.download_sources.get("apkcombo"))
                self.assertTrue(app.signing_cert_sha256)
                self.assertTrue(all(len(pin) == 64 for pin in app.signing_cert_sha256))

    def invalid_config(self, **overrides):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for source in CONFIG_DIR.glob("*.json"):
                shutil.copy2(source, root / source.name)
            path = root / "apps.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["apps"]["youtube"].update(overrides)
            path.write_text(json.dumps(data), encoding="utf-8")
            return load_config(root)

    def test_rejects_unknown_provider(self):
        with self.assertRaisesRegex(ConfigError, "provider"):
            self.invalid_config(download_sources={"typo": ["https://example.org"]})

    def test_rejects_non_https_backup(self):
        with self.assertRaisesRegex(ConfigError, "HTTPS"):
            self.invalid_config(download_sources={"direct": ["http://example.org/app.apk"]})

    def test_rejects_malformed_publisher_pin(self):
        with self.assertRaisesRegex(ConfigError, "certificate|fingerprint"):
            self.invalid_config(signing_cert_sha256=["not-a-fingerprint"])

    def test_rejects_sources_string_instead_of_array(self):
        with self.assertRaises(ConfigError):
            self.invalid_config(download_sources={"direct": "https://example.org/app.apk"})


if __name__ == "__main__":
    unittest.main()
