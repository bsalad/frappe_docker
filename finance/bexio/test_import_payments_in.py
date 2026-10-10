"""Offline tests for import_payments_in.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import copy
import unittest
from decimal import Decimal

import import_payments_in as ipi

# the lookups the mapping reads from ERPNext; the ids and names are invented
LOOKUPS = {
    "currency": {"1": "CHF", "3": "USD"},
    "customer": {"100": "Beispiel AG"},
    "bank": {"1": {"account": "1020 - Bank - bic", "currency": "CHF"}},
    "receivable": {"CHF": "1100 - Forderungen - bic", "USD": "1101 - Forderungen USD - bic"},
    "exchange": {("USD", "CHF"): [("2025-06-30", Decimal("0.90")), ("2024-01-01", Decimal("0.85"))]},
    "invoice": {"500": "ACC-SINV-0001"},
    "gl": {"2030": "2030 - Erhaltene Anzahlungen - bic", "3806": "3806 - Kursdifferenzen - bic", "3203": "3203 - Provisionsbeiträge - bic"},
    "cost_center": "Main - bic",
}

INVOICE = {
    "id": 500, "document_nr": "RE-1001", "contact_id": 100, "currency_id": 1,
    "total": "1081.00", "total_received_payments": "0.00", "total_gross": "1000.00",
}
INVOICE_USD = {
    "id": 501, "document_nr": "RE-1002", "contact_id": 100, "currency_id": 3,
    "total": "200.00", "total_received_payments": "0.00", "total_gross": "200.00",
}

PAYMENT = {
    "id": 811, "date": "2026-01-20", "value": "1081.000000", "title": "payment receipt",
    "kb_invoice_id": 500, "bank_account_id": 1, "kb_bill_id": None, "kb_credit_voucher_id": None,
    "is_cash_discount": False, "is_client_account_redemption": False,
}


def row(base=PAYMENT, **changes):
    """A copy of a payment row with some fields changed."""
    doc = copy.deepcopy(base)
    doc.update(changes)
    return doc


def lookups(**changes):
    values = copy.deepcopy(LOOKUPS)
    values.update(changes)
    return values


def export(payments, invoices=(INVOICE, INVOICE_USD)):
    """A minimal export: payments grouped by their invoice, as invoice_payments.json groups them."""
    grouped = {}
    for r in payments:
        grouped.setdefault(r["kb_invoice_id"], []).append(r)
    return {
        "payments": [{"parent_id": k, "rows": v} for k, v in grouped.items()],
        "invoices": [copy.deepcopy(i) for i in invoices],
        "bank_accounts": [{"id": 1, "currency_id": 1}],
        "currencies": [{"id": 1, "name": "CHF"}, {"id": 3, "name": "USD"}],
    }


class ChfPayment(unittest.TestCase):
    def test_receipt_is_allocated_to_the_planned_invoice_name(self):
        # not drafted in ERPNext yet: the name is bexio's document number
        doc, received = ipi.map_payment(PAYMENT, INVOICE, lookups(invoice={}), Decimal("1081.00"))
        self.assertEqual(doc["doctype"], "Payment Entry")
        self.assertEqual(doc["payment_type"], "Receive")
        self.assertEqual((doc["party_type"], doc["party"]), ("Customer", "Beispiel AG"))
        self.assertEqual(doc["paid_to"], "1020 - Bank - bic")
        self.assertEqual(doc["paid_from"], "1100 - Forderungen - bic")
        self.assertEqual(doc["posting_date"], "2026-01-20")
        self.assertEqual(doc["bexio_id"], "811")
        self.assertEqual(received, Decimal("1081.00"))
        self.assertEqual(doc["references"], [{
            "reference_doctype": "Sales Invoice", "reference_name": "RE-1001",
            "allocated_amount": 1081.0, "total_amount": 1081.0, "outstanding_amount": 1081.0,
        }])

    def test_an_erpnext_invoice_by_bexio_id_is_referenced_by_its_name(self):
        doc, _ = ipi.map_payment(PAYMENT, INVOICE, LOOKUPS, Decimal("1081.00"))
        self.assertEqual(doc["references"][0]["reference_name"], "ACC-SINV-0001")


class ForeignPayment(unittest.TestCase):
    def test_usd_invoice_is_received_in_chf_at_the_rate_on_or_before_the_payment_date(self):
        usd = row(PAYMENT, kb_invoice_id=501, value="200.000000", date="2026-01-20")
        doc, received = ipi.map_payment(usd, INVOICE_USD, LOOKUPS, Decimal("200.00"))
        self.assertEqual(doc["paid_from_account_currency"], "USD")
        self.assertEqual(doc["paid_amount"], 200.0)
        self.assertEqual(doc["source_exchange_rate"], 0.9)
        self.assertEqual(received, Decimal("180.00"))
        self.assertEqual(doc["received_amount"], 180.0)

    def test_an_older_payment_takes_the_older_rate(self):
        usd = row(PAYMENT, kb_invoice_id=501, value="200.000000", date="2025-03-01")
        doc, received = ipi.map_payment(usd, INVOICE_USD, LOOKUPS, Decimal("200.00"))
        self.assertEqual(received, Decimal("170.00"))

    def test_no_rate_on_or_before_the_date_is_unmapped(self):
        usd = row(PAYMENT, kb_invoice_id=501, value="200.000000", date="2023-06-01")
        with self.assertRaisesRegex(ipi.Unmapped, "no Currency Exchange USD"):
            ipi.map_payment(usd, INVOICE_USD, LOOKUPS, Decimal("200.00"))

    def test_no_receivable_in_the_invoice_currency_is_unmapped(self):
        usd = row(PAYMENT, kb_invoice_id=501, value="200.000000", date="2026-01-20")
        with self.assertRaisesRegex(ipi.Unmapped, "no receivable account in USD"):
            ipi.map_payment(usd, INVOICE_USD, lookups(receivable={"CHF": "1100 - Forderungen - bic"}), Decimal("200.00"))


class NotAReceipt(unittest.TestCase):
    def test_flags_that_are_not_bank_money_are_each_unmapped(self):
        for flag, reason in ipi.NOT_A_RECEIPT:
            with self.subTest(flag=flag):
                with self.assertRaisesRegex(ipi.Unmapped, reason.split(",")[0]):
                    ipi.map_payment(row(PAYMENT, **{flag: 1}), INVOICE, LOOKUPS, Decimal("1081.00"))

    def test_credit_voucher_payment_without_a_bank_is_named_as_such(self):
        cv = row(PAYMENT, kb_credit_voucher_id=7, bank_account_id=None)
        with self.assertRaisesRegex(ipi.Unmapped, "credit voucher offset"):
            ipi.map_payment(cv, INVOICE, LOOKUPS, Decimal("1081.00"))


class Unmapped_(unittest.TestCase):
    def test_invoice_missing_from_the_export(self):
        with self.assertRaisesRegex(ipi.Unmapped, "not in the export"):
            ipi.map_payment(PAYMENT, None, LOOKUPS, Decimal("1081.00"))

    def test_contact_without_a_customer(self):
        with self.assertRaisesRegex(ipi.Unmapped, "no Customer"):
            ipi.map_payment(PAYMENT, dict(INVOICE, contact_id=999), LOOKUPS, Decimal("1081.00"))

    def test_payment_without_a_bank_or_booking_account(self):
        with self.assertRaisesRegex(ipi.Unmapped, "no bank account"):
            ipi.map_payment(row(PAYMENT, bank_account_id=None), INVOICE, LOOKUPS, Decimal("1081.00"))

    def test_bank_without_an_erpnext_bank_account(self):
        with self.assertRaisesRegex(ipi.Unmapped, "no ERPNext Bank Account"):
            ipi.map_payment(row(PAYMENT, bank_account_id=9), INVOICE, LOOKUPS, Decimal("1081.00"))

    def test_bank_not_in_chf(self):
        bank = {"1": {"account": "1021 - Bank EUR - bic", "currency": "EUR"}}
        with self.assertRaisesRegex(ipi.Unmapped, "not in CHF"):
            ipi.map_payment(PAYMENT, INVOICE, lookups(bank=bank), Decimal("1081.00"))

    def test_invoice_without_a_document_number_and_not_in_erpnext(self):
        with self.assertRaisesRegex(ipi.Unmapped, "no document number"):
            ipi.map_payment(PAYMENT, dict(INVOICE, document_nr=""), lookups(invoice={}), Decimal("1081.00"))


class Outstanding(unittest.TestCase):
    def test_partial_payments_in_date_order_and_the_last_one_clears_the_invoice(self):
        first = row(PAYMENT, id=812, date="2026-02-01", value="300.000000")
        last = row(PAYMENT, id=813, date="2026-03-01", value="781.000000")
        results = ipi.plan(export([last, first]), LOOKUPS)
        self.assertEqual([r["bexio_id"] for r in results], ["812", "813"])
        self.assertTrue(all(r["doc"] for r in results))
        self.assertEqual(results[1]["doc"]["references"][0]["outstanding_amount"], 781.0)

    def test_payment_beyond_what_is_owed_is_unmapped_not_capped(self):
        first = row(PAYMENT, id=811, date="2026-01-10", value="300.000000")
        again = row(PAYMENT, id=734, date="2026-01-23", value="1081.000000")
        results = ipi.plan(export([first, again]), LOOKUPS)
        self.assertTrue(results[0]["doc"])
        self.assertIn("exceeds the outstanding amount", results[1]["error"])

    def test_money_beyond_an_invoice_that_is_paid_is_an_advance_on_2030(self):
        first = row(PAYMENT, id=811, date="2026-01-10", value="1081.000000")
        again = row(PAYMENT, id=734, date="2026-01-23", value="1081.000000", title="Overpayment")
        results = ipi.plan(export([first, again]), LOOKUPS)
        self.assertIsNone(results[1]["error"])
        self.assertEqual(results[1]["doc"]["paid_from"], "2030 - Erhaltene Anzahlungen - bic")
        self.assertNotIn("references", results[1]["doc"])
        self.assertEqual(results[1]["chf"], Decimal("1081.00"))

    def test_an_unmapped_payment_does_not_reduce_what_is_owed(self):
        unmapped = row(PAYMENT, id=810, date="2026-01-05", bank_account_id=None)
        mapped = row(PAYMENT, id=811, date="2026-01-10", value="1081.000000")
        results = ipi.plan(export([unmapped, mapped]), LOOKUPS)
        self.assertIsNotNone(results[1]["doc"])
        self.assertEqual(results[1]["doc"]["references"][0]["outstanding_amount"], 1081.0)


class Reconciliation(unittest.TestCase):
    def test_a_mapped_total_that_differs_from_bexios_received_is_listed(self):
        results = ipi.plan(export([row(PAYMENT)]), LOOKUPS)
        data = export([row(PAYMENT)], invoices=[dict(INVOICE, total_received_payments="1000.00")])
        self.assertEqual(ipi.reconciliation(results, data), ["invoice 500: mapped 1081.00 against bexio's received 1000.00"])

    def test_equal_totals_are_not_listed(self):
        results = ipi.plan(export([row(PAYMENT)]), LOOKUPS)
        data = export([row(PAYMENT)], invoices=[dict(INVOICE, total_received_payments="1081.00")])
        self.assertEqual(ipi.reconciliation(results, data), [])


class Summary(unittest.TestCase):
    def test_totals_per_year_and_reasons_without_names_or_ids(self):
        results = ipi.plan(export([
            row(PAYMENT),
            row(PAYMENT, id=812, date="2025-02-01", bank_account_id=None),
            row(PAYMENT, id=813, kb_invoice_id=501, date="2026-01-20", value="200.000000"),
            row(PAYMENT, id=814, kb_invoice_id=501, date="2023-06-01", value="200.000000"),
        ], invoices=(INVOICE, INVOICE_USD)), LOOKUPS)
        text = ipi.summary(results)
        self.assertNotIn("Beispiel", text)
        self.assertNotIn("811", text)
        self.assertIn("2026", text)
        self.assertIn("no bank account and no booking account in the export", text)
        self.assertIn("no Currency Exchange USD to CHF on or before the payment date", text)
        self.assertIn("total", text)

    def test_chf_received_is_summed_per_payment_year(self):
        results = ipi.plan(export([row(PAYMENT)]), LOOKUPS)
        self.assertEqual(results[0]["year"], "2026")
        self.assertEqual(results[0]["chf"], Decimal("1081.00"))


class FieldCheck(unittest.TestCase):
    def test_a_field_the_doctype_does_not_have_is_named(self):
        doc, _ = ipi.map_payment(PAYMENT, INVOICE, LOOKUPS, Decimal("1081.00"))
        metas = {
            "Payment Entry": {k for k in doc if k not in ("references",)} - {"received_amount"},
            "Payment Entry Reference": {"reference_doctype", "reference_name", "allocated_amount", "total_amount"},
        }
        self.assertEqual(ipi.unknown_fields(doc, metas), ["Payment Entry Reference.outstanding_amount", "Payment Entry.received_amount"])

    def test_nothing_unknown_when_every_field_exists(self):
        doc, _ = ipi.map_payment(PAYMENT, INVOICE, LOOKUPS, Decimal("1081.00"))
        metas = {
            "Payment Entry": set(doc),
            "Payment Entry Reference": set(doc["references"][0]),
        }
        self.assertEqual(ipi.unknown_fields(doc, metas), [])


class WritePlan(unittest.TestCase):
    def test_a_payment_is_handed_over_without_a_name_so_the_series_names_it(self):
        results = ipi.plan(export([row(PAYMENT)]), LOOKUPS)
        documents, skipped = ipi.write_plan(results, {"ACC-SINV-0001"})
        self.assertEqual(skipped, [])
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["doctype"], "Payment Entry")
        self.assertIsNone(documents[0]["name"])
        self.assertEqual(documents[0]["bexio_id"], "811")
        self.assertEqual(documents[0]["values"]["bexio_id"], "811")
        self.assertEqual((documents[0]["values"]["reference_no"], documents[0]["values"]["reference_date"]),
                         ("bexio payment 811", "2026-01-20"))
        self.assertNotIn("doctype", documents[0]["values"])
        self.assertEqual(documents[0]["values"]["references"][0]["reference_name"], "ACC-SINV-0001")

    def test_a_payment_whose_invoice_is_not_submitted_is_skipped_and_counted(self):
        results = ipi.plan(export([row(PAYMENT)]), LOOKUPS)
        documents, skipped = ipi.write_plan(results, set())
        self.assertEqual(documents, [])
        self.assertEqual(skipped, ["811"])

    def test_an_unmapped_payment_is_not_handed_over(self):
        results = ipi.plan(export([row(PAYMENT, bank_account_id=None)]), LOOKUPS)
        self.assertEqual(ipi.write_plan(results, {"ACC-SINV-0001"}), ([], []))

    def test_over_allocation_is_the_unmapped_reason_the_live_run_stops_on(self):
        first = row(PAYMENT, id=811, date="2026-01-10", value="300.000000")
        again = row(PAYMENT, id=734, date="2026-01-23", value="1081.000000")
        results = ipi.plan(export([first, again]), LOOKUPS)
        over = [r["bexio_id"] for r in results if r["error"] == ipi.OVER_ALLOCATED]
        self.assertEqual(over, ["734"])

    def test_an_advance_is_handed_over_on_the_invoice_submission_test_too(self):
        first = row(PAYMENT, id=811, date="2026-01-10", value="1081.000000")
        again = row(PAYMENT, id=734, date="2026-01-23", value="1081.000000")
        results = ipi.plan(export([first, again]), LOOKUPS)
        documents, skipped = ipi.write_plan(results, set())
        self.assertEqual(documents, [])
        self.assertEqual(skipped, ["811", "734"])


CHF_ERP = {"501": {"name": "ACC-SINV-0002", "currency": "CHF", "total": Decimal("180.00"), "outstanding": Decimal("180.00")}}
USD_PAYMENT = row(PAYMENT, kb_invoice_id=501, value="200.000000")


def chf_booked(chf, erp=CHF_ERP):
    """Lookups for a USD invoice that ERPNext holds in CHF, and the journal booked `chf` to the bank for its receipt."""
    return lookups(invoice_erp=erp, booked_chf={"811": Decimal(chf)})


class ChfBooked(unittest.TestCase):
    def test_the_chf_booked_to_the_bank_is_received_and_settles_the_chf_outstanding(self):
        doc, received = ipi.map_payment(USD_PAYMENT, INVOICE_USD, chf_booked("180.00"), Decimal("180.00"))
        self.assertEqual(doc["paid_from"], "1100 - Forderungen - bic")
        self.assertEqual(doc["paid_from_account_currency"], "CHF")
        self.assertEqual(received, Decimal("180.00"))
        self.assertEqual(doc["references"][0]["reference_name"], "ACC-SINV-0002")
        self.assertEqual(doc["references"][0]["allocated_amount"], 180.0)
        self.assertNotIn("deductions", doc)

    def test_a_gap_of_up_to_five_cents_is_an_exchange_difference_on_kursdifferenzen(self):
        doc, received = ipi.map_payment(USD_PAYMENT, INVOICE_USD, chf_booked("179.98"), Decimal("180.00"))
        self.assertEqual(received, Decimal("179.98"))
        self.assertEqual(doc["references"][0]["allocated_amount"], 180.0)
        self.assertEqual(doc["deductions"], [{"account": "3806 - Kursdifferenzen - bic", "cost_center": "Main - bic", "amount": 0.02}])

    def test_a_larger_gap_is_left_open_and_not_booked_as_a_difference(self):
        doc, _ = ipi.map_payment(USD_PAYMENT, INVOICE_USD, chf_booked("170.00"), Decimal("180.00"))
        self.assertEqual(doc["references"][0]["allocated_amount"], 170.0)
        self.assertNotIn("deductions", doc)
        results = ipi.plan(export([USD_PAYMENT]), chf_booked("170.00"))
        self.assertIn("stays open", results[0]["note"])

    def test_a_larger_gap_bexio_booked_to_another_account_is_a_deduction_to_that_account(self):
        lk = lookups(invoice_erp=CHF_ERP, booked_chf={"811": Decimal("170.00")},
                     booked_deductions={"811": (4598, "3203", Decimal("10.00"))})
        doc, received = ipi.map_payment(USD_PAYMENT, INVOICE_USD, lk, Decimal("180.00"))
        self.assertEqual(doc["references"][0]["allocated_amount"], 180.0)
        self.assertEqual(doc["deductions"], [{"account": "3203 - Provisionsbeiträge - bic", "cost_center": "Main - bic", "amount": 10.0}])
        self.assertEqual(received, Decimal("170.00"))

    def test_a_bexio_deduction_of_another_amount_does_not_settle_the_gap(self):
        lk = lookups(invoice_erp=CHF_ERP, booked_chf={"811": Decimal("170.00")},
                     booked_deductions={"811": (4598, "3203", Decimal("5.00"))})
        doc, _ = ipi.map_payment(USD_PAYMENT, INVOICE_USD, lk, Decimal("180.00"))
        self.assertEqual(doc["references"][0]["allocated_amount"], 170.0)
        self.assertNotIn("deductions", doc)

    def test_a_bexio_deduction_to_an_account_the_chart_lacks_is_unmapped(self):
        lk = lookups(invoice_erp=CHF_ERP, booked_chf={"811": Decimal("170.00")},
                     booked_deductions={"811": (4598, "3299", Decimal("10.00"))})
        with self.assertRaisesRegex(ipi.Unmapped, "no account 3299"):
            ipi.map_payment(USD_PAYMENT, INVOICE_USD, lk, Decimal("180.00"))

    def test_the_journal_line_of_a_booked_deduction_is_the_one_the_plan_covers(self):
        lk = lookups(invoice_erp=CHF_ERP, booked_chf={"811": Decimal("170.00")},
                     booked_deductions={"811": (4598, "3203", Decimal("10.00"))})
        results = ipi.plan(export([USD_PAYMENT]), lk)
        self.assertEqual(results[0]["journal_line"], 4598)
        self.assertIsNone(results[0]["error"])

    def test_more_than_the_chf_outstanding_is_unmapped(self):
        with self.assertRaisesRegex(ipi.Unmapped, ipi.OVER_ALLOCATED):
            ipi.map_payment(USD_PAYMENT, INVOICE_USD, chf_booked("181.00"), Decimal("180.00"))

    def test_a_receipt_with_no_chf_booking_in_the_journal_is_unmapped(self):
        with self.assertRaisesRegex(ipi.Unmapped, "no CHF booking"):
            ipi.map_payment(USD_PAYMENT, INVOICE_USD, lookups(invoice_erp=CHF_ERP), Decimal("180.00"))

    def test_the_plan_settles_the_chf_booked_receipt_on_the_chf_outstanding(self):
        results = ipi.plan(export([USD_PAYMENT]), chf_booked("180.00"))
        self.assertEqual(results[0]["chf"], Decimal("180.00"))
        self.assertIsNone(results[0]["error"])


class Advance(unittest.TestCase):
    def test_money_received_before_the_invoice_owes_anything_has_no_reference(self):
        doc, received = ipi.map_payment(row(PAYMENT, value="1081.000000"), INVOICE, LOOKUPS, Decimal("0"))
        self.assertEqual(doc["paid_from"], "2030 - Erhaltene Anzahlungen - bic")
        self.assertEqual((doc["party_type"], doc["party"]), ("Customer", "Beispiel AG"))
        self.assertNotIn("references", doc)
        self.assertEqual(received, Decimal("1081.00"))

    def test_a_partly_owed_invoice_is_not_an_advance(self):
        with self.assertRaisesRegex(ipi.Unmapped, ipi.OVER_ALLOCATED):
            ipi.map_payment(row(PAYMENT, value="1081.000000"), INVOICE, LOOKUPS, Decimal("500"))


class VatOnPayment(unittest.TestCase):
    ACCOUNTS = [{"id": 77, "account_no": "1020"}, {"id": 93, "account_no": "1100"},
                {"id": 127, "account_no": "2200"}, {"id": 129, "account_no": "2202"}]
    GL = {"2200": "2200 - Geschuldete MWST - bic", "2202": "2202 - Umsatzsteuerausgleich - bic"}

    def line(self, line_id, debit, credit, amount, ref=811):
        return {"id": line_id, "ref_class": "KbClientAccountEntry", "ref_id": ref, "debit_account_id": debit,
                "credit_account_id": credit, "base_currency_amount": amount, "amount": amount, "currency_id": 1}

    def test_vat_lines_are_the_2202_to_2200_moves_on_a_receipt(self):
        journal = [self.line(5, 129, 127, "19.50"), self.line(6, 129, 127, 0, ref=812), self.line(7, 77, 93, "100.00")]
        self.assertEqual([x["id"] for x in ipi.vat_lines(journal, self.ACCOUNTS, {"811", "812"})], [5, 6])

    def test_the_bank_booked_is_the_debit_on_a_bank_and_the_credit_on_the_receivable(self):
        journal = [self.line(7, 77, 93, "1081.0000"), self.line(8, 129, 127, "19.50")]
        self.assertEqual(ipi.bank_booked_chf(journal, self.ACCOUNTS, {"811"}), {"811": Decimal("1081.0000")})

    def test_each_receipt_moves_its_vat_as_a_journal_entry_on_its_date(self):
        results = ipi.plan(export([row(PAYMENT)]), LOOKUPS)
        docs, _ = ipi.write_plan(results, {"ACC-SINV-0001"}, [self.line(5, 129, 127, "19.50")], self.GL)
        je = [d for d in docs if d["doctype"] == "Journal Entry"]
        self.assertEqual(len(je), 1)
        self.assertEqual((je[0]["bexio_id"], je[0]["name"]), ("5", None))
        # the loader stores what is in values, so the bexio id must be there too: a rerun finds the document by it
        self.assertEqual(je[0]["values"]["bexio_id"], "5")
        self.assertEqual(je[0]["values"]["posting_date"], "2026-01-20")
        self.assertEqual(je[0]["values"]["accounts"], [
            {"account": "2202 - Umsatzsteuerausgleich - bic", "debit_in_account_currency": 19.5},
            {"account": "2200 - Geschuldete MWST - bic", "credit_in_account_currency": 19.5},
        ])

    def test_a_zero_vat_line_is_no_journal_entry(self):
        results = ipi.plan(export([row(PAYMENT)]), LOOKUPS)
        docs, _ = ipi.write_plan(results, {"ACC-SINV-0001"}, [self.line(5, 129, 127, 0)], self.GL)
        self.assertEqual([d["doctype"] for d in docs], ["Payment Entry"])

    def test_a_receipt_line_that_is_none_of_these_is_left_for_erp_fd93(self):
        journal = [self.line(7, 77, 93, "100.00"), self.line(5, 129, 127, "19.50"), self.line(9, 93, 99, "100.82")]
        accounts = self.ACCOUNTS + [{"id": 99, "account_no": "3203"}, {"id": 123, "account_no": "2030"}]
        self.assertEqual([x["id"] for x in ipi.left_lines(journal, accounts, {"811"})], [9])

    def test_a_receipt_line_bexio_booked_from_the_receivable_to_another_account_is_a_deduction(self):
        journal = [self.line(4, 99, 93, "0"), self.line(7, 77, 93, "170.00"), self.line(9, 99, 93, "10.00")]
        accounts = self.ACCOUNTS + [{"id": 99, "account_no": "3203"}, {"id": 123, "account_no": "2030"}]
        self.assertEqual(ipi.bank_booked_deductions(journal, accounts, {"811"}), {"811": (9, "3203", Decimal("10.00"))})

    def test_vat_of_a_receipt_not_handed_over_is_no_journal_entry(self):
        results = ipi.plan(export([row(PAYMENT)]), LOOKUPS)
        docs, skipped = ipi.write_plan(results, set(), [self.line(5, 129, 127, "19.50")], self.GL)
        self.assertEqual((docs, skipped), ([], ["811"]))


if __name__ == "__main__":
    unittest.main()
