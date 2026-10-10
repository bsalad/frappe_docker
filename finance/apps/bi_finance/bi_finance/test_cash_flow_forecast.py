"""Offline tests of the Cash Flow Forecast report's reads of the books: which suppliers are the insurers.

Invented accounts and suppliers only; no site, no network. The database is mocked. The module imports frappe, so run
it in the image, as test_treasury.py does (finance/docs/erpnext-setup.md):

    docker run --rm -v "$PWD/finance/apps/bi_finance:/home/frappe/bi_finance_src:ro" \
        frappe-finance-custom:v16.50.0-swiss-bi6 \
        sh -c 'cd /home/frappe/bi_finance_src && ../frappe-bench/env/bin/python -m unittest -v bi_finance.test_cash_flow_forecast'
"""

import contextlib
import datetime
import sqlite3
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


class NewSalesRunRate(unittest.TestCase):
    def patch_readers(self, stack, payments):
        # every read of the books stubbed to nothing but the opening cash and the new sales receipts
        stubs = {
            "opening_cash": 1000.0, "open_documents": {}, "paid_history": {},
            "recurring_sources": {"bills": [], "bank": []}, "personnel_postings": [], "vat_balances": {},
            "vat_paid_since": 0.0, "sales_paid_in_windows": payments, "purchase_paid_in_windows": [],
            "insurer_suppliers": set(),
        }
        return {name: stack.enter_context(mock.patch.object(cff, name, return_value=value))
                for name, value in stubs.items()}

    def compute(self, payments, include):
        with contextlib.ExitStack() as stack:
            mocks = self.patch_readers(stack, payments)
            stack.enter_context(mock.patch.object(cff, "_", lambda text: text))  # no site, so no translations
            result = cff.compute("Test Company", AS_OF, include_run_rate=include)
        return result, mocks

    def payments(self):
        # 1300 collected in the most recent window from an invoice issued in it: 25 a week over the 13 weeks
        return [(AS_OF - datetime.timedelta(days=80), AS_OF - datetime.timedelta(days=20), 1300.0)]

    def test_the_run_rate_is_a_receipt_in_each_of_the_thirteen_weeks(self):
        result, _mocks = self.compute(self.payments(), include=True)
        self.assertEqual(result["run_rate"], {"weekly": 25.0, "since": AS_OF - datetime.timedelta(days=364)})
        self.assertEqual([row["new_sales"] for row in result["weeks"]], [25.0] * 13)
        self.assertEqual([row["receipt"] for row in result["weeks"]], [0.0] * 13)
        self.assertEqual(result["weeks"][-1]["closing"], 1325.0)

    def test_the_filter_off_leaves_the_run_rate_out(self):
        result, mocks = self.compute(self.payments(), include=False)
        mocks["sales_paid_in_windows"].assert_not_called()
        self.assertIsNone(result["run_rate"])
        self.assertEqual([row["new_sales"] for row in result["weeks"]], [0.0] * 13)
        self.assertEqual(result["weeks"][-1]["closing"], 1000.0)

    def execute(self, filters):
        with contextlib.ExitStack() as stack:
            self.patch_readers(stack, self.payments())
            stack.enter_context(mock.patch.object(cff, "_", lambda text: text))  # no site, so no translations
            stack.enter_context(mock.patch.object(cff.frappe, "get_cached_value", return_value="CHF"))
            stack.enter_context(mock.patch.object(cff, "fmt_money", return_value="25.00 CHF"))
            return cff.execute(filters)

    def test_the_report_includes_the_run_rate_by_default_and_says_so(self):
        _columns, rows, message, _chart, _summary = self.execute({"company": "Test Company", "as_of_date": AS_OF})
        self.assertEqual([row["new_sales"] for row in rows], [25.0] * 13)
        self.assertIn("New sales are in the forecast at 25.00 CHF a week", message)

    def test_the_report_has_the_new_purchases_column_and_a_basis_note_with_the_excluded_count_and_no_names(self):
        day = AS_OF - datetime.timedelta(days=80)
        paid = AS_OF - datetime.timedelta(days=20)
        with contextlib.ExitStack() as stack:
            self.patch_readers(stack, [])
            stack.enter_context(mock.patch.object(cff, "purchase_paid_in_windows", return_value=[
                ("Test Supplier", day, paid, 1300.0), ("Test Insurer", day, paid, 400.0)]))
            stack.enter_context(mock.patch.object(cff, "insurer_suppliers", return_value={"Test Insurer"}))
            stack.enter_context(mock.patch.object(cff, "_", lambda text: text))
            stack.enter_context(mock.patch.object(cff.frappe, "get_cached_value", return_value="CHF"))
            stack.enter_context(mock.patch.object(cff, "fmt_money", return_value="25.00 CHF"))
            columns, rows, message, _chart, _summary = cff.execute(
                {"company": "Test Company", "as_of_date": AS_OF, "include_run_rate": 0})
        self.assertIn(("New purchases", "new_purchases"), [(c["label"], c["fieldname"]) for c in columns])
        self.assertEqual([row["new_purchases"] for row in rows], [25.0] * 13)
        self.assertIn("New purchases are in the forecast at 25.00 CHF a week", message)
        self.assertIn("1 suppliers left out", message)
        self.assertNotIn("Test Insurer", message)

    def test_the_receipts_are_payment_entries_against_sales_invoices_over_the_four_windows(self):
        with mock.patch.object(cff, "frappe") as frappe:
            frappe.db.sql.return_value = [(AS_OF - datetime.timedelta(days=80), AS_OF - datetime.timedelta(days=20), 1300.0)]
            rows = cff.sales_paid_in_windows("Test Company", AS_OF)
        self.assertEqual(rows, [(AS_OF - datetime.timedelta(days=80), AS_OF - datetime.timedelta(days=20), 1300.0)])
        sql, args = frappe.db.sql.call_args.args
        self.assertIn("ple.voucher_type = 'Payment Entry'", sql)
        self.assertEqual(args, ("Test Company", AS_OF - datetime.timedelta(days=364), AS_OF))

    def test_the_report_filter_off_shows_the_documents_alone(self):
        _columns, rows, message, _chart, _summary = self.execute(
            {"company": "Test Company", "as_of_date": AS_OF, "include_run_rate": 0, "include_new_purchases": 0})
        self.assertEqual([row["new_sales"] for row in rows], [0.0] * 13)
        self.assertIsNone(message)

    def test_the_lines_report_lists_the_run_rate_as_an_inflow_and_the_filter_leaves_it_out(self):
        from bi_finance.bi_finance.report.cash_flow_forecast_lines import cash_flow_forecast_lines as lines_report

        def run(filters):
            with contextlib.ExitStack() as stack:
                self.patch_readers(stack, self.payments())
                stack.enter_context(mock.patch.object(cff, "_", lambda text: text))
                stack.enter_context(mock.patch.object(lines_report, "_", lambda text: text))
                _columns, rows = lines_report.execute(filters)
            return rows

        rows = run({"company": "Test Company", "as_of_date": AS_OF})
        receipts = [row for row in rows if row["type"] == "Expected receipts from new sales"]
        self.assertEqual([row["amount"] for row in receipts], [25.0] * 13)
        self.assertEqual(run({"company": "Test Company", "as_of_date": AS_OF, "include_run_rate": 0, "include_new_purchases": 0}), [])


