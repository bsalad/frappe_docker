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
    def test_live_list_has_hrms_at_pinned_tag(self):
        # Payroll runs on the live site (finance/docs/hrms.md, Decision 2026-10-10).
        hrms = [app for app in load("apps.json") if is_hrms(app)]
        self.assertEqual(hrms, [{"url": "https://github.com/frappe/hrms", "branch": "v16.50.0"}])

    def test_copy_list_has_hrms_at_pinned_tag(self):
        hrms = [app for app in load("apps-copy.json") if is_hrms(app)]
        self.assertEqual(hrms, [{"url": "https://github.com/frappe/hrms", "branch": "v16.50.0"}])

    def test_copy_list_is_live_list(self):
        self.assertEqual(load("apps-copy.json"), load("apps.json"))


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
                    "  exit 1\nfi\n"
                    'if [ "$1" = build ]; then exit 0; fi\nexit 1\n' % (log, " ".join(existing_images))
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

    def test_live_builds_base_then_bi_finance_then_bi_payroll(self):
        # Each layer sits on the one before it; the live tag is the last layer.
        hrms = [{"url": "https://github.com/frappe/hrms", "branch": "v16.50.0"}]
        code, err, calls = self.run_in_layout({"apps.json": hrms}, tag="t1")
        self.assertEqual(code, 0, err)
        builds = [line for line in calls.splitlines() if line.startswith("build ")]
        self.assertEqual(len(builds), 3, calls)
        self.assertIn("--tag frappe-finance-custom:t1-base ", builds[0])
        self.assertIn("--build-arg BASE=frappe-finance-custom:t1-base --tag frappe-finance-custom:t1-finance ", builds[1])
        self.assertIn("--file finance/images/bi_finance.Containerfile", builds[1])
        self.assertIn("--build-arg BASE=frappe-finance-custom:t1-finance --tag frappe-finance-custom:t1 ", builds[2])
        self.assertIn("--file finance/images/bi_payroll.Containerfile", builds[2])

    def test_refuses_an_existing_finance_layer_tag(self):
        erpnext = [{"url": "https://github.com/frappe/erpnext", "branch": "v16.50.0"}]
        code, err, calls = self.run_in_layout(
            {"apps.json": erpnext}, tag="t1", existing_images=["frappe-finance-custom:t1-finance"]
        )
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
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


APPLY = os.path.join(ROOT, "finance", "scripts", "apply-patches.sh")
PATCH_DIR = os.path.join(ROOT, "finance", "patches", "erpnextswiss")

# The lines of erpnextswiss item_tools.py at the pin around the stray comma (its line 43):
# the context of the real patch, so the patch applies to this invented file as to the app.
ITEM_TOOLS = [
    "def get_voucher_value(voucher_code, customer):",
    '    value = frappe.db.sql("""',
    "                SELECT ...",
    "                WHERE ",
    "                    `item_code` = %(voucher)s",
    "                    AND `parent` IN (SELECT `name` FROM `tabSales Invoice` WHERE `docstatus` = 1 AND `customer` = %(customer)s);\"\"\",",
    "            ,",
    "            {",
    "                'voucher': voucher_code,",
    "                'customer': customer",
    "            },",
    "            as_dict=True)",
]


def git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )


class PatchStep(unittest.TestCase):
    """apply-patches.sh, the image build's patch step, on invented trees: no docker, no network."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.app = os.path.join(self.tmp, "app")
        self.patches = os.path.join(self.tmp, "patches")
        os.makedirs(self.app)
        os.makedirs(self.patches)
        git(self.app, "init", "-q")

    def commit(self, name, text):
        """Write a file into the app and commit it: the app at its pin."""
        os.makedirs(os.path.dirname(os.path.join(self.app, name)), exist_ok=True)
        with open(os.path.join(self.app, name), "w") as f:
            f.write(text)
        git(self.app, "add", name)
        git(self.app, "commit", "-q", "-m", "pin")

    def patch(self, name, text):
        with open(os.path.join(self.patches, name), "w") as f:
            f.write(text)

    def apply(self, patch_dir=None):
        return subprocess.run(
            ["sh", APPLY, patch_dir or self.patches, self.app],
            capture_output=True, text=True,
        )

    def read(self, name):
        with open(os.path.join(self.app, name)) as f:
            return f.read()

    def test_applies_in_name_order(self):
        self.commit("tool.py", "VALUE = 1\nOTHER = 2\n")
        self.patch("0001-value.patch", "--- a/tool.py\n+++ b/tool.py\n@@ -1,2 +1,2 @@\n-VALUE = 1\n+VALUE = 3\n OTHER = 2\n")
        # Applies only after 0001: its context reads VALUE = 3.
        self.patch("0002-value-again.patch", "--- a/tool.py\n+++ b/tool.py\n@@ -1,2 +1,2 @@\n-VALUE = 3\n+VALUE = 5\n OTHER = 2\n")
        run = self.apply()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.read("tool.py"), "VALUE = 5\nOTHER = 2\n")
        self.assertIn("applied: " + os.path.join(self.patches, "0002-value-again.patch"), run.stdout)

    def test_a_patch_that_does_not_apply_stops_the_step_and_names_it(self):
        self.commit("tool.py", "VALUE = 1\nOTHER = 2\n")
        self.patch("0001-broken.patch", "--- a/tool.py\n+++ b/tool.py\n@@ -1,2 +1,2 @@\n-VALUE = 9\n+VALUE = 3\n OTHER = 2\n")
        run = self.apply()
        self.assertEqual(run.returncode, 1)
        self.assertIn("patch does not apply to " + self.app + ": " + os.path.join(self.patches, "0001-broken.patch"), run.stderr)
        self.assertEqual(self.read("tool.py"), "VALUE = 1\nOTHER = 2\n")

    def test_relative_paths_work(self):
        # Regression: a relative patch path was read from the app's directory, not the caller's.
        self.commit("tool.py", "VALUE = 1\nOTHER = 2\n")
        self.patch("0001-value.patch", "--- a/tool.py\n+++ b/tool.py\n@@ -1,2 +1,2 @@\n-VALUE = 1\n+VALUE = 3\n OTHER = 2\n")
        run = subprocess.run(
            ["sh", APPLY, os.path.relpath(self.patches, self.tmp), os.path.relpath(self.app, self.tmp)],
            cwd=self.tmp, capture_output=True, text=True,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.read("tool.py"), "VALUE = 3\nOTHER = 2\n")

    def test_no_patches_is_a_no_op(self):
        self.commit("tool.py", "VALUE = 1\n")
        run = self.apply()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.read("tool.py"), "VALUE = 1\n")

    def test_wrong_arguments_are_a_usage_error(self):
        run = subprocess.run(["sh", APPLY, self.patches], capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)

    def test_our_item_tools_patch_fixes_the_stray_comma(self):
        # Regression: at the pin, item_tools.py does not compile (line 43, a bare comma).
        source = "\n".join(ITEM_TOOLS) + "\n"
        with self.assertRaises(SyntaxError):
            compile(source, "item_tools.py", "exec")
        # The patch names the file from the repository root: erpnextswiss/scripts/item_tools.py.
        path = "erpnextswiss/scripts/item_tools.py"
        self.commit(path, source)
        run = subprocess.run(["sh", APPLY, PATCH_DIR, self.app], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        compile(self.read(path), path, "exec")


if __name__ == "__main__":
    unittest.main()
