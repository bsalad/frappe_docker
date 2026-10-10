"""Offline tests of the Cash Flow Forecast report's reads of the books: which suppliers are the insurers.

Invented accounts and suppliers only; no site, no network. The database is mocked. The module imports frappe, so run
it in the image, as test_treasury.py does (finance/docs/erpnext-setup.md):

    docker run --rm -v "$PWD/finance/apps/bi_finance:/home/frappe/bi_finance_src:ro" \
        frappe-finance-custom:v16.50.0-swiss-bi6 \
        sh -c 'cd /home/frappe/bi_finance_src && ../frappe-bench/env/bin/python -m unittest -v bi_finance.test_cash_flow_forecast'
"""

import datetime
import types
import unittest
from unittest import mock

from bi_finance import cash_forecast as cf
from bi_finance.bi_finance.report.cash_flow_forecast import cash_flow_forecast as cff

D = datetime.date
AS_OF = D(2026, 10, 10)


def bank_line(name, day, withdrawal, description):
    return types.SimpleNamespace(name=name, date=day, withdrawal=withdrawal, description=description)


class BankOutflows(unittest.TestCase):
    def patched(self, lines, transfer_je=(), transfer_pe=()):
        frappe = mock.Mock()
        frappe.get_all.side_effect = [["Test Bank - TC"], lines]
        # the four reconciliation queries in order: payroll or VAT, bill payment, transfer by Journal Entry, transfer by Payment Entry
        frappe.db.sql.side_effect = [[], [], list(transfer_je), list(transfer_pe)]
        return mock.patch.object(cff, "frappe", frappe)

    def test_a_monthly_transfer_to_an_own_account_is_not_a_recurring_cost(self):
        # the money moves between two company accounts, both in the opening cash: the outflow is no cost
        lines = [bank_line("BT-1", D(2026, 7, 5), 500.0, "Transfer Invented Savings"),
                 bank_line("BT-2", D(2026, 8, 5), 500.0, "Transfer Invented Savings"),
                 bank_line("BT-3", D(2026, 9, 5), 500.0, "Transfer Invented Savings")]
        with self.patched(lines, transfer_je=[("BT-1",), ("BT-2",), ("BT-3",)]):
            kept, left_out = cff.bank_outflows("Test Company", AS_OF, [])
        self.assertEqual(kept, [])
        self.assertEqual(left_out["transfer between own bank accounts"], 3)
        self.assertEqual(cf.recurring_costs(kept, AS_OF, strict=True), [])

    def test_a_cost_next_to_a_transfer_stays_a_recurring_cost(self):
        lines = [bank_line("BT-1", D(2026, 7, 5), 500.0, "Transfer Invented Savings"),
                 bank_line("BT-2", D(2026, 7, 9), 20.0, "Subscr Invented Cloud Inv 1001"),
                 bank_line("BT-3", D(2026, 8, 9), 20.0, "Subscr Invented Cloud Inv 1002"),
                 bank_line("BT-4", D(2026, 9, 9), 20.0, "Subscr Invented Cloud Inv 1003")]
        with self.patched(lines, transfer_je=[("BT-1",)]):
            kept, left_out = cff.bank_outflows("Test Company", AS_OF, [])
        self.assertEqual([name for _key, _day, _amount, name in kept], ["BT-2", "BT-3", "BT-4"])
        self.assertEqual(left_out, {"transfer between own bank accounts": 1})

    def test_a_transfer_by_an_internal_transfer_payment_entry_is_left_out(self):
        lines = [bank_line("BT-1", D(2026, 7, 5), 300.0, "Invented Own Account")]
        with self.patched(lines, transfer_pe=[("BT-1",)]):
            kept, left_out = cff.bank_outflows("Test Company", AS_OF, [])
        self.assertEqual(kept, [])
        self.assertEqual(left_out, {"transfer between own bank accounts": 1})

    def test_the_transfer_set_is_read_from_both_journal_entries_and_internal_transfers(self):
        with mock.patch.object(cff, "frappe", mock.Mock()) as frappe:
            frappe.db.sql.side_effect = [[("BT-1",)], [], [("BT-2",)], [("BT-3",)]]
            payroll_or_vat, bill, transfer = cff.reconciled_bank_lines(["BT-1", "BT-2", "BT-3"])
        self.assertEqual(payroll_or_vat, {"BT-1"})
        self.assertEqual(bill, set())
        self.assertEqual(transfer, {"BT-2", "BT-3"})
        # a Journal Entry is a transfer only with two bank or cash lines
        self.assertIn("having count(*) >= 2", frappe.db.sql.call_args_list[2].args[0])
        self.assertIn("Internal Transfer", frappe.db.sql.call_args_list[3].args[0])

    def test_no_lines_no_reconciliation_queries(self):
        with mock.patch.object(cff, "frappe", mock.Mock()) as frappe:
            self.assertEqual(cff.reconciled_bank_lines([]), (set(), set(), set()))
        frappe.db.sql.assert_not_called()


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