class LedgerFixture(unittest.TestCase):
    """The Payment Ledger and the invoices on an in-memory SQLite copy, so the queries of the report's reads run on
    invented rows. The as-of date, the payments and the returns are netted by the query itself."""

    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "create table `tabPayment Ledger Entry` (company, against_voucher_type, against_voucher_no, voucher_type,"
            " voucher_no, party, amount, posting_date, delinked)")
        for doctype in ("Purchase Invoice", "Sales Invoice"):
            self.db.execute(f"create table `tab{doctype}` (name, due_date, posting_date, supplier)")
        self.frappe = mock.Mock()
        self.frappe.db.sql.side_effect = self.sql
        self.frappe.get_all.side_effect = self.get_all
        patch = mock.patch.object(cff, "frappe", self.frappe)
        patch.start()
        self.addCleanup(patch.stop)

    def sql(self, query, args=()):
        args = [arg.isoformat() if isinstance(arg, datetime.date) else arg for arg in args]
        return self.db.execute(query.replace("%s", "?"), args).fetchall()

    def get_all(self, doctype, filters, fields):
        names = filters["name"][1]
        rows = self.db.execute(
            f"select name, due_date, posting_date from `tab{doctype}` where name in ({','.join('?' * len(names))})",
            names).fetchall()
        return [types.SimpleNamespace(name=name, due_date=datetime.date.fromisoformat(due),
                                      posting_date=datetime.date.fromisoformat(posted)) for name, due, posted in rows]

    def invoice(self, doctype, name, due, posted, supplier="Test Supplier"):
        self.db.execute(f"insert into `tab{doctype}` values (?, ?, ?, ?)",
                        (name, due.isoformat(), posted.isoformat(), supplier))

    def entry(self, doctype, name, amount, posted, party="Test Supplier", voucher=None):
        """A Payment Ledger row against an invoice: its own row when voucher is None, else a payment or return."""
        voucher = voucher or name
        self.db.execute(
            "insert into `tabPayment Ledger Entry` values ('Test Company', ?, ?, ?, ?, ?, ?, ?, 0)",
            (doctype, name, doctype if voucher == name else "Payment Entry", voucher, party, amount, posted.isoformat()))


