"""Offline tests for import_sales.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import copy
import unittest
from decimal import Decimal

import import_sales as isl

# the lookups the mapping reads from ERPNext; the ids and names are invented
LOOKUPS = {
    "currency": {"1": "CHF", "2": "EUR"},
    "customer": {"100": "Beispiel AG"},
    "item": {"7": "BEISPIEL-DIENST"},
    "account": {"30": "3200 - Honorare - bic"},
    "tax": {
        "28": {"name": "USt 8.1% Normal", "account": "2200 - Umsatzsteuer - bic", "rate": Decimal("8.1")},
        "29": {"name": "USt 2.6% Reduziert", "account": "2200 - Umsatzsteuer - bic", "rate": Decimal("2.6")},
    },
    "invoice": {"500": "SINV-0001"},
    "existing": {"Sales Invoice": set(), "Sales Order": set(), "Quotation": set()},
}

INVOICE = {
    "id": 500, "document_nr": "RE-1001", "contact_id": 100, "currency_id": 1,
    "is_valid_from": "2024-03-01", "is_valid_to": "2024-03-31", "kb_item_status_id": 9,
    "header": "Vielen Dank.", "footer": "Zahlbar innert 30 Tagen.",
    "positions": [
        {"type": "KbPositionArticle", "article_id": 7, "amount": "2", "unit_price": "50.00",
         "account_id": 30, "tax_id": 28, "text": "Beratung"},
        {"type": "KbPositionCustom", "amount": "1", "unit_price": "50.00",
         "account_id": 30, "tax_id": 29, "text": "Material"},
        {"type": "KbPositionText", "text": "Zwischentitel"},
    ],
    "taxs": [{"percentage": "8.1", "value": "8.10"}, {"percentage": "2.6", "value": "1.30"}],
    "total_net": "150.00", "total_taxes": "9.40", "total_gross": "159.40",
}


def record(base, **changes):
    """A copy of a base record with some fields changed."""
    doc = copy.deepcopy(base)
    doc.update(changes)
    return doc


def lookups(**changes):
    values = copy.deepcopy(LOOKUPS)
    values.update(changes)
    return values


class DomesticInvoice(unittest.TestCase):
    def test_two_rates_and_a_free_text_position(self):
        doc = isl.sales_invoice(INVOICE, LOOKUPS)
        self.assertEqual(doc["doctype"], "Sales Invoice")
        self.assertEqual(doc["customer"], "Beispiel AG")
        self.assertEqual(doc["bexio_id"], "500")
        self.assertEqual((doc["posting_date"], doc["due_date"]), ("2024-03-01", "2024-03-31"))
        self.assertEqual(doc["currency"], "CHF")
        self.assertEqual([r["item_code"] for r in doc["items"]], ["BEISPIEL-DIENST", "bexio Position", "bexio Position"])
        self.assertEqual(doc["items"][1]["description"], "Material")
        self.assertEqual(doc["items"][1]["income_account"], "3200 - Honorare - bic")

    def test_text_line_is_a_row_of_zero(self):
        doc = isl.sales_invoice(INVOICE, LOOKUPS)
        self.assertEqual(doc["items"][2]["description"], "Zwischentitel")
        self.assertEqual(doc["items"][2]["amount"], 0.0)

    def test_taxes_are_one_row_per_bexio_tax_id_and_match_bexio_to_the_rappen(self):
        doc, differences, totals, rate = isl._document("Sales Invoice", INVOICE, LOOKUPS)
        self.assertEqual([(t["description"], t["tax_amount"]) for t in doc["taxes"]],
                         [("USt 8.1% Normal", 8.1), ("USt 2.6% Reduziert", 1.3)])
        self.assertEqual(differences, [])
        self.assertEqual(totals, (Decimal("150.00"), Decimal("9.40"), Decimal("159.40")))
        self.assertEqual(rate, Decimal("1"))

    def test_remarks_keep_the_bexio_number_and_terms_keep_header_and_footer(self):
        doc = isl.sales_invoice(INVOICE, LOOKUPS)
        self.assertEqual(doc["remarks"], "bexio Nr. RE-1001")
        self.assertEqual(doc["terms"], "Vielen Dank.\nZahlbar innert 30 Tagen.")


class ForeignCurrency(unittest.TestCase):
    def test_eur_invoice_keeps_currency_and_rate_and_reports_chf_totals(self):
        eur = record(INVOICE, currency_id=2, exchange_rate="0.94",
                     positions=[{"type": "KbPositionArticle", "article_id": 7, "amount": "1",
                                 "unit_price": "200.00", "account_id": 30, "tax_id": 28}],
                     taxs=[{"percentage": "8.1", "value": "16.20"}],
                     total_net="200.00", total_taxes="16.20", total_gross="216.20")
        doc, differences, totals, rate = isl._document("Sales Invoice", eur, LOOKUPS)
        self.assertEqual(doc["currency"], "EUR")
        self.assertEqual(doc["conversion_rate"], 0.94)
        self.assertEqual(doc["taxes"][0]["tax_amount"], 16.2)
        self.assertEqual(differences, [])
        self.assertEqual(totals, (Decimal("200.00"), Decimal("16.20"), Decimal("216.20")))
        self.assertEqual(rate, Decimal("0.94"))

    def test_foreign_currency_without_rate_is_unmapped(self):
        eur = record(INVOICE, currency_id=2)
        with self.assertRaises(isl.Unmapped):
            isl.sales_invoice(eur, LOOKUPS)


class CreditNote(unittest.TestCase):
    def setUp(self):
        self.credit = {
            "id": 800, "invoice_id": 500, "document_nr": "GS-0001", "contact_id": 100, "currency_id": 1,
            "is_valid_from": "2024-04-02", "kb_item_status_id": 9,
            "positions": [{"type": "KbPositionCustom", "amount": "1", "unit_price": "50.00",
                           "account_id": 30, "tax_id": 28, "text": "Gutschrift"}],
            "taxs": [{"percentage": "8.1", "value": "4.05"}],
            "total_net": "50.00", "total_taxes": "4.05", "total_gross": "54.05",
        }

    def test_is_return_against_the_original_with_negative_rows(self):
        doc = isl.credit_note(self.credit, LOOKUPS)
        self.assertEqual(doc["doctype"], "Sales Invoice")
        self.assertEqual(doc["is_return"], 1)
        self.assertEqual(doc["return_against"], "SINV-0001")
        self.assertEqual(doc["bexio_id"], "credit-800")
        self.assertEqual(doc["items"][0]["qty"], -1.0)
        self.assertEqual(doc["items"][0]["amount"], -50.0)
        self.assertEqual(doc["taxes"][0]["tax_amount"], -4.05)

    def test_totals_match_bexio_and_are_negative(self):
        _doc, differences, totals, _rate = isl._document("Sales Invoice", self.credit, LOOKUPS, credit=True)
        self.assertEqual(differences, [])
        self.assertEqual(totals, (Decimal("-50.00"), Decimal("-4.05"), Decimal("-54.05")))

    def test_original_missing_is_unmapped(self):
        credit = record(self.credit, invoice_id=999)
        with self.assertRaises(isl.Unmapped):
            isl.credit_note(credit, LOOKUPS)


class SalesOrderAndQuotation(unittest.TestCase):
    def test_order_rows_carry_a_delivery_date(self):
        order = record(INVOICE, id=600)
        doc = isl.sales_order(order, LOOKUPS)
        self.assertEqual(doc["doctype"], "Sales Order")
        self.assertEqual(doc["transaction_date"], "2024-03-01")
        self.assertTrue(all(r["delivery_date"] == "2024-03-01" for r in doc["items"]))
        self.assertEqual(doc["bexio_id"], "600")

    def test_offer_is_a_quotation_to_the_customer(self):
        offer = record(INVOICE, id=700, is_valid_until="2024-04-30")
        doc = isl.quotation(offer, LOOKUPS)
        self.assertEqual(doc["doctype"], "Quotation")
        self.assertEqual((doc["quotation_to"], doc["party_name"]), ("Customer", "Beispiel AG"))
        self.assertEqual(doc["valid_till"], "2024-04-30")
        self.assertNotIn("customer", doc)


class Differences(unittest.TestCase):
    def test_a_bexio_tax_that_differs_is_reported_and_not_adjusted(self):
        off = record(INVOICE, taxs=[{"percentage": "8.1", "value": "8.11"}, {"percentage": "2.6", "value": "1.30"}])
        doc, differences, _totals, _rate = isl._document("Sales Invoice", off, LOOKUPS)
        self.assertEqual(differences, ["tax 8.1% +0.01"])
        self.assertEqual(doc["taxes"][0]["tax_amount"], 8.1)

    def test_a_total_that_differs_is_reported(self):
        off = record(INVOICE, total_gross="159.41")
        _doc, differences, _totals, _rate = isl._document("Sales Invoice", off, LOOKUPS)
        self.assertEqual(differences, ["gross -0.01"])


class Unmapped_(unittest.TestCase):
    def assertUnmapped(self, change):
        with self.assertRaises(isl.Unmapped):
            isl.sales_invoice(change, LOOKUPS)

    def test_unknown_contact(self):
        self.assertUnmapped(record(INVOICE, contact_id=999))

    def test_unknown_bexio_tax_id(self):
        positions = copy.deepcopy(INVOICE["positions"])
        positions[0]["tax_id"] = 3
        self.assertUnmapped(record(INVOICE, positions=positions))

    def test_tax_template_without_a_single_rate_row(self):
        values = lookups(tax={"28": {"name": "USt", "account": None, "rate": None}, "29": LOOKUPS["tax"]["29"]})
        with self.assertRaises(isl.Unmapped):
            isl.sales_invoice(INVOICE, values)

    def test_unknown_position_type(self):
        positions = copy.deepcopy(INVOICE["positions"])
        positions[0]["type"] = "KbPositionSubtotal"
        self.assertUnmapped(record(INVOICE, positions=positions))

    def test_discount(self):
        positions = copy.deepcopy(INVOICE["positions"])
        positions[0]["discount_in_percent"] = "10"
        self.assertUnmapped(record(INVOICE, positions=positions))

    def test_article_without_an_item(self):
        positions = copy.deepcopy(INVOICE["positions"])
        positions[0]["article_id"] = 8
        self.assertUnmapped(record(INVOICE, positions=positions))

    def test_unknown_currency(self):
        self.assertUnmapped(record(INVOICE, currency_id=9))


class FieldCheck(unittest.TestCase):
    def test_a_field_the_doctype_does_not_have_is_named(self):
        doc = isl.sales_invoice(INVOICE, LOOKUPS)
        metas = {
            "Sales Invoice": set(doc) - {"remarks", "doctype"} | {"items", "taxes"},
            "Sales Invoice Item": {"item_code", "description", "qty", "rate", "amount", "income_account"},
            "Sales Taxes and Charges": {"charge_type", "account_head", "description", "tax_amount"},
        }
        self.assertEqual(isl.unknown_fields(doc, metas), ["Sales Invoice.remarks"])


class Plan(unittest.TestCase):
    def test_one_unmapped_record_is_counted_and_listed_by_id_only(self):
        bad = record(INVOICE, id=501, contact_id=999)
        data = {"invoices": [INVOICE, bad], "credit_vouchers": [], "orders": [], "offers": []}
        results = isl.plan(data, LOOKUPS)
        self.assertEqual(sum(1 for r in results if r["doc"]), 1)
        lines = isl.detail_lines(results)
        self.assertEqual(lines, ["Sales Invoice 501: unmapped: contact 999 has no Customer"])

    def test_summary_has_totals_and_no_names(self):
        data = {"invoices": [INVOICE], "credit_vouchers": [], "orders": [], "offers": []}
        text = isl.summary(isl.plan(data, LOOKUPS), LOOKUPS)
        self.assertIn("invoices", text)
        self.assertIn("2024", text)
        self.assertNotIn("Beispiel", text)


if __name__ == "__main__":
    unittest.main()
