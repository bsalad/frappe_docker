"""Offline tests for import_credit_note.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import copy
import unittest
from decimal import Decimal

import import_credit_note as icn
import import_sales as isl

# the lookups the mapping reads from ERPNext; the ids and names are invented
LOOKUPS = {
    "currency": {"1": "CHF", "2": "EUR"},
    "customer": {"104": "Beispiel Kunde AG"},
    "account": {"287": "3200 - Honorare - bic", "129": "2202 - Abrechnungskonto MWST - bic", "93": "1100 - Debitoren - bic"},
    "vat": "2202 - Abrechnungskonto MWST - bic",
    "invoice": {"106": "RE-0106"},
}

INVOICE = {
    "id": 106, "contact_id": 104, "currency_id": 1, "total": "1081.000000",
    "total_net": "1000.000000", "total_taxes": "81.000000", "total_credit_vouchers": "1081.000000",
    "positions": [{"type": "KbPositionCustom", "account_id": 287, "tax_id": 28, "amount": "1.000000",
                   "unit_price": "1000.000000"}],
}

# two lines of voucher 4: the net against the receivables (credit 93) and the VAT against them
JOURNAL = [
    {"id": 6168, "date": "2026-06-09T00:00:00+02:00", "amount": 1000, "debit_account_id": 287,
     "credit_account_id": 93, "ref_class": "KbCreditVoucher", "ref_id": 4, "description": "Testleistung"},
    {"id": 6169, "date": "2026-06-09T00:00:00+02:00", "amount": 81, "debit_account_id": 129,
     "credit_account_id": 93, "ref_class": "KbCreditVoucher", "ref_id": 4, "description": "Testleistung"},
    {"id": 1, "date": "2026-06-01T00:00:00+02:00", "amount": 5, "debit_account_id": 287,
     "credit_account_id": 93, "ref_class": "KbInvoice", "ref_id": 106, "description": "unrelated"},
]

PAYMENTS = [
    {"parent_id": 106, "rows": [
        {"id": 900, "date": "2026-06-09", "kb_invoice_id": 106, "kb_credit_voucher_id": 4,
         "kb_credit_voucher_text": "Gutschrift CN-TEST-0004", "bank_account_id": None, "value": "1081.000000"},
    ]},
]


def copied(base, **changes):
    value = copy.deepcopy(base)
    value.update(changes)
    return value


def lookups(**changes):
    return copied(LOOKUPS, **changes)


class CreditNoteDocument(unittest.TestCase):
    def setUp(self):
        self.doc, self.number, self.totals = icn.credit_note(JOURNAL, PAYMENTS, [INVOICE], lookups())

    def test_return_against_the_invoice_it_credits(self):
        self.assertEqual(self.doc["doctype"], "Sales Invoice")
        self.assertEqual(self.doc["is_return"], 1)
        self.assertEqual(self.doc["return_against"], "RE-0106")
        self.assertEqual(self.doc["customer"], "Beispiel Kunde AG")
        self.assertEqual(self.doc["currency"], "CHF")

    def test_keyed_by_the_voucher_and_numbered_by_bexio(self):
        self.assertEqual(self.doc["bexio_id"], "credit-4")
        self.assertEqual(self.number, "CN-TEST-0004")
        self.assertEqual(self.doc["remarks"], "bexio Gutschrift 4 zu Rechnung 106")

    def test_dated_on_the_voucher_and_keeps_the_outstanding_on_the_invoice(self):
        self.assertEqual(self.doc["posting_date"], "2026-06-09")
        self.assertEqual(self.doc["set_posting_time"], 1)
        self.assertEqual(self.doc["update_outstanding_for_self"], 0)

    def test_item_is_the_revenue_line_at_minus_one(self):
        self.assertEqual(len(self.doc["items"]), 1)
        item = self.doc["items"][0]
        self.assertEqual(item["item_code"], isl.GENERIC_ITEM)
        self.assertEqual(item["qty"], -1.0)
        self.assertEqual(item["rate"], 1000.0)
        self.assertEqual(item["income_account"], "3200 - Honorare - bic")

    def test_one_actual_tax_row_on_the_vat_account_at_minus_the_vat_line(self):
        self.assertEqual(len(self.doc["taxes"]), 1)
        tax = self.doc["taxes"][0]
        self.assertEqual(tax["charge_type"], "Actual")
        self.assertEqual(tax["account_head"], "2202 - Abrechnungskonto MWST - bic")
        self.assertEqual(tax["tax_amount"], -81.0)

    def test_totals_are_the_negative_of_bexio_s(self):
        net, tax, grand = self.totals
        self.assertEqual((net, tax, grand), (Decimal("-1000"), Decimal("-81"), Decimal("-1081")))
        item = self.doc["items"][0]
        self.assertEqual(Decimal(str(item["qty"] * item["rate"])) + Decimal(str(self.doc["taxes"][0]["tax_amount"])), grand)


class CreditNoteRefused(unittest.TestCase):
    def assertRefused(self, journal=JOURNAL, payments=PAYMENTS, invoices=None, look=None):
        with self.assertRaises(isl.Unmapped):
            icn.credit_note(journal, payments, invoices or [INVOICE], look or lookups())

    def test_two_lines_are_required(self):
        self.assertRefused(journal=JOURNAL[:1] + JOURNAL[2:])

    def test_the_lines_must_credit_one_account(self):
        journal = copy.deepcopy(JOURNAL)
        journal[1]["credit_account_id"] = 94
        self.assertRefused(journal=journal)

    def test_the_total_must_be_the_payment_rows(self):
        payments = copy.deepcopy(PAYMENTS)
        payments[0]["rows"][0]["value"] = "1080.000000"
        self.assertRefused(payments=payments)

    def test_the_vat_line_must_be_on_the_vat_account(self):
        look = lookups(account=dict(LOOKUPS["account"], **{"129": "3808 - Other - bic"}))
        self.assertRefused(look=look)

    def test_one_credit_voucher_only(self):
        payments = copy.deepcopy(PAYMENTS)
        payments[0]["rows"].append(dict(payments[0]["rows"][0], id=901, kb_credit_voucher_id=5))
        self.assertRefused(payments=payments)

    def test_the_invoice_must_be_in_erpnext(self):
        self.assertRefused(look=lookups(invoice={}))

    def test_the_invoice_must_be_in_chf(self):
        self.assertRefused(invoices=[copied(INVOICE, currency_id=2)])


class Drafts(unittest.TestCase):
    def test_plan_names_the_return_and_keeps_its_key(self):
        doc, number, _totals = icn.credit_note(JOURNAL, PAYMENTS, [INVOICE], lookups())
        plan = icn.drafts(doc, number)
        self.assertEqual(plan["exchange_rates"], [])
        [item] = plan["documents"]
        self.assertEqual((item["doctype"], item["name"], item["bexio_id"]), ("Sales Invoice", "CN-TEST-0004", "credit-4"))
        self.assertNotIn("doctype", item["values"])
        self.assertEqual(item["values"]["return_against"], "RE-0106")


if __name__ == "__main__":
    unittest.main()
