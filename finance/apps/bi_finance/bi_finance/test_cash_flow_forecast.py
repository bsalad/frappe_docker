"""Offline tests of the Cash Flow Forecast report's reads of the books: which suppliers are the insurers.

Invented accounts and suppliers only; no site, no network. The database is mocked. The module imports frappe, so run
it in the image, as test_treasury.py does (finance/docs/erpnext-setup.md):

    docker run --rm -v "$PWD/finance/apps/bi_finance:/home/frappe/bi_finance_src:ro" \
        frappe-finance-custom:v16.50.0-swiss-bi6 \
        sh -c 'cd /home/frappe/bi_finance_src && ../frappe-bench/env/bin/python -m unittest -v bi_finance.test_cash_flow_forecast'
"""

import unittest
from unittest import mock

from bi_finance.bi_finance.report.cash_flow_forecast import cash_flow_forecast as cff


class InsurerSuppliers(unittest.TestCase):
    def patched(self, accounts, rows):
        frappe = mock.Mock()
        frappe.get_all.return_value = accounts
        frappe.db.sql.return_value = rows
        return mock.patch.object(cff, "frappe", frappe), frappe

    def test_the_suppliers_of_bills_with_an_item_on_an_insurer_payable(self):
        patch, frappe = self.patched(["2270 - Test Pension - TC", "2279 - Test Tax - TC"],
                                     [("Test Insurer A",), ("Test Insurer B",)])
        with patch:
            self.assertEqual(cff.insurer_suppliers("Test Company"), {"Test Insurer A", "Test Insurer B"})
        self.assertEqual(frappe.db.sql.call_args.args[1],
                         ("Test Company", ["2270 - Test Pension - TC", "2279 - Test Tax - TC"]))

    def test_the_payables_are_looked_up_by_account_number_for_the_company(self):
        patch, frappe = self.patched([], [])
        with patch:
            cff.insurer_suppliers("Test Company")
        self.assertEqual(frappe.get_all.call_args.kwargs["filters"],
                         {"company": "Test Company", "account_number": ["between", ["2270", "2279"]]})

    def test_no_insurer_payable_no_insurer(self):
        patch, frappe = self.patched([], [])
        with patch:
            self.assertEqual(cff.insurer_suppliers("Test Company"), set())
        frappe.db.sql.assert_not_called()


if __name__ == "__main__":
    unittest.main()
