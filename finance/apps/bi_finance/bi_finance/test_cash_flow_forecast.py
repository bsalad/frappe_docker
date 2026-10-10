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
    def patched(self, lines, transfer_je=(), transfer_pe=(), journal=()):
        frappe = mock.Mock()
        frappe.get_all.side_effect = [["Test Bank - TC"], lines]
        # the five reconciliation queries in order: payroll or VAT, bill payment, transfer by Journal Entry, transfer by
        # Payment Entry, any Journal Entry
        frappe.db.sql.side_effect = [[], [], list(transfer_je), list(transfer_pe), [(name,) for name in journal]]
        return mock.patch.object(cff, "frappe", frappe)

    def test_a_monthly_transfer_to_an_own_account_is_not_a_recurring_cost(self):
        # the money moves between two company accounts, both in the opening cash: the outflow is no cost
        lines = [bank_line("BT-1", D(2026, 7, 5), 500.0, "Transfer Invented Savings"),
                 bank_line("BT-2", D(2026, 8, 5), 500.0, "Transfer Invented Savings"),
                 bank_line("BT-3", D(2026, 9, 5), 500.0, "Transfer Invented Savings")]
        with self.patched(lines, transfer_je=[("BT-1",), ("BT-2",), ("BT-3",)]):
            kept, left_out, _journal = cff.bank_outflows("Test Company", AS_OF, [])
        self.assertEqual(kept, [])
        self.assertEqual(left_out["transfer between own bank accounts"], 3)
        self.assertEqual(cf.recurring_costs(kept, AS_OF, strict=True), [])

    def test_a_cost_next_to_a_transfer_stays_a_recurring_cost(self):
        lines = [bank_line("BT-1", D(2026, 7, 5), 500.0, "Transfer Invented Savings"),
                 bank_line("BT-2", D(2026, 7, 9), 20.0, "Subscr Invented Cloud Inv 1001"),
                 bank_line("BT-3", D(2026, 8, 9), 20.0, "Subscr Invented Cloud Inv 1002"),
                 bank_line("BT-4", D(2026, 9, 9), 20.0, "Subscr Invented Cloud Inv 1003")]
        with self.patched(lines, transfer_je=[("BT-1",)]):
            kept, left_out, _journal = cff.bank_outflows("Test Company", AS_OF, [])
        self.assertEqual([name for _key, _day, _amount, name in kept], ["BT-2", "BT-3", "BT-4"])
        self.assertEqual(left_out, {"transfer between own bank accounts": 1})

    def test_a_transfer_by_an_internal_transfer_payment_entry_is_left_out(self):
        lines = [bank_line("BT-1", D(2026, 7, 5), 300.0, "Invented Own Account")]
        with self.patched(lines, transfer_pe=[("BT-1",)]):
            kept, left_out, _journal = cff.bank_outflows("Test Company", AS_OF, [])
        self.assertEqual(kept, [])
        self.assertEqual(left_out, {"transfer between own bank accounts": 1})

    def test_the_transfer_set_is_read_from_both_journal_entries_and_internal_transfers(self):
        with mock.patch.object(cff, "frappe", mock.Mock()) as frappe:
            frappe.db.sql.side_effect = [[("BT-1",)], [], [("BT-2",)], [("BT-3",)], [("BT-1",), ("BT-4",)]]
            payroll_or_vat, bill, transfer, journal = cff.reconciled_bank_lines(["BT-1", "BT-2", "BT-3", "BT-4"])
        self.assertEqual(payroll_or_vat, {"BT-1"})
        self.assertEqual(bill, set())
        self.assertEqual(transfer, {"BT-2", "BT-3"})
        self.assertEqual(journal, {"BT-1", "BT-4"})
        # a Journal Entry is a transfer only with two bank or cash lines
        self.assertIn("having count(*) >= 2", frappe.db.sql.call_args_list[2].args[0])
        self.assertIn("Internal Transfer", frappe.db.sql.call_args_list[3].args[0])
        self.assertIn("payment_document = 'Journal Entry'", frappe.db.sql.call_args_list[4].args[0])

    def test_no_lines_no_reconciliation_queries(self):
        with mock.patch.object(cff, "frappe", mock.Mock()) as frappe:
            self.assertEqual(cff.reconciled_bank_lines([]), (set(), set(), set(), set()))
        frappe.db.sql.assert_not_called()

    def test_a_bank_line_a_journal_entry_reconciles_to_stays_in_its_group_and_its_median(self):
        # the group is grouped as before: the line a Journal Entry reconciles to is in it, so its median is unchanged
        lines = [bank_line("BT-1", D(2026, 7, 5), 40.0, "Subscr Invented Card 1001"),
                 bank_line("BT-2", D(2026, 8, 5), 40.0, "Subscr Invented Card 1002"),
                 bank_line("BT-3", D(2026, 9, 5), 40.0, "Subscr Invented Card 1003")]
        with self.patched(lines, journal=["BT-2"]):
            kept, left_out, journal = cff.bank_outflows("Test Company", AS_OF, [])
        self.assertEqual([name for _key, _day, _amount, name in kept], ["BT-1", "BT-2", "BT-3"])
        self.assertEqual(journal, {"BT-2"})
        self.assertEqual(left_out, {})
        item = cf.recurring_costs(kept, AS_OF, strict=True)[0]
        self.assertEqual((item["count"], item["amount"], item["period"]), (3, 40.0, "monthly"))


