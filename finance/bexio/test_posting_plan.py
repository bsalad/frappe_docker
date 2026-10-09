"""Offline tests for posting_plan.py: which bucket each journal line lands in, and the coverage check.

All data is invented. Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import io
import json
import os
import tempfile
import unittest

import posting_plan as pp


def line(i, date, description, debit, credit, amount, ref_class=None, ref_id=None):
    return {
        "id": i,
        "date": date + "T00:00:00+02:00",
        "description": description,
        "debit_account_id": debit,
        "credit_account_id": credit,
        "amount": amount,
        "base_currency_amount": amount,
        "ref_class": ref_class,
        "ref_id": ref_id,
    }


def entry(entry_id, kind, date, rows):
    return {
        "id": entry_id,
        "type": kind,
        "date": date,
        "entries": [dict(row, date=date) for row in rows],
    }


def row(row_id, description, debit, credit, amount):
    return {"id": row_id, "description": description, "debit_account_id": debit, "credit_account_id": credit, "amount": amount}


def sample():
    """A small export: one invoice with a payment, one bill with a payment, a bank booking and a manual entry with tax lines."""
    journal = [
        line(1, "2024-03-01", "Invoice A", 93, 154, 1000, "KbInvoice", 7),
        line(2, "2024-03-01", "Invoice A", 93, 129, 80, "KbInvoice", 7),
        line(3, "2024-03-20", "Payment A", 77, 93, 1080, "KbClientAccountEntry", 11),
        line(4, "2024-04-01", "Bill B", 237, 122, 200, "KbBill", "uuid-b"),
        line(5, "2024-05-01", "Pay B", 122, 77, 200, "KbClientAccountEntry", 99),
        line(6, "2024-06-01", "Bank booking C", 256, 77, 20),
        line(7, "2024-06-01", "Bank booking C", 96, 77, 0),
        line(8, "2024-06-05", "Office supplies", 241, 77, 50),
        line(9, "2024-06-05", "Office supplies", 96, 77, 0),
        line(10, "2024-07-02", "Credit note D", 3204, 93, 500, "KbCreditVoucher", 4),
        line(11, "2024-01-01", "Provisional balance carried forward", 77, 273, 300),
        line(12, "2024-12-31", "Salary run", 186, 91, 999),
    ]
    # A manual row's id is its journal line's id; the tax line (id 7, 9) has none in the file.
    manual = [
        entry(50, "banking_transaction", "2024-06-01", [row(6, "Bank booking C", 256, 77, 20)]),
        entry(52, "manual_single_entry", "2024-06-05", [row(8, "Office supplies", 241, 77, 50)]),
    ]
    return {"journal": journal, "manual_entries": manual, "invoice_payments": [{"parent_id": 7, "rows": [{"id": 11}]}]}


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.data = sample()
        self.buckets = pp.classify(self.data)

    def test_invoices_and_bills_go_to_their_documents(self):
        self.assertEqual(self.buckets[1], "sales_invoice")
        self.assertEqual(self.buckets[2], "sales_invoice")
        self.assertEqual(self.buckets[4], "purchase_invoice")

    def test_a_client_account_entry_is_a_payment_by_its_target(self):
        # ref_id 11 is an invoice payment (customer side); ref_id 99 is not, so it is a bill payment (supplier side).
        self.assertEqual(self.buckets[3], "sales_payment")
        self.assertEqual(self.buckets[5], "purchase_payment")

    def test_credit_voucher_is_its_own_bucket(self):
        self.assertEqual(self.buckets[10], "credit_voucher")

    def test_banking_entry_and_its_tax_line_are_one_bank_booking(self):
        self.assertEqual(self.buckets[6], "bank_booking")
        self.assertEqual(self.buckets[7], "bank_booking")

    def test_other_manual_entry_and_its_tax_line_are_manual(self):
        self.assertEqual(self.buckets[8], "manual")
        self.assertEqual(self.buckets[9], "manual")

    def test_year_start_carry_forward_is_its_own_bucket(self):
        self.assertEqual(self.buckets[11], "carry_forward")

    def test_line_with_no_export_source_is_unsourced(self):
        self.assertEqual(self.buckets[12], "unsourced")

    def test_tax_line_with_two_possible_parents_of_different_kinds_is_unsourced(self):
        # Same date and description as a banking entry and a manual entry: the plan does not guess.
        data = sample()
        data["manual_entries"].append(entry(60, "manual_single_entry", "2024-06-01", [row(61, "Bank booking C", 241, 77, 20)]))
        # Journal line 13: a tax line with the same date and description as the banking entry and this manual one.
        data["journal"].append(line(13, "2024-06-01", "Bank booking C", 96, 77, 0))
        self.assertEqual(pp.classify(data)[13], "unsourced")


class CoverageTest(unittest.TestCase):
    def test_complete_plan_passes(self):
        data = sample()
        self.assertEqual(pp.coverage(data, pp.classify(data)), [])

    def test_a_line_without_a_bucket_fails(self):
        data = sample()
        buckets = pp.classify(data)
        del buckets[3]
        problems = pp.coverage(data, buckets)
        self.assertTrue(any("no bucket" in p for p in problems))

    def test_an_unknown_bucket_fails(self):
        data = sample()
        buckets = pp.classify(data)
        buckets[1] = "made_up"
        self.assertTrue(any("unknown bucket" in p for p in pp.coverage(data, buckets)))

    def test_a_bucket_naming_no_line_fails(self):
        data = sample()
        buckets = pp.classify(data)
        buckets[999] = "manual"
        self.assertTrue(any("name no journal line" in p for p in pp.coverage(data, buckets)))

    def test_duplicate_journal_ids_fail(self):
        data = sample()
        data["journal"].append(dict(data["journal"][0]))
        self.assertTrue(any("not unique" in p for p in pp.coverage(data, pp.classify(data))))


class SummaryTest(unittest.TestCase):
    def test_counts_and_sums_per_bucket_and_year(self):
        data = sample()
        table = pp.summary(data, pp.classify(data))
        self.assertEqual(table[("sales_invoice", "2024")], (2, 1080.0))
        self.assertEqual(table[("bank_booking", "2024")], (2, 20.0))
        self.assertEqual(table[("carry_forward", "2024")], (1, 300.0))


class MainTest(unittest.TestCase):
    def test_main_reports_coverage_for_an_export_directory(self):
        data = sample()
        with tempfile.TemporaryDirectory() as folder:
            for name in ("journal", "manual_entries", "invoice_payments"):
                with open(os.path.join(folder, name + ".json"), "w", encoding="utf-8") as f:
                    json.dump(data[name], f)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = pp.main([folder])
        self.assertEqual(status, 0)
        self.assertIn("coverage: every journal line in exactly one bucket", out.getvalue())


if __name__ == "__main__":
    unittest.main()