class NewPurchasesRunRate(LedgerFixture):
    """The new purchases run-rate on invented rows of the Payment Ledger: a bill posted in the most recent window and
    paid in it by a Payment Entry, 1300, is 25 a week; a recurring supplier's bill of 900 in the same window is left
    out when its supplier is recurring. The sign and the window bounds are the query's, read here as they are."""

    def setUp(self):
        super().setUp()
        posted, paid = AS_OF - datetime.timedelta(days=50), AS_OF - datetime.timedelta(days=20)
        self.posted, self.paid = posted, paid
        self.invoice("Purchase Invoice", "PI-WINDOW", posted, posted)
        self.entry("Purchase Invoice", "PI-WINDOW", 1300.0, posted)
        self.entry("Purchase Invoice", "PI-WINDOW", -1300.0, paid, voucher="PE-WINDOW")
        self.invoice("Purchase Invoice", "PI-RECURRING", posted, posted, supplier="Test Recurring Supplier")
        self.entry("Purchase Invoice", "PI-RECURRING", 900.0, posted, party="Test Recurring Supplier")
        self.entry("Purchase Invoice", "PI-RECURRING", -900.0, paid, party="Test Recurring Supplier", voucher="PE-RECURRING")

    def sql(self, query, args=()):
        # SQLite keeps the dates as text; MariaDB returns them as dates, which the window arithmetic compares
        return [(supplier, datetime.date.fromisoformat(issued), datetime.date.fromisoformat(paid), amount)
                for supplier, issued, paid, amount in super().sql(query, args)]

    def test_the_payments_of_the_bills_posted_in_a_window_are_read_as_positive_amounts(self):
        self.assertEqual(sorted(cff.purchase_paid_in_windows("Test Company", AS_OF)), [
            ("Test Recurring Supplier", self.posted, self.paid, 900.0),
            ("Test Supplier", self.posted, self.paid, 1300.0),
        ])

    def test_a_payment_of_a_bill_from_before_the_four_windows_is_not_counted(self):
        old = AS_OF - datetime.timedelta(days=400)
        weekly, _since, _excluded = cf.purchase_run_rate(
            [("Test Old Supplier", old, self.paid, 500.0), ("Test Supplier", self.posted, self.paid, 1300.0)], AS_OF, set())
        self.assertEqual(weekly, 25.0)

    def test_the_recurring_suppliers_and_the_insurers_are_left_out_and_counted(self):
        payments = cff.purchase_paid_in_windows("Test Company", AS_OF)
        weekly, since, excluded = cf.purchase_run_rate(payments, AS_OF, {"Test Recurring Supplier", "Test Insurer"})
        # the recurring supplier's 900 is out: 1300 in the most recent window, a quarter of it a week
        self.assertEqual((weekly, since, excluded), (25.0, AS_OF - datetime.timedelta(days=364), 1))
        weekly, _since, excluded = cf.purchase_run_rate(payments, AS_OF, {"Test Supplier", "Test Recurring Supplier"})
        self.assertEqual((weekly, excluded), (0.0, 2))

    def compute(self, include, recurring=(), insurers=()):
        bills = [{"supplier": supplier, "amount": 900.0, "period": "monthly", "count": 3, "last_bill": "PI-HIST"}
                 for supplier in recurring]
        stubs = {
            "opening_cash": 1000.0, "open_documents": {}, "paid_history": {}, "sales_paid_in_windows": [],
            "recurring_sources": {"bills": bills, "bank": []}, "personnel_postings": [], "vat_balances": {},
            "vat_paid_since": 0.0, "insurer_suppliers": set(insurers),
        }
        with contextlib.ExitStack() as stack:
            for name, value in stubs.items():
                stack.enter_context(mock.patch.object(cff, name, return_value=value))
            stack.enter_context(mock.patch.object(cf, "recurring_dates", return_value=[]))  # the lines of the other sources
            stack.enter_context(mock.patch.object(cff, "_", lambda text: text))
            return cff.compute("Test Company", AS_OF, include_run_rate=False, include_new_purchases=include)

    def test_each_week_carries_the_run_rate_and_the_closing_cash_pays_it(self):
        result = self.compute(include=True, recurring={"Test Recurring Supplier"})
        self.assertEqual(result["new_purchases"], {"weekly": 25.0, "since": AS_OF - datetime.timedelta(days=364), "excluded": 1})
        self.assertEqual([row["new_purchases"] for row in result["weeks"]], [25.0] * 13)
        self.assertEqual([row["bill"] for row in result["weeks"]], [0.0] * 13)
        self.assertEqual(result["weeks"][-1]["closing"], round(1000.0 - 25.0 * 13, 2))

    def test_an_insurer_bill_is_left_out_of_the_run_rate(self):
        # the insurer's 1300 is out; the recurring supplier's 900 is not recurring here, so it stays: 900 over 52 weeks
        result = self.compute(include=True, insurers={"Test Supplier"})
        self.assertEqual((result["new_purchases"]["weekly"], result["new_purchases"]["excluded"]), (17.31, 1))

    def test_the_filter_off_leaves_the_line_out_and_does_not_read_the_payments(self):
        with mock.patch.object(cff, "purchase_paid_in_windows") as paid:
            result = self.compute(include=False)
        paid.assert_not_called()
        self.assertIsNone(result["new_purchases"])
        self.assertEqual([row["new_purchases"] for row in result["weeks"]], [0.0] * 13)
        self.assertEqual(result["weeks"][-1]["closing"], 1000.0)

    def test_the_lines_report_lists_the_run_rate_as_an_outflow_and_the_filter_leaves_it_out(self):
        from bi_finance.bi_finance.report.cash_flow_forecast_lines import cash_flow_forecast_lines as lines_report

        def run(filters):
            with contextlib.ExitStack() as stack:
                stubs = {"opening_cash": 1000.0, "open_documents": {}, "paid_history": {}, "sales_paid_in_windows": [],
                         "recurring_sources": {"bills": [], "bank": []}, "personnel_postings": [], "vat_balances": {},
                         "vat_paid_since": 0.0, "insurer_suppliers": set()}
                for name, value in stubs.items():
                    stack.enter_context(mock.patch.object(cff, name, return_value=value))
                stack.enter_context(mock.patch.object(cff, "_", lambda text: text))
                stack.enter_context(mock.patch.object(lines_report, "_", lambda text: text))
                _columns, rows = lines_report.execute(filters)
            return rows

        rows = run({"company": "Test Company", "as_of_date": AS_OF})
        purchases = [row for row in rows if row["type"] == "Expected payments for new purchase bills"]
        # both payments of the window, 1300 and 900, a thirteenth of the mean of four windows each week: an outflow
        self.assertEqual([row["amount"] for row in purchases], [-round(2200 / (cf.WEEKS * cf.RUN_RATE_WINDOWS), 2)] * 13)
        self.assertEqual([row for row in run({"company": "Test Company", "as_of_date": AS_OF, "include_new_purchases": 0})
                          if row["type"] == "Expected payments for new purchase bills"], [])


