"""Offline tests for import_payments_out.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import io
import unittest
from decimal import Decimal

import import_master as im
import import_payments_out as po
import import_purchase as ip
from test_import_master import FakeErp


def lookups(invoices=None, accounts=None, banks=None):
    return po.Lookups(
        banks=dict(banks if banks is not None else {"1": "1020 - UBS Test - bic"}),
        accounts=dict(accounts if accounts is not None else {"91": "1091 - Lohndurchlaufkonto - bic"}),
        invoices=dict(invoices if invoices is not None else {
            "b-1": {"name": "ACC-PINV-0001", "supplier": "Lieferant Test AG", "credit_to": "2000 - Kreditoren - bic",
                    "currency": "CHF", "grand_total": 402.85},
        }),
    )


def payment(pid="p-1", status="paid", amount="402.85", currency="CHF", bill_id="b-1", is_salary=False, **extra):
    return dict({
        "id": pid, "status": status, "amount": amount, "currency": currency, "execution_date": "2025-03-12",
        "document_no": "R-1", "is_salary": is_salary, "type": "iban",
        "sender": {"id": 1, "iban": "CH0000000000000000000"},
        "purchase_reference": {"bill_id": bill_id, "bill_payment_id": None},
    }, **extra)


class MapPaymentTest(unittest.TestCase):
    def test_executed_supplier_payment_is_a_payment_entry_against_the_invoice(self):
        doc = po.map_payment(payment(), lookups())
        self.assertEqual(doc["doctype"], "Payment Entry")
        self.assertEqual((doc["payment_type"], doc["party_type"], doc["party"]), ("Pay", "Supplier", "Lieferant Test AG"))
        self.assertEqual((doc["paid_from"], doc["paid_to"]), ("1020 - UBS Test - bic", "2000 - Kreditoren - bic"))
        self.assertEqual((doc["paid_amount"], doc["received_amount"]), (402.85, 402.85))
        self.assertEqual(doc["references"], [{"reference_doctype": "Purchase Invoice", "reference_name": "ACC-PINV-0001",
                                              "allocated_amount": 402.85}])
        self.assertEqual((doc["posting_date"], doc["reference_no"], doc["bexio_id"]), ("2025-03-12", "R-1", "p-1"))

    def test_only_paid_is_executed(self):
        for status in ("transmitted", "downloaded", "open", "cancelled"):
            with self.subTest(status=status), self.assertRaisesRegex(ip.Skipped, "not executed: status " + status):
                po.map_payment(payment(status=status), lookups())

    def test_paid_without_a_bill_link_is_unmapped(self):
        with self.assertRaisesRegex(ip.MappingError, "no bill link"):
            po.map_payment(payment(bill_id=None), lookups())

    def test_bill_without_a_purchase_invoice_in_erpnext_is_unmapped(self):
        with self.assertRaisesRegex(ip.MappingError, "no Purchase Invoice"):
            po.map_payment(payment(bill_id="b-9"), lookups())

    def test_amount_that_differs_from_the_invoice_is_unmapped(self):
        with self.assertRaisesRegex(ip.MappingError, "amount differs"):
            po.map_payment(payment(amount="400.00"), lookups())

    def test_foreign_currency_is_unmapped_as_the_export_has_no_rate(self):
        with self.assertRaisesRegex(ip.MappingError, "currency EUR"):
            po.map_payment(payment(currency="EUR"), lookups())

    def test_unknown_bank_is_unmapped(self):
        with self.assertRaisesRegex(ip.MappingError, "no Bank Account"):
            po.map_payment(payment(), lookups(banks={}))

    def test_salary_payment_is_a_journal_entry_on_the_salary_account(self):
        doc = po.map_payment(payment(amount="5000.00", bill_id=None, is_salary=True), lookups())
        self.assertEqual(doc["doctype"], "Journal Entry")
        self.assertEqual(doc["voucher_type"], "Bank Entry")
        self.assertEqual(doc["accounts"], [
            {"account": "1091 - Lohndurchlaufkonto - bic", "debit_in_account_currency": 5000.0, "credit_in_account_currency": 0},
            {"account": "1020 - UBS Test - bic", "debit_in_account_currency": 0, "credit_in_account_currency": 5000.0},
        ])

    def test_salary_without_the_salary_account_is_unmapped(self):
        with self.assertRaisesRegex(ip.MappingError, "Lohndurchlaufkonto"):
            po.map_payment(payment(bill_id=None, is_salary=True), lookups(accounts={}))

    def test_salary_with_a_bill_link_is_unmapped(self):
        with self.assertRaisesRegex(ip.MappingError, "salary payment with a bill link"):
            po.map_payment(payment(is_salary=True), lookups())

    def test_salary_that_is_not_executed_is_skipped_before_anything_else(self):
        with self.assertRaisesRegex(ip.Skipped, "not executed"):
            po.map_payment(payment(status="transmitted", bill_id=None, is_salary=True), lookups(accounts={}))


class DryRunTest(unittest.TestCase):
    def test_totals_per_year_status_and_currency_and_the_reasons(self):
        mapped = payment("p-1")
        transmitted = payment("p-2", status="transmitted", amount="10.00")
        unlinked = payment("p-3", bill_id=None, amount="20.00")
        late = dict(payment("p-4", amount="30.00", bill_id="b-9"), execution_date="2026-01-02")
        totals = po.dry_run([mapped, transmitted, unlinked, late], lookups())

        row = totals.rows[(2025, "paid", "CHF")]
        self.assertEqual((row["records"], row["mapped"], row["skipped"], row["unmapped"]), (2, 1, 0, 1))
        self.assertEqual((row["amount"], row["mapped_amount"]), (Decimal("422.85"), Decimal("402.85")))
        self.assertEqual(totals.rows[(2025, "transmitted", "CHF")]["skipped"], 1)
        self.assertEqual(totals.rows[(2026, "paid", "CHF")]["unmapped"], 1)
        self.assertEqual(totals.reasons[("paid, not salary, and no bill link in the export", "CHF")]["records"], 1)
        self.assertEqual(totals.reasons[("not executed: status transmitted", "CHF")]["amount"], Decimal("10.00"))
        self.assertTrue(any(p.startswith("payment p-3: unmapped, ") for p in totals.problems))
        self.assertTrue(any(p.startswith("payment p-2 (bill b-1): skipped, ") for p in totals.problems))
        self.assertTrue(any(p.startswith("payment p-4 (bill b-9): unmapped, ") for p in totals.problems))

    def test_report_prints_totals_and_the_reasons_without_bexio_ids(self):
        totals = po.dry_run([payment("p-1"), payment("p-2", status="open")], lookups())
        text = po.report(totals, "/somewhere/export")
        self.assertIn("nothing was written", text)
        self.assertIn("not executed: status open", text)
        self.assertNotIn("p-1", text)
        self.assertNotIn("p-2", text)

    def test_main_takes_the_dry_run_only(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(po.main([]), 2)


class LookupsTest(unittest.TestCase):
    def test_from_erp_keys_banks_accounts_and_invoices_by_bexio_id(self):
        erp = FakeErp({
            "Bank Account": [{"name": "UBS Test", "bexio_id": "1", "account": "1020 - UBS Test - bic", "company": im.COMPANY}],
            "Account": [{"name": "1091 - Lohndurchlaufkonto - bic", "bexio_id": "91", "company": im.COMPANY}],
            "Purchase Invoice": [{"name": "ACC-PINV-0001", "bexio_id": "b-1", "supplier": "Lieferant Test AG",
                                  "credit_to": "2000 - Kreditoren - bic", "currency": "CHF", "grand_total": 402.85,
                                  "company": im.COMPANY}],
        })
        found = po.Lookups.from_erp(erp)
        self.assertEqual(found.banks, {"1": "1020 - UBS Test - bic"})
        self.assertEqual(found.accounts, {"91": "1091 - Lohndurchlaufkonto - bic"})
        self.assertEqual(found.invoices["b-1"]["name"], "ACC-PINV-0001")


if __name__ == "__main__":
    unittest.main()
