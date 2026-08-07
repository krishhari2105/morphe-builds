import json
import shutil
import tempfile
import unittest
from pathlib import Path

from morphe_builder.config import CONFIG_DIR, ConfigError, load_config, resolve_patch_selection


class ConfigTests(unittest.TestCase):
    def test_repository_configuration_is_valid(self):
        config = load_config()
        self.assertEqual(config.apps["youtube"].package, "com.google.android.youtube")
        self.assertTrue(config.sources["morphe"].scheduled)
        self.assertEqual(config.sources["morphe-dev"].channel, "prerelease")

    def test_patch_selection_merges_defaults_and_app(self):
        config = load_config()
        enable, disable = resolve_patch_selection(config, "morphe", "youtube")
        self.assertEqual(enable, [])
        self.assertEqual(disable, [])

    def test_rejects_malformed_source_patch_lists(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for source in CONFIG_DIR.glob("*.json"):
                shutil.copy2(source, root / source.name)
            path = root / "patches.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["sources"] = {"morphe": {"defaults": {"enable": "PatchName"}}}
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(root)


if __name__ == "__main__":
    unittest.main()