class CountedOnce(unittest.TestCase):
    def line(self, key, name, day=D(2026, 7, 5)):
        return (key, day, 40.0, name)

    def test_a_group_every_line_of_which_a_journal_entry_reconciles_to_is_counted_by_the_contra_source(self):
        bank = [self.line("subscr invented card", "BT-1"), self.line("subscr invented card", "BT-2"),
                self.line("invented telco", "BT-3")]
        self.assertEqual(cf.counted_by_contra(bank, {"BT-1", "BT-2"}), {"subscr invented card"})

    def test_a_group_with_a_line_no_journal_entry_reconciles_to_is_kept(self):
        bank = [self.line("subscr invented card", "BT-1"), self.line("subscr invented card", "BT-2")]
        self.assertEqual(cf.counted_by_contra(bank, {"BT-1"}), set())

    def test_nothing_reconciled_counts_nothing(self):
        self.assertEqual(cf.counted_by_contra([self.line("subscr invented card", "BT-1")], set()), set())

    def test_a_journal_entry_paid_to_a_bill_is_not_a_recurring_cost(self):
        # the entry is reconciled against a purchase invoice: the bill carries it
        with mock.patch.object(cff, "frappe", mock.Mock()) as frappe:
            frappe.db.sql.side_effect = [[("JE-1",)]]
            billed = cff.billed_journal_entries("Test Company", ["JE-1", "JE-2"])
        self.assertEqual(billed, {"JE-1"})
        self.assertIn("against_voucher_type = 'Purchase Invoice'", frappe.db.sql.call_args.args[0])

    def test_no_journal_entries_no_billed_query(self):
        with mock.patch.object(cff, "frappe", mock.Mock()) as frappe:
            self.assertEqual(cff.billed_journal_entries("Test Company", []), set())
        frappe.db.sql.assert_not_called()


def journal_row(number, day, debit, entry, account_type="Expense", name=None):
    # (account number, account type, account name, posting date, debit, journal entry), as journal_outflows reads it
    return (number, account_type, name or "Invented Account " + number, day, float(debit), entry)


