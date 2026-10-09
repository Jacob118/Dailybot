import io
import json
import threading
import unittest
from unittest.mock import patch

import app


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class UpdateCheckerTests(unittest.TestCase):
    def make_agent(self):
        agent = app.Agent.__new__(app.Agent)
        agent.lock = threading.RLock()
        agent.update_state = {}
        return agent

    def test_newer_release_is_reported_with_release_link(self):
        body = json.dumps({
            "tag_name": "v0.4.0",
            "body": "Bug fixes",
        }).encode()
        with patch.object(app.urllib.request, "urlopen", return_value=FakeResponse(body)) as urlopen:
            result = self.make_agent().check_for_updates()

        self.assertEqual(result["status"], "update-available")
        self.assertEqual(result["currentVersion"], "0.3.1")
        self.assertEqual(result["latestVersion"], "0.4.0")
        self.assertEqual(result["releaseUrl"], "https://github.com/Jacob118/Dailybot/releases/tag/v0.4.0")
        self.assertEqual(result["releaseNotes"], "Bug fixes")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 8)

    def test_equal_release_is_reported_as_up_to_date(self):
        body = json.dumps({"tag_name": "v0.3.1", "body": ""}).encode()
        with patch.object(app.urllib.request, "urlopen", return_value=FakeResponse(body)):
            result = self.make_agent().check_for_updates()

        self.assertEqual(result["status"], "up-to-date")
        self.assertEqual(result["latestVersion"], "0.3.1")

    def test_version_comparison_pads_missing_components(self):
        self.assertEqual(app.version_tuple("v1.2"), app.version_tuple("1.2.0"))
        self.assertGreater(app.version_tuple("1.2.1"), app.version_tuple("1.2"))


if __name__ == "__main__":
    unittest.main()
