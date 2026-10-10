"""Offline guard on the layout Frappe syncs: a module's folders sit in <package>/<scrub(module)>/.

Frappe's migrate walks only that folder for doctype, report, workspace, number_card, dashboard_chart
and page, so a synced folder anywhere else is never loaded. Pure Python, no site:

    python3 -m unittest bi_finance.test_layout

from this app's directory, or in the image as in finance/docs/erpnext-setup.md.

The second guard is on the stamps. Migrate skips a synced file whose 'modified' equals the stored
row's, so a content change without a new stamp never reaches the site. synced_manifest.json, next
to this file, records per synced JSON the sha of its content without 'modified' and that stamp.
A changed file must carry a newer stamp; the builder then updates the manifest with it.
"""

import hashlib
import json
import os
import re
import unittest
from datetime import datetime

PACKAGE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(PACKAGE)
MANIFEST = os.path.join(PACKAGE, "synced_manifest.json")
SYNCED = {"doctype", "report", "workspace", "number_card", "dashboard_chart", "page"}
STAMP = "%Y-%m-%d %H:%M:%S.%f"  # Frappe's 'modified' format


def scrub(name):
    # frappe.scrub
    return name.replace(" ", "_").replace("-", "_").lower()


def modules():
    with open(os.path.join(PACKAGE, "modules.txt"), encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def synced_jsons():
    # the JSON files under a synced folder of a module folder, relative to the package
    found = []
    for module in modules():
        home = os.path.join(PACKAGE, scrub(module))
        for root, _dirs, files in os.walk(home):
            inside = set(os.path.relpath(root, home).split(os.sep))
            if not SYNCED & inside:
                continue
            found += [os.path.relpath(os.path.join(root, f), PACKAGE) for f in files if f.endswith(".json")]
    return sorted(path.replace(os.sep, "/") for path in found)


def fingerprint(path):
    # (sha of the document without 'modified', 'modified')
    with open(os.path.join(PACKAGE, path), encoding="utf-8") as f:
        doc = json.load(f)
    body = {k: v for k, v in doc.items() if k != "modified"}
    sha = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()
    return sha, doc["modified"]


def stale(recorded, current):
    # files whose content differs from the manifest and whose stamp is not newer than the recorded one
    out = []
    for path, (sha, modified) in current.items():
        if path not in recorded or sha == recorded[path]["sha"]:
            continue
        if datetime.strptime(modified, STAMP) <= datetime.strptime(recorded[path]["modified"], STAMP):
            out.append(path)
    return sorted(out)


class Layout(unittest.TestCase):
    def test_each_module_has_a_folder_that_is_a_package(self):
        for module in modules():
            folder = os.path.join(PACKAGE, scrub(module))
            self.assertTrue(os.path.isfile(os.path.join(folder, "__init__.py")), folder)

    def test_no_synced_folder_sits_outside_a_module_folder(self):
        homes = [os.path.join(PACKAGE, scrub(module)) + os.sep for module in modules()]
        stray = []
        for root, dirs, _files in os.walk(APP):
            if any((root + os.sep).startswith(home) for home in homes):
                continue
            stray += [os.path.join(root, d) for d in dirs if d in SYNCED]
        self.assertEqual(stray, [])


class Package(unittest.TestCase):
    def test_pyproject_names_this_app_so_the_image_installs_it(self):
        # the layer runs pip install -e; flit looks for a module named after the project, so a
        # project named after another app (bi_finance) fails the build
        with open(os.path.join(APP, "pyproject.toml"), encoding="utf-8") as f:
            name = re.search(r'^name = "([^"]+)"', f.read(), re.M).group(1)
        self.assertEqual(name, "bi_payroll")
        self.assertTrue(os.path.isfile(os.path.join(PACKAGE, "__init__.py")))


class Stamps(unittest.TestCase):
    def setUp(self):
        with open(MANIFEST, encoding="utf-8") as f:
            self.recorded = json.load(f)

    def test_every_synced_json_is_in_the_manifest(self):
        current = synced_jsons()
        self.assertEqual(sorted(set(current) - set(self.recorded)), [], "add to synced_manifest.json")
        self.assertEqual(sorted(set(self.recorded) - set(current)), [], "gone from the module folders")

    def test_a_changed_synced_json_has_a_newer_stamp(self):
        current = {path: fingerprint(path) for path in synced_jsons()}
        changed = stale(self.recorded, current)
        # the sha and stamp are printed so the manifest can be updated with them
        self.assertEqual(changed, [], "; ".join(f"{p} modified {current[p][1]} sha {current[p][0]}" for p in changed))


class StaleRule(unittest.TestCase):
    RECORDED = {"a.json": {"sha": "x", "modified": "2026-10-10 09:00:00.000000"}}

    def test_changed_content_with_the_same_stamp_is_stale(self):
        self.assertEqual(stale(self.RECORDED, {"a.json": ("y", "2026-10-10 09:00:00.000000")}), ["a.json"])

    def test_changed_content_with_an_older_stamp_is_stale(self):
        self.assertEqual(stale(self.RECORDED, {"a.json": ("y", "2026-10-10 08:00:00.000000")}), ["a.json"])

    def test_changed_content_with_a_newer_stamp_passes(self):
        self.assertEqual(stale(self.RECORDED, {"a.json": ("y", "2026-10-10 10:00:00.000000")}), [])

    def test_unchanged_content_passes_whatever_the_stamp(self):
        self.assertEqual(stale(self.RECORDED, {"a.json": ("x", "2026-10-10 08:00:00.000000")}), [])


if __name__ == "__main__":
    unittest.main()