class JournalSources(unittest.TestCase):
    def contra_read(self, rows, billed=()):
        # journal_outflows, then billed_journal_entries: the two reads of the books, in that order
        with mock.patch.object(cff, "frappe", mock.Mock()) as frappe:
            frappe.db.sql.side_effect = [rows, [(entry,) for entry in billed]]
            return cff.contra_sources("Test Company", AS_OF)

    def contra_sources(self, rows, billed=()):
        items, left_out, _not_modelled = self.contra_read(rows, billed)
        return items, left_out

    def test_an_irregular_account_gets_no_line_and_is_counted_as_not_modelled(self):
        # entries on one invented account at uneven dates: no regular series, so no line, and the count is reported
        rows = [journal_row("6570", D(2026, month, day), 200.0, "JE-6570-%d-%d" % (month, day))
                for month, day in ((1, 3), (1, 20), (4, 2), (9, 28))]
        items, left_out, not_modelled = self.contra_read(rows)
        self.assertEqual(items, [])
        self.assertEqual(left_out, {})
        self.assertEqual(dict(not_modelled), {"6570": 4})

    def test_a_regular_account_is_modelled_and_not_counted_as_not_modelled(self):
        items, _left_out, not_modelled = self.contra_read(self.monthly("6940", 15.0))
        self.assertEqual(len(items), 1)
        self.assertEqual(not_modelled, {})

    def monthly(self, number, amount, account_type="Expense", name=None, months=(1, 2, 3, 4, 5, 6, 7, 8, 9)):
        return [journal_row(number, D(2026, month, 8), amount, "JE-%s-%d" % (number, month), account_type, name)
                for month in months]

    def test_a_monthly_card_settlement_is_a_recurring_cost_labelled_by_its_account(self):
        rows = self.monthly("2010", 800.0, name="2010 Invented Card Settlement - TC")
        items, left_out = self.contra_sources(rows)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["supplier"], "2010")
        self.assertEqual(items[0]["period"], "monthly")
        self.assertEqual(items[0]["amount"], 800.0)
        self.assertEqual(items[0]["count"], 9)
        self.assertEqual(items[0]["label"], "2010 Invented Card Settlement - TC")
        self.assertEqual(left_out, {})

    def test_a_payroll_clearing_is_left_out(self):
        items, left_out = self.contra_sources(self.monthly("1091", 900.0))
        self.assertEqual(items, [])
        self.assertEqual(left_out, {"carried by another line": 9})

    def test_a_salary_entry_and_an_insurer_entry_are_left_out(self):
        rows = self.monthly("5820", 300.0) + self.monthly("2271", 250.0)
        items, left_out = self.contra_sources(rows)
        self.assertEqual(items, [])
        self.assertEqual(left_out, {"carried by another line": 18})

    def test_a_vat_settlement_is_left_out(self):
        rows = self.monthly("1172", 650.0) + self.monthly("2200", 120.0)
        items, left_out = self.contra_sources(rows)
        self.assertEqual(items, [])
        self.assertEqual(left_out, {"carried by another line": 18})

    def test_a_transfer_to_a_bank_account_is_left_out(self):
        rows = self.monthly("1020", 500.0, account_type="Bank")
        items, left_out = self.contra_sources(rows)
        self.assertEqual(items, [])
        self.assertEqual(left_out, {"transfer between own bank accounts": 9})

    def test_an_entry_reconciled_to_a_purchase_invoice_is_left_out(self):
        rows = self.monthly("6570", 200.0)
        items, left_out = self.contra_sources(rows, billed=["JE-6570-%d" % month for month in range(1, 10)])
        self.assertEqual(items, [])
        self.assertEqual(left_out, {"reconciled to a purchase invoice": 9})

    def test_two_entries_to_one_account_on_one_day_with_one_amount_count_once(self):
        rows = self.monthly("6940", 15.0, months=(7, 8, 9)) + [journal_row("6940", D(2026, 9, 8), 15.0, "JE-dup")]
        items, _left_out = self.contra_sources(rows)
        self.assertEqual(items[0]["count"], 3)

    def test_the_contra_source_has_its_own_query_for_bank_credits(self):
        with mock.patch.object(cff, "frappe", mock.Mock()) as frappe:
            frappe.db.sql.side_effect = [[], []]
            cff.journal_outflows("Test Company", AS_OF)
        self.assertIn("bank.credit > 0", frappe.db.sql.call_args.args[0])
        self.assertIn("'Bank', 'Cash'", frappe.db.sql.call_args.args[0])


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


def money(value, currency=None):
    """fmt_money without a site: the amount to two places, in the currency the test reads."""
    return "%.2f CHF" % value


