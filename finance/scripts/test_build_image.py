"""Offline tests for the two app lists and the modes of build-image.sh: no docker, no network.

Run with: cd finance/scripts && python3 -m unittest test_build_image
"""

import json
import os
import shutil
import subprocess
import tempfile
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

    def run_in_layout(self, files, tag="t1", mode="live", existing_images=()):
        """Run the script from a scratch layout with a stub docker; return (code, stderr, docker calls)."""
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "finance", "scripts"))
            os.makedirs(os.path.join(tmp, "bin"))
            shutil.copy(SCRIPT, os.path.join(tmp, "finance", "scripts"))
            for name, apps in files.items():
                with open(os.path.join(tmp, "finance", name), "w") as f:
                    json.dump(apps, f)
            log = os.path.join(tmp, "docker.log")
            stub = os.path.join(tmp, "bin", "docker")
            with open(stub, "w") as f:
                f.write(
                    '#!/bin/sh\necho "$@" >> "%s"\n'
                    'if [ "$1 $2" = "image inspect" ]; then\n'
                    '  case " %s " in *" $3 "*) exit 0 ;; esac\n'
                    "  exit 1\nfi\nexit 1\n" % (log, " ".join(existing_images))
                )
            os.chmod(stub, 0o755)
            env = {"PATH": os.path.join(tmp, "bin") + os.pathsep + os.environ["PATH"]}
            run = subprocess.run(
                ["sh", os.path.join(tmp, "finance", "scripts", "build-image.sh"), mode, tag],
                capture_output=True, text=True, env=env,
            )
            calls = ""
            if os.path.exists(log):
                with open(log) as f:
                    calls = f.read()
            return run.returncode, run.stderr, calls

    def test_live_refuses_a_list_with_hrms(self):
        hrms = [{"url": "https://github.com/frappe/hrms", "branch": "v16.50.0"}]
        code, err, calls = self.run_in_layout({"apps.json": hrms})
        self.assertNotEqual(code, 0)
        self.assertIn("lists hrms", err)
        self.assertNotIn("build", calls)

    def test_copy_refuses_a_list_without_hrms(self):
        erpnext = [{"url": "https://github.com/frappe/erpnext", "branch": "v16.50.0"}]
        code, err, calls = self.run_in_layout({"apps-copy.json": erpnext}, mode="copy")
        self.assertNotEqual(code, 0)
        self.assertIn("no hrms", err)
        self.assertNotIn("build", calls)

    def test_refuses_an_existing_tag(self):
        erpnext = [{"url": "https://github.com/frappe/erpnext", "branch": "v16.50.0"}]
        code, err, calls = self.run_in_layout(
            {"apps.json": erpnext}, tag="t1", existing_images=["frappe-finance-custom:t1"]
        )
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertNotIn("build", calls)

    def test_refuses_an_existing_base_tag(self):
        erpnext = [{"url": "https://github.com/frappe/erpnext", "branch": "v16.50.0"}]
        code, err, _ = self.run_in_layout(
            {"apps.json": erpnext}, tag="t1", existing_images=["frappe-finance-custom:t1-base"]
        )
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

    def test_wrong_arguments_are_a_usage_error(self):
        for args in ([], ["bogus", "t1"], ["live"], ["live", "t1", "x"]):
            run = subprocess.run(["sh", SCRIPT] + args, capture_output=True, text=True)
            self.assertEqual(run.returncode, 2, args)


if __name__ == "__main__":
    unittest.main()
