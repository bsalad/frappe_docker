"""Offline tests for the two app lists and the modes of build-image.sh: no docker, no network.

Run with: cd finance/scripts && python3 -m unittest test_build_image
"""

import json
import os
import subprocess
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, "finance", "scripts", "build-image.sh")


def load(name):
    with open(os.path.join(ROOT, "finance", name)) as f:
        return json.load(f)


def is_hrms(app):
    return app["url"].rstrip("/").rsplit("/", 1)[-1] == "hrms"


class AppLists(unittest.TestCase):
    def test_live_list_has_no_hrms(self):
        self.assertFalse([app for app in load("apps.json") if is_hrms(app)])

    def test_copy_list_has_hrms_at_pinned_tag(self):
        hrms = [app for app in load("apps-copy.json") if is_hrms(app)]
        self.assertEqual(hrms, [{"url": "https://github.com/frappe/hrms", "branch": "v16.50.0"}])

    def test_copy_list_is_live_list_plus_hrms(self):
        live = load("apps.json")
        copy = load("apps-copy.json")
        self.assertEqual([app for app in copy if not is_hrms(app)], live)


class BuildImageModes(unittest.TestCase):
    def test_help_lists_both_modes(self):
        out = subprocess.run(
            ["sh", SCRIPT, "--help"], capture_output=True, text=True, check=True
        ).stdout
        self.assertIn("live|copy", out)
        self.assertIn("live", out)
        self.assertIn("copy", out)


if __name__ == "__main__":
    unittest.main()