class NewSalesRunRate(unittest.TestCase):
    def patch_readers(self, stack, payments):
        # every read of the books stubbed to nothing but the opening cash and the new sales receipts
        stubs = {
            "opening_cash": 1000.0, "open_documents": {}, "paid_history": {},
            "recurring_sources": {"bills": [], "bank": [], "contra": [], "contra_not_modelled": {}},
            "personnel_postings": [], "vat_balances": {},
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
        # 1300 collected in the most recent window from an invoice issued in it: the mean of the two windows of mean2
        # is 650, 50 a week over the 13 weeks
        return [(AS_OF - datetime.timedelta(days=80), AS_OF - datetime.timedelta(days=20), 1300.0)]

    def test_the_run_rate_is_a_receipt_in_each_of_the_thirteen_weeks(self):
        result, _mocks = self.compute(self.payments(), include=True)
        self.assertEqual(result["run_rate"], {"weekly": 50.0, "low": 0.0, "high": 100.0,
                                              "since": AS_OF - datetime.timedelta(days=182), "basis": "mean2"})
        self.assertEqual([row["new_sales"] for row in result["weeks"]], [50.0] * 13)
        self.assertEqual([row["receipt"] for row in result["weeks"]], [0.0] * 13)
        self.assertEqual(result["weeks"][-1]["closing"], 1650.0)
        # at the low of the basis the sales come to nothing (0 a week), at the high to 100 a week: 1650 less and more by 650
        self.assertEqual(result["closing_band"], (1000.0, 2300.0))

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
            stack.enter_context(mock.patch.object(cff, "fmt_money", side_effect=money))
            return cff.execute(filters)

    def test_the_report_includes_the_run_rate_by_default_and_says_so(self):
        _columns, rows, message, _chart, _summary = self.execute({"company": "Test Company", "as_of_date": AS_OF})
        self.assertEqual([row["new_sales"] for row in rows], [50.0] * 13)
        self.assertIn("New sales are in the forecast at 50.00 CHF a week (0.00 CHF to 100.00 CHF): "
                      "the mean of the last two 13-week windows", message)
        self.assertIn("End of week 13 the closing cash is 1650.00 CHF, between 1000.00 CHF and 2300.00 CHF", message)

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
            stack.enter_context(mock.patch.object(cff, "fmt_money", side_effect=money))
            columns, rows, message, _chart, _summary = cff.execute(
                {"company": "Test Company", "as_of_date": AS_OF, "include_run_rate": 0})
        self.assertIn(("New purchases", "new_purchases"), [(c["label"], c["fieldname"]) for c in columns])
        self.assertEqual([row["new_purchases"] for row in rows], [50.0] * 13)
        self.assertIn("New purchases are in the forecast at 50.00 CHF a week (0.00 CHF to 100.00 CHF)", message)
        self.assertIn("1 suppliers left out", message)
        self.assertNotIn("Test Insurer", message)

    def test_the_receipts_are_payment_entries_against_sales_invoices_over_the_longest_basis(self):
        with mock.patch.object(cff, "frappe") as frappe:
            frappe.db.sql.return_value = [(AS_OF - datetime.timedelta(days=80), AS_OF - datetime.timedelta(days=20), 1300.0)]
            rows = cff.sales_paid_in_windows("Test Company", AS_OF)
        self.assertEqual(rows, [(AS_OF - datetime.timedelta(days=80), AS_OF - datetime.timedelta(days=20), 1300.0)])
        sql, args = frappe.db.sql.call_args.args
        self.assertIn("ple.voucher_type = 'Payment Entry'", sql)
        # the seasonal basis reads the window a year back, so the payments are read from as far back as that window
        self.assertEqual(args, ("Test Company", AS_OF - datetime.timedelta(days=cf.RUN_RATE_LOOKBACK_DAYS), AS_OF))

    def test_the_report_filter_off_shows_the_documents_alone(self):
        _columns, rows, message, _chart, _summary = self.execute(
            {"company": "Test Company", "as_of_date": AS_OF, "include_run_rate": 0, "include_new_purchases": 0})
        self.assertEqual([row["new_sales"] for row in rows], [0.0] * 13)
        self.assertNotIn("New sales are in the forecast", message)

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
        self.assertEqual([row["amount"] for row in receipts], [50.0] * 13)
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
        rate = cf.purchase_run_rate(
            [("Test Old Supplier", old, self.paid, 500.0), ("Test Supplier", self.posted, self.paid, 1300.0)], AS_OF, set())
        self.assertEqual(rate["weekly"], 50.0)

    def test_the_recurring_suppliers_and_the_insurers_are_left_out_and_counted(self):
        payments = cff.purchase_paid_in_windows("Test Company", AS_OF)
        rate = cf.purchase_run_rate(payments, AS_OF, {"Test Recurring Supplier", "Test Insurer"})
        # the recurring supplier's 900 is out: 1300 in the most recent window, the mean of two windows is 650, 50 a week
        self.assertEqual((rate["weekly"], rate["since"], rate["excluded"]), (50.0, AS_OF - datetime.timedelta(days=182), 1))
        rate = cf.purchase_run_rate(payments, AS_OF, {"Test Supplier", "Test Recurring Supplier"})
        self.assertEqual((rate["weekly"], rate["excluded"]), (0.0, 2))

    def compute(self, include, recurring=(), insurers=()):
        bills = [{"supplier": supplier, "amount": 900.0, "period": "monthly", "count": 3, "last_bill": "PI-HIST"}
                 for supplier in recurring]
        stubs = {
            "opening_cash": 1000.0, "open_documents": {}, "paid_history": {}, "sales_paid_in_windows": [],
            "recurring_sources": {"bills": bills, "bank": [], "contra": [], "contra_not_modelled": {}},
            "personnel_postings": [], "vat_balances": {},
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
        self.assertEqual(result["new_purchases"], {"weekly": 50.0, "low": 0.0, "high": 100.0,
                                                   "since": AS_OF - datetime.timedelta(days=182), "basis": "mean2", "excluded": 1})
        self.assertEqual([row["new_purchases"] for row in result["weeks"]], [50.0] * 13)
        self.assertEqual([row["bill"] for row in result["weeks"]], [0.0] * 13)
        self.assertEqual(result["weeks"][-1]["closing"], round(1000.0 - 50.0 * 13, 2))

    def test_an_insurer_bill_is_left_out_of_the_run_rate(self):
        # the insurer's 1300 is out; the recurring supplier's 900 is not recurring here, so it stays: 900 over two windows
        result = self.compute(include=True, insurers={"Test Supplier"})
        self.assertEqual((result["new_purchases"]["weekly"], result["new_purchases"]["excluded"]), (34.62, 1))

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
                         "recurring_sources": {"bills": [], "bank": [], "contra": [], "contra_not_modelled": {}},
                         "personnel_postings": [], "vat_balances": {},
                         "vat_paid_since": 0.0, "insurer_suppliers": set()}
                for name, value in stubs.items():
                    stack.enter_context(mock.patch.object(cff, name, return_value=value))
                stack.enter_context(mock.patch.object(cff, "_", lambda text: text))
                stack.enter_context(mock.patch.object(lines_report, "_", lambda text: text))
                _columns, rows = lines_report.execute(filters)
            return rows

        rows = run({"company": "Test Company", "as_of_date": AS_OF})
        purchases = [row for row in rows if row["type"] == "Expected payments for new purchase bills"]
        # both payments of the window, 1300 and 900, a thirteenth of the mean of the two windows each week: an outflow
        self.assertEqual([row["amount"] for row in purchases], [-round(2200 / 2 / cf.WEEKS, 2)] * 13)
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
            "recurring_sources": {"bills": [item], "bank": [], "contra": [], "contra_not_modelled": {}},
            "personnel_postings": [], "vat_balances": {},
            "vat_paid_since": 0.0,
        }
        with contextlib.ExitStack() as stack:
            for name, value in stubs.items():
                stack.enter_context(mock.patch.object(cff, name, return_value=value))
            stack.enter_context(mock.patch.object(cf, "recurring_dates", return_value=recurring))
            stack.enter_context(mock.patch.object(cff, "_", lambda text: text))
            return cff.compute("Test Company", AS_OF, include_run_rate=False, include_new_purchases=False)


