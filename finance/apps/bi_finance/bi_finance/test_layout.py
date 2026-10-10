"""Offline guard on the layout Frappe syncs: a module's folders sit in <package>/<scrub(module)>/.

Frappe's migrate walks only that folder for doctype, report, workspace, number_card, dashboard_chart
and page, so a synced folder anywhere else is never loaded. Pure Python, no site:

    python3 -m unittest bi_finance.test_layout

from this app's directory, or in the image as in finance/docs/erpnext-setup.md.
"""

import os
import unittest

PACKAGE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(PACKAGE)
SYNCED = {"doctype", "report", "workspace", "number_card", "dashboard_chart", "page"}


def scrub(name):
    # frappe.scrub
    return name.replace(" ", "_").replace("-", "_").lower()


def modules():
    with open(os.path.join(PACKAGE, "modules.txt"), encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


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


if __name__ == "__main__":
    unittest.main()
