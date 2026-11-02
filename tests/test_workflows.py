import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from morphe_builder.github import GitHubClient


class BuildWorkflowTests(unittest.TestCase):
    def test_unsigned_preparation_authenticates_release_lookup(self):
        workflow = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build.yml"
        # Read the scalar environment entries from the preparation step, without
        # introducing a YAML dependency for this one workflow integration check.
        step = workflow.read_text(encoding="utf-8").split("      - name: Prepare unsigned ", 1)[1]
        step = step.split("      - ", 1)[0]
        env_block = step.split("        env:\n", 1)[1].split("        run:", 1)[0]
        secrets = {"secrets.GITHUB_TOKEN": "read-only-workflow-token"}
        env = {}
        for name, value in re.findall(r"^          (\w+): (.+)$", env_block, re.MULTILINE):
            if value.startswith("${{") and value.endswith("}}"):
                env[name] = secrets.get(value[3:-2].strip(), "")

        def release_api(request, **kwargs):
            response = requests.Response()
            response.request = request
            response.url = request.url
            response.status_code = (
                200 if request.headers.get("Authorization") == "Bearer read-only-workflow-token" else 403
            )
            response._content = b"[]" if response.status_code == 200 else b'{"message": "rate limit exceeded"}'
            return response

        with patch.dict(os.environ, env, clear=True):
            client = GitHubClient()
            with patch.object(client.http.session, "send", side_effect=release_api):
                releases = client.list_releases("MorpheApp/morphe-patches")
        self.assertEqual(releases, [])


if __name__ == "__main__":
    unittest.main()