class OwnerAccounts(unittest.TestCase):
    def compute(self, include):
        # a card settlement and an owner's current account, each a monthly journal entry series, in the contra source
        def contra(number, label, amount):
            return {"supplier": number, "period": "monthly", "amount": amount, "count": 9,
                    "last_date": D(2026, 9, 5), "last_bill": "JE-" + number, "label": label}

        sources = {"bills": [], "bank": [], "contra_not_modelled": {}, "contra": [
            contra("2010", "2010 Invented Card Settlement - TC", 800.0),
            contra("2100", "2100 Invented Owner Account - TC", 500.0)]}
        stubs = {
            "opening_cash": 1000.0, "open_documents": {}, "paid_history": {}, "recurring_sources": sources,
            "personnel_postings": [], "vat_balances": {}, "vat_paid_since": 0.0, "sales_paid_in_windows": [],
        }
        with contextlib.ExitStack() as stack:
            for name, value in stubs.items():
                stack.enter_context(mock.patch.object(cff, name, return_value=value))
            stack.enter_context(mock.patch.object(cff, "_", lambda text: text))  # no site, so no translations
            return cff.compute("Test Company", AS_OF, include_run_rate=False, include_new_purchases=False,
                               include_owner_accounts=include)

    def recurring(self, result):
        return sorted({(line["party"], line["amount"]) for line in result["lines"] if line["kind"] == "recurring"})

    def test_an_owner_account_is_labelled_as_average_and_discretionary(self):
        result = self.compute(include=True)
        self.assertEqual(self.recurring(result), [
            ("2010 Invented Card Settlement - TC", 800.0),
            ("2100 Invented Owner Account - TC (average, discretionary)", 500.0)])
        owner = [line for line in result["lines"] if line["party"].startswith("2100")]
        self.assertEqual(owner[0]["doctype"], "Journal Entry")
        self.assertEqual(owner[0]["name"], "JE-2100")

    def test_the_filter_off_leaves_the_owner_account_out_and_keeps_the_others(self):
        result = self.compute(include=False)
        self.assertEqual(self.recurring(result), [("2010 Invented Card Settlement - TC", 800.0)])

    def test_the_contra_source_lines_are_monthly_from_the_last_entry(self):
        dates = sorted(line["day"] for line in self.compute(include=True)["lines"]
                       if line["party"] == "2010 Invented Card Settlement - TC")
        self.assertEqual(dates, [D(2026, 11, 5), D(2026, 12, 5), D(2027, 1, 5)])


if __name__ == "__main__":
    unittest.main()