class OpenDocuments(LedgerFixture):
    """open_documents' reads of the Payment Ledger: the same sign rule for both doctypes. Invented names and amounts."""

    def open(self, doctype):
        return cff.open_documents("Test Company", AS_OF, doctype)

    def test_a_bill_open_on_the_as_of_date_is_owed_at_its_due_date(self):
        self.invoice("Purchase Invoice", "PI-OPEN", D(2026, 10, 20), D(2026, 9, 1))
        self.entry("Purchase Invoice", "PI-OPEN", 100.0, D(2026, 9, 1))
        self.assertEqual(self.open("Purchase Invoice"), {"PI-OPEN": ("Test Supplier", 100.0, D(2026, 10, 20))})

    def test_a_bill_paid_before_the_as_of_date_is_not_open(self):
        self.invoice("Purchase Invoice", "PI-PAID", D(2026, 9, 20), D(2026, 9, 1))
        self.entry("Purchase Invoice", "PI-PAID", 50.0, D(2026, 9, 1))
        self.entry("Purchase Invoice", "PI-PAID", -50.0, D(2026, 9, 15), voucher="PE-PAID")
        self.assertEqual(self.open("Purchase Invoice"), {})

    def test_a_part_payment_leaves_the_rest_owed(self):
        self.invoice("Purchase Invoice", "PI-PART", D(2026, 10, 20), D(2026, 9, 1))
        self.entry("Purchase Invoice", "PI-PART", 100.0, D(2026, 9, 1))
        self.entry("Purchase Invoice", "PI-PART", -30.0, D(2026, 9, 15), voucher="PE-PART")
        self.assertEqual(self.open("Purchase Invoice"), {"PI-PART": ("Test Supplier", 70.0, D(2026, 10, 20))})

    def test_a_bill_posted_after_the_as_of_date_is_not_open(self):
        self.invoice("Purchase Invoice", "PI-LATE", D(2026, 11, 1), D(2026, 10, 12))
        self.entry("Purchase Invoice", "PI-LATE", 80.0, D(2026, 10, 12))
        self.assertEqual(self.open("Purchase Invoice"), {})

    def test_a_return_nets_against_its_bill(self):
        self.invoice("Purchase Invoice", "PI-RET", D(2026, 10, 20), D(2026, 9, 1))
        self.entry("Purchase Invoice", "PI-RET", 200.0, D(2026, 9, 1))
        self.entry("Purchase Invoice", "PI-RET", -50.0, D(2026, 9, 10), voucher="PI-RET-R")
        self.assertEqual(self.open("Purchase Invoice"), {"PI-RET": ("Test Supplier", 150.0, D(2026, 10, 20))})

    def test_a_fully_returned_bill_is_not_open(self):
        self.invoice("Purchase Invoice", "PI-CREDITED", D(2026, 10, 20), D(2026, 9, 1))
        self.entry("Purchase Invoice", "PI-CREDITED", 60.0, D(2026, 9, 1))
        self.entry("Purchase Invoice", "PI-CREDITED", -60.0, D(2026, 9, 10), voucher="PI-CREDITED-R")
        self.assertEqual(self.open("Purchase Invoice"), {})

    def test_a_sales_invoice_is_owed_as_before(self):
        self.invoice("Sales Invoice", "SI-OPEN", D(2026, 10, 5), D(2026, 9, 1))
        self.entry("Sales Invoice", "SI-OPEN", 300.0, D(2026, 9, 1), party="Test Customer")
        self.entry("Sales Invoice", "SI-OPEN", -100.0, D(2026, 9, 20), party="Test Customer", voucher="PE-SI")
        self.assertEqual(self.open("Sales Invoice"), {"SI-OPEN": ("Test Customer", 200.0, D(2026, 10, 5))})

    def test_an_open_bill_reaches_the_forecast_as_a_bill_line_on_its_due_date(self):
        self.invoice("Purchase Invoice", "PI-LINE", D(2026, 10, 20), D(2026, 9, 1))
        self.entry("Purchase Invoice", "PI-LINE", 100.0, D(2026, 9, 1))
        result = self.compute(recurring=[])
        bills = [line for line in result["lines"] if line["kind"] == "bill"]
        self.assertEqual([(line["day"], line["amount"], line["name"]) for line in bills], [(D(2026, 10, 20), 100.0, "PI-LINE")])

    def test_a_recurring_line_within_fifteen_days_of_an_open_bill_of_the_supplier_is_dropped(self):
        self.invoice("Purchase Invoice", "PI-RENT", D(2026, 10, 25), D(2026, 9, 25))
        self.entry("Purchase Invoice", "PI-RENT", 100.0, D(2026, 9, 25))
        near, far = D(2026, 10, 30), D(2026, 12, 1)  # 5 days after the open bill, and 37 days after it
        result = self.compute(recurring=[near, far])
        recurring = [line["day"] for line in result["lines"] if line["kind"] == "recurring"]
        self.assertEqual(recurring, [far])

    def compute(self, recurring):
        item = {"supplier": "Test Supplier", "amount": 40.0, "period": "monthly", "count": 3, "last_bill": "PI-HIST"}
        stubs = {
            "opening_cash": 1000.0, "paid_history": {}, "sales_paid_in_windows": [],
            "recurring_sources": {"bills": [item], "bank": []}, "personnel_postings": [], "vat_balances": {},
            "vat_paid_since": 0.0,
        }
        with contextlib.ExitStack() as stack:
            for name, value in stubs.items():
                stack.enter_context(mock.patch.object(cff, name, return_value=value))
            stack.enter_context(mock.patch.object(cf, "recurring_dates", return_value=recurring))
            stack.enter_context(mock.patch.object(cff, "_", lambda text: text))
            return cff.compute("Test Company", AS_OF, include_run_rate=False, include_new_purchases=False)


if __name__ == "__main__":
    unittest.main()
