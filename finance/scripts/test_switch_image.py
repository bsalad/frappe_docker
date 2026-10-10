"""Offline tests for switch_image.py and switch-image.sh: invented tags, stub docker and curl,
no network. Run with: cd finance/scripts && python3 -m unittest test_switch_image
"""

import os
import shutil
import subprocess
import tempfile
import unittest

import switch_image

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(ROOT, "finance", "scripts", "switch-image.sh")
PY = os.path.join(ROOT, "finance", "scripts", "switch_image.py")

COMPOSE = """\
services:
  frontend:
    image: frappe-finance-custom:t-old
    ports: !override
      - "127.0.0.1:8080:8080"
  backend:
    image: frappe-finance-custom:t-old
  scheduler:
    image: frappe-finance-custom:t-old
"""


class Functions(unittest.TestCase):
    def test_current_tag_reads_the_one_tag(self):
        self.assertEqual(switch_image.current_tag(COMPOSE), "t-old")

    def test_current_tag_refuses_a_mix(self):
        mixed = COMPOSE.replace("scheduler:\n    image: frappe-finance-custom:t-old", "scheduler:\n    image: frappe-finance-custom:t-other")
        with self.assertRaises(ValueError):
            switch_image.current_tag(mixed)

    def test_current_tag_refuses_no_tag(self):
        with self.assertRaises(ValueError):
            switch_image.current_tag("services:\n  frontend:\n    image: other/thing:1\n")

    def test_set_tag_moves_every_service_and_keeps_other_lines(self):
        moved = switch_image.set_tag(COMPOSE, "t-new")
        self.assertEqual(switch_image.current_tag(moved), "t-new")
        self.assertEqual(moved.count("frappe-finance-custom:t-new"), 3)
        self.assertIn('      - "127.0.0.1:8080:8080"', moved)
        self.assertIn("ports: !override", moved)

    def test_remove_after_switch_lists_old_and_new_base_and_finance(self):
        self.assertEqual(
            switch_image.remove_after_switch("t-old", "t-new"),
            [
                "frappe-finance-custom:t-old",
                "frappe-finance-custom:t-old-base",
                "frappe-finance-custom:t-new-base",
                "frappe-finance-custom:t-new-finance",
            ],
        )

    def test_remove_refuses_the_same_tag(self):
        with self.assertRaises(ValueError):
            switch_image.remove_after_switch("t-old", "t-old")

    def test_live_compose_file_runs_one_tag(self):
        with open(os.path.join(ROOT, "finance-local.yml")) as f:
            switch_image.current_tag(f.read())


class SwitchShell(unittest.TestCase):
    """switch-image.sh in a scratch layout, with stub docker (logs its calls) and stub curl."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        os.makedirs(os.path.join(self.tmp, "finance", "scripts"))
        os.makedirs(os.path.join(self.tmp, "bin"))
        shutil.copy(SCRIPT, os.path.join(self.tmp, "finance", "scripts"))
        shutil.copy(PY, os.path.join(self.tmp, "finance", "scripts"))
        with open(os.path.join(self.tmp, "pwd.yml"), "w") as f:
            f.write("services: {}\n")
        self.compose = os.path.join(self.tmp, "finance-local.yml")
        with open(self.compose, "w") as f:
            f.write(COMPOSE)
        self.log = os.path.join(self.tmp, "calls.log")

    def stub(self, name, body):
        path = os.path.join(self.tmp, "bin", name)
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)

    def run_switch(self, args, existing, ping):
        images = " ".join(existing)
        self.stub(
            "docker",
            'echo "$@" >> "%s"\n'
            'if [ "$1 $2" = "image inspect" ]; then\n'
            '  case " %s " in *" $3 "*) exit 0 ;; esac\n'
            "  exit 1\nfi\nexit 0\n" % (self.log, images),
        )
        self.stub("curl", 'echo "%s"\n' % ping)
        env = {
            "PATH": os.path.join(self.tmp, "bin") + os.pathsep + os.environ["PATH"],
            "SWITCH_PING_TRIES": "2",
            "SWITCH_PING_WAIT": "0",
        }
        run = subprocess.run(
            ["sh", os.path.join(self.tmp, "finance", "scripts", "switch-image.sh")] + args,
            capture_output=True, text=True, env=env,
        )
        calls = ""
        if os.path.exists(self.log):
            with open(self.log) as f:
                calls = f.read()
        with open(self.compose) as f:
            compose = f.read()
        return run, calls, compose

    def test_success_switches_and_removes_old_and_new_base(self):
        run, calls, compose = self.run_switch(
            ["t-new"],
            existing=["frappe-finance-custom:t-old", "frappe-finance-custom:t-old-base",
                      "frappe-finance-custom:t-new", "frappe-finance-custom:t-new-base",
                      "frappe-finance-custom:t-new-finance"],
            ping="200",
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(switch_image.current_tag(compose), "t-new")
        self.assertIn("up -d", calls)
        self.assertIn("rmi frappe-finance-custom:t-old\n", calls)
        self.assertIn("rmi frappe-finance-custom:t-old-base\n", calls)
        self.assertIn("rmi frappe-finance-custom:t-new-base\n", calls)
        self.assertIn("rmi frappe-finance-custom:t-new-finance\n", calls)
        self.assertNotIn("rmi frappe-finance-custom:t-new\n", calls)

    def test_ping_failure_rolls_back_and_keeps_the_old_image(self):
        run, calls, compose = self.run_switch(
            ["t-new"],
            existing=["frappe-finance-custom:t-old", "frappe-finance-custom:t-new"],
            ping="502",
        )
        self.assertEqual(run.returncode, 1)
        self.assertEqual(switch_image.current_tag(compose), "t-old")
        self.assertIn("rolling finance-local.yml back", run.stderr)
        self.assertEqual(calls.count("up -d"), 2)
        self.assertNotIn("rmi", calls)

    def test_refuses_a_new_image_that_is_not_built(self):
        run, calls, compose = self.run_switch(
            ["t-new"], existing=["frappe-finance-custom:t-old"], ping="200"
        )
        self.assertEqual(run.returncode, 1)
        self.assertIn("not built", run.stderr)
        self.assertEqual(switch_image.current_tag(compose), "t-old")
        self.assertNotIn("compose", calls)

    def test_refuses_the_tag_already_running(self):
        run, calls, compose = self.run_switch(
            ["t-old"], existing=["frappe-finance-custom:t-old"], ping="200"
        )
        self.assertEqual(run.returncode, 1)
        self.assertIn("already runs", run.stderr)
        self.assertNotIn("compose", calls)

    def test_wrong_arguments_are_a_usage_error(self):
        for args in ([], ["t-new", "x"]):
            run = subprocess.run(["sh", SCRIPT] + args, capture_output=True, text=True)
            self.assertEqual(run.returncode, 2, args)


if __name__ == "__main__":
    unittest.main()
