"""Offline tests for the VAT step of swiss-setup.py: the bexio code table and the
comparison behind `vat --check`. No ERPNext, no network; frappe is stubbed.

Run with: python3 -m unittest discover -s finance/scripts -p 'test_*.py'
"""

import importlib.util
import os
import sys
import types
import unittest
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))

# swiss-setup.py imports frappe at load time; only the table and the pure
# functions are tested here, so a stub is enough.
sys.modules.setdefault("frappe", types.ModuleType("frappe"))
sys.modules.setdefault("frappe.utils", types.ModuleType("frappe.utils"))
sys.modules["frappe.utils"].get_bench_path = lambda: ""

spec = importlib.util.spec_from_file_location("swiss_setup", os.path.join(HERE, "swiss-setup.py"))
ss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ss)


class BexioTable(unittest.TestCase):
    def test_all_42_codes(self):
        self.assertEqual(len(ss.BEXIO_TAXES), 42)

    def test_bexio_ids_unique_per_kind(self):
        ids = Counter(row[1] for row in ss.BEXIO_TAXES)
        self.assertEqual(max(ids.values()), 1)

    def test_codes_repeat_only_for_two_periods(self):
        codes = Counter(row[2] for row in ss.BEXIO_TAXES)
        self.assertEqual({c for c, n in codes.items() if n > 1}, {"VES", "VEV", "VKÜ"})

    def test_titles_unique(self):
        titles = [ss.tax_title(row[2], row[4], row[3]) for row in ss.BEXIO_TAXES]
        self.assertEqual(len(titles), len(set(titles)))

    def test_title_names_code_and_rate(self):
        self.assertEqual(ss.tax_title("UN81", 8.1, "Normalsatz"), "UN81 8.1% Normalsatz")
        self.assertEqual(ss.tax_title("UEX", 0, "Export, steuerbefreit"), "UEX 0% Export, steuerbefreit")

    def test_accounts_are_kmu_accounts(self):
        for row in ss.BEXIO_TAXES:
            self.assertIn(row[7], ss.FIXED_ACCOUNTS)
            if row[8]:
                self.assertEqual(row[8], "2202")

    def test_vat_books_to_bexio_transitory_accounts(self):
        # bexio books sales VAT at the invoice date on 2202, and purchase VAT at the bill date on 1172 (moved to
        # 1170 or 1171 only when the bill is paid); a template on 2200 or 1170/1171 would book it where bexio does not
        for row in ss.BEXIO_TAXES:
            if row[0] == "S":
                self.assertEqual(row[7], "2202", row[2])
            elif row[2] not in ("VES", "VEV", "VKÜ"):
                self.assertEqual(row[7], "1172", row[2])

    def test_reverse_charge_only_on_bezugsteuer(self):
        for row in ss.BEXIO_TAXES:
            self.assertEqual(row[8] is not None, row[2].startswith("BZ"), row[2])

    def test_defaults_are_the_8_1_codes(self):
        by_id = {row[1]: row for row in ss.BEXIO_TAXES}
        self.assertEqual(by_id[ss.DEFAULT_SALES][2:5], ("UN81", "Normalsatz", 8.1))
        self.assertEqual(by_id[ss.DEFAULT_PURCHASE][2:5], ("VM81", "Normalsatz Material/DL", 8.1))


class TaxRows(unittest.TestCase):
    def setUp(self):
        ss.account = lambda number: "%s - Konto - bic" % number

    def test_reverse_charge_nets_to_zero(self):
        rows = ss.tax_rows("Purchase Taxes and Charges Template", "P", "1171", "2203", 8.1)
        self.assertEqual([(r["account_head"], r["add_deduct_tax"], r["rate"]) for r in rows],
                         [("1171 - Konto - bic", "Add", 8.1), ("2203 - Konto - bic", "Deduct", 8.1)])

    def test_sales_row(self):
        rows = ss.tax_rows("Sales Taxes and Charges Template", "S", "2200", None, 8.1)
        self.assertEqual(rows, [{"charge_type": "On Net Total", "account_head": "2200 - Konto - bic", "rate": 8.1}])


class Differences(unittest.TestCase):
    taxes = [
        {"id": 28, "code": "UN81", "value": 8.1, "account_id": 127, "is_active": True},
        {"id": 3, "code": "UEX", "value": 0, "account_id": 127, "is_active": True},
        {"id": 18, "code": "US37", "value": 3.7, "account_id": 127, "is_active": False},
    ]
    numbers = {"127": "2200"}

    def erp(self):
        return {"28": ("UN81", 8.1, "2200", False), "3": ("UEX", 0.0, "2200", False),
                "18": ("US37", 3.7, "2200", True)}

    def test_match_gives_no_difference(self):
        self.assertEqual(ss.vat_differences(self.taxes, self.numbers, self.erp()), [])

    def test_rate_account_and_active_are_compared(self):
        erp = self.erp()
        erp["28"] = ("UN81", 7.7, "2200", False)
        erp["3"] = ("UEX", 0.0, "1170", False)
        erp["18"] = ("US37", 3.7, "2200", False)
        diffs = ss.vat_differences(self.taxes, self.numbers, erp)
        self.assertEqual(len(diffs), 3)
        self.assertTrue(any("rate" in d for d in diffs))
        self.assertTrue(any("account" in d for d in diffs))
        self.assertTrue(any("disabled" in d for d in diffs))

    def test_missing_and_extra(self):
        erp = self.erp()
        del erp["3"]
        erp["99"] = ("UXX", 0.0, "2200", False)
        diffs = ss.vat_differences(self.taxes, self.numbers, erp)
        self.assertTrue(any(d.startswith("missing: 3") for d in diffs))
        self.assertTrue(any(d.startswith("extra: 99") for d in diffs))


if __name__ == "__main__":
    unittest.main()
