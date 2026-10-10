"""Offline tests for import_sales.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import copy
import json
import os
import tempfile
import unittest
from decimal import Decimal

import import_master as im
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
    "vat": "2202 - Abrechnungskonto MWST - bic",
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
    "total_net": "150.00", "total_taxes": "9.40", "total_gross": "159.40", "total": "159.40",
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

    def test_tax_rows_book_to_the_transitory_account_not_the_template_account(self):
        # the templates point at 2200; the sale's VAT goes to 2202 at the invoice date, and bexio moves it to 2200 on payment
        doc = isl.sales_invoice(INVOICE, LOOKUPS)
        self.assertEqual([t["account_head"] for t in doc["taxes"]], ["2202 - Abrechnungskonto MWST - bic"] * 2)
        self.assertNotIn("2200 - Umsatzsteuer - bic", [t["account_head"] for t in doc["taxes"]])

    def test_remarks_keep_the_bexio_number_and_terms_keep_header_and_footer(self):
        doc = isl.sales_invoice(INVOICE, LOOKUPS)
        self.assertEqual(doc["remarks"], "bexio Nr. RE-1001")
        self.assertEqual(doc["terms"], "Vielen Dank.\nZahlbar innert 30 Tagen.")


# an EUR invoice of 200.00 net and 216.20 gross, as bexio has it; its rate to CHF is 0.94
EUR_INVOICE = record(INVOICE, currency_id=2, exchange_rate="0.94",
                     positions=[{"type": "KbPositionArticle", "article_id": 7, "amount": "1",
                                 "unit_price": "200.00", "account_id": 30, "tax_id": 28}],
                     taxs=[{"percentage": "8.1", "value": "16.20"}],
                     total_net="200.00", total_taxes="16.20", total_gross="216.20", total="216.20")


class ForeignCurrency(unittest.TestCase):
    def test_eur_invoice_is_booked_in_chf_at_bexios_rate_with_the_original_in_the_remarks(self):
        doc, differences, totals, rate = isl._document("Sales Invoice", EUR_INVOICE, LOOKUPS)
        self.assertEqual((doc["currency"], doc["conversion_rate"]), ("CHF", 1.0))
        self.assertEqual(doc["items"][0]["rate"], 188.0)
        self.assertEqual(doc["taxes"][0]["tax_amount"], 15.23)
        self.assertEqual(doc["remarks"], "bexio Nr. RE-1001; bexio: EUR 216.20 @ 0.94")
        self.assertEqual(differences, [])
        self.assertEqual(totals, (Decimal("188.00"), Decimal("15.23"), Decimal("203.23")))
        self.assertEqual(rate, Decimal("1"))

    def test_each_amount_is_rounded_to_the_cent_in_chf(self):
        usd = record(INVOICE, currency_id=2, exchange_rate="0.90199",
                     positions=[{"type": "KbPositionCustom", "amount": "1", "unit_price": "33.33",
                                 "account_id": 30, "tax_id": 28, "text": "Material"}],
                     taxs=[{"percentage": "8.1", "value": "2.70"}],
                     total_net="33.33", total_taxes="2.70", total_gross="36.03", total="36.03")
        doc, differences, _totals, _rate = isl._document("Sales Invoice", usd, LOOKUPS)
        self.assertEqual(doc["items"][0]["rate"], 30.06)
        # bexio's tax 2.44 against 8.1 % of 30.06 (2.43): the rappen goes into the tax row with the total's
        self.assertEqual(doc["taxes"][0]["tax_amount"], 2.44)
        self.assertIn("total +0.01 taken into the last tax row", differences)
        self.assertEqual(doc["remarks"], "bexio Nr. RE-1001; bexio: EUR 36.03 @ 0.90199")

    def test_foreign_currency_without_rate_is_unmapped(self):
        eur = record(INVOICE, currency_id=2)
        with self.assertRaises(isl.Unmapped):
            isl.sales_invoice(eur, LOOKUPS)


class JournalCheck(unittest.TestCase):
    """A foreign invoice is left out unless bexio's journal books its CHF total on the receivables account."""

    ACCOUNTS = [{"id": 93, "account_no": "1100"}, {"id": 3203, "account_no": "3200"}]

    def journal(self, base, invoice_id=500):
        return [{"ref_class": "KbInvoice", "ref_id": invoice_id, "ref_uuid": None, "debit_account_id": 93,
                 "credit_account_id": 3203, "amount": 216.2, "base_currency_amount": base, "currency_factor": 0.94}]

    def plan(self, base=None, **changes):
        data = {"invoices": [record(EUR_INVOICE, **changes)], "accounts": self.ACCOUNTS}
        if base is not None:
            data["journal"] = self.journal(base)
        return isl.plan(data, lookups(rate_at=lambda code, day: (Decimal("0.90"), day)))

    def test_a_booking_that_matches_the_chf_total_maps(self):
        (result,) = self.plan(base=203.23)
        self.assertIsNotNone(result["doc"])
        self.assertIsNone(result["error"])

    def test_a_booking_5_rappen_off_is_within_tolerance(self):
        (result,) = self.plan(base=203.18)
        self.assertIsNotNone(result["doc"])

    def test_a_booking_more_than_5_rappen_off_is_left_out_with_the_difference(self):
        (result,) = self.plan(base=203.13)
        self.assertIsNone(result["doc"])
        self.assertEqual(result["error"], "CHF total differs from bexio's CHF booking on 1100 by +0.10")

    def test_no_journal_or_no_booking_is_left_out(self):
        (result,) = self.plan()
        self.assertEqual(result["error"], "bexio's journal has no CHF booking on 1100 for the invoice")

    def test_a_domestic_invoice_is_not_checked_against_the_journal(self):
        (result,) = self.plan(currency_id=1, exchange_rate=None)
        self.assertIsNotNone(result["doc"])
        self.assertEqual(result["doc"]["currency"], "CHF")

    def test_an_invoice_without_a_rate_of_its_own_takes_the_rate_bexio_booked_it_at(self):
        journal = self.journal(203.23)
        journal[0]["currency_factor"] = 0.94
        data = {"invoices": [record(EUR_INVOICE, exchange_rate=None)], "accounts": self.ACCOUNTS, "journal": journal}

        def rate_at(code, day):
            raise AssertionError("bexio booked the invoice at a rate: no ECB lookup")

        (result,) = isl.plan(data, lookups(rate_at=rate_at))
        self.assertIsNone(result["error"])
        self.assertTrue(result["doc"]["remarks"].endswith("; bexio: EUR 216.20 @ 0.94"))

    def test_booked_chf_takes_the_receivable_debit_and_the_credit_back(self):
        journal = self.journal(203.23) + [dict(self.journal(10)[0], debit_account_id=3203, credit_account_id=93)]
        self.assertEqual(im.booked_chf(journal, self.ACCOUNTS, "1100"), {("KbInvoice", "500"): Decimal("193.23")})


class CreditNote(unittest.TestCase):
    def setUp(self):
        self.credit = {
            "id": 800, "invoice_id": 500, "document_nr": "GS-0001", "contact_id": 100, "currency_id": 1,
            "is_valid_from": "2024-04-02", "kb_item_status_id": 9,
            "positions": [{"type": "KbPositionCustom", "amount": "1", "unit_price": "50.00",
                           "account_id": 30, "tax_id": 28, "text": "Gutschrift"}],
            "taxs": [{"percentage": "8.1", "value": "4.05"}],
            "total_net": "50.00", "total_taxes": "4.05", "total_gross": "54.05", "total": "54.05",
        }

    def test_is_return_against_the_original_with_negative_rows(self):
        doc = isl.credit_note(self.credit, LOOKUPS)
        self.assertEqual(doc["doctype"], "Sales Invoice")
        self.assertEqual(doc["is_return"], 1)
        self.assertEqual(doc["return_against"], "SINV-0001")
        self.assertEqual(doc["bexio_id"], "credit-800")
        self.assertEqual(doc["items"][0]["qty"], -1.0)
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


class OptionalAndEmpty(unittest.TestCase):
    def test_an_optional_position_is_an_alternative_row_and_counts_in_no_total(self):
        offer = record(INVOICE, id=900, positions=[
            {"type": "KbPositionArticle", "article_id": 7, "amount": "2", "unit_price": "50.00",
             "account_id": 30, "tax_id": 28, "text": "Beratung"},
            {"type": "KbPositionCustom", "amount": "1", "unit_price": "50.00", "account_id": 30, "tax_id": 29,
             "text": "Zusatz", "is_optional": True},
        ], taxs=[{"percentage": "8.1", "value": "8.10"}], total_net="100.00", total_taxes="8.10",
            total_gross="108.10", total="108.10")
        doc, differences, _, _ = isl._document("Quotation", offer, LOOKUPS)
        self.assertEqual(differences, [])
        self.assertNotIn("is_alternative", doc["items"][0])
        self.assertEqual((doc["items"][1]["description"], doc["items"][1]["is_alternative"]), ("Zusatz", 1))
        self.assertEqual([t["tax_amount"] for t in doc["taxes"]], [8.1])

    def test_an_offer_with_only_optional_positions_has_a_zero_total_as_bexio_has(self):
        offer = record(INVOICE, id=901, positions=[
            {"type": "KbPositionCustom", "amount": "1", "unit_price": "50.00", "account_id": 30, "tax_id": 29,
             "text": "Zusatz", "is_optional": True},
        ], taxs=[], total_net="0", total_taxes="0", total_gross="0", total="0")
        doc, differences, totals, _ = isl._document("Quotation", offer, LOOKUPS)
        self.assertEqual(doc["taxes"], [])
        self.assertEqual(differences, [])
        self.assertEqual(totals, (Decimal("0"), Decimal("0"), Decimal("0")))

    def test_an_optional_position_outside_a_quotation_is_unmapped(self):
        order = record(INVOICE, id=902, positions=[
            {"type": "KbPositionCustom", "amount": "1", "unit_price": "50.00", "account_id": 30, "tax_id": 29,
             "text": "Zusatz", "is_optional": True},
        ], total="0", total_net="0", total_taxes="0", total_gross="0", taxs=[])
        with self.assertRaisesRegex(isl.Unmapped, "optional position outside a quotation"):
            isl.sales_order(order, LOOKUPS)

    def test_an_offer_without_positions_is_unmapped_not_an_empty_quotation(self):
        offer = record(INVOICE, id=903, positions=[], taxs=[], total_net="0", total_taxes="0",
                       total_gross="0", total="0")
        with self.assertRaisesRegex(isl.Unmapped, "no positions"):
            isl.quotation(offer, LOOKUPS)


class DeliveryNote(unittest.TestCase):
    def test_delivery_is_a_delivery_note_dated_by_its_day(self):
        delivery = record(INVOICE, id=800, document_nr="LI-1", is_valid_from="2024-03-05")
        doc = isl.delivery_note(delivery, LOOKUPS)
        self.assertEqual(doc["doctype"], "Delivery Note")
        self.assertEqual((doc["posting_date"], doc["set_posting_time"]), ("2024-03-05", 1))
        self.assertEqual(doc["customer"], "Beispiel AG")
        self.assertEqual(doc["bexio_id"], "800")
        self.assertNotIn("transaction_date", doc)
        self.assertTrue(all("income_account" not in r and "delivery_date" not in r for r in doc["items"]))

    def test_its_positions_and_taxes_map_as_on_an_invoice(self):
        doc, differences, totals, _rate = isl._document("Delivery Note", record(INVOICE, id=801), LOOKUPS)
        self.assertEqual(differences, [])
        self.assertEqual(totals, (Decimal("150.00"), Decimal("9.40"), Decimal("159.40")))
        self.assertEqual(len(doc["taxes"]), 2)

    def test_a_delivery_with_a_zero_total_keeps_its_quantities_at_zero_rate(self):
        # bexio gives the deliveries a zero total though their positions carry prices: the draft agrees with it
        zero = record(INVOICE, id=803, taxs=[{"percentage": "8.1", "value": "0.00"}, {"percentage": "2.6", "value": "0.00"}],
                      total_net="0.00", total_taxes="0.00", total_gross="0.00", total="0.00")
        doc, differences, totals, _rate = isl._document("Delivery Note", zero, LOOKUPS)
        # the third row is the title line (a text position), which has no price to take out
        self.assertEqual([r["qty"] for r in doc["items"]], [2.0, 1.0, 1.0])
        self.assertTrue(all(r["rate"] == 0.0 and r["price_list_rate"] == 0.0 for r in doc["items"]))
        self.assertEqual(totals, (Decimal("0"), Decimal("0"), Decimal("0")))
        self.assertEqual(len(differences), 1)
        self.assertTrue(differences[0].startswith("bexio's total is zero"))

    def test_a_delivery_with_a_non_zero_total_keeps_its_prices(self):
        doc, _differences, _totals, _rate = isl._document("Delivery Note", record(INVOICE, id=804), LOOKUPS)
        self.assertEqual([r["rate"] for r in doc["items"]], [50.0, 50.0, 0.0])


class Differences(unittest.TestCase):
    def test_a_bexio_tax_that_differs_is_reported_and_not_adjusted(self):
        off = record(INVOICE, taxs=[{"percentage": "8.1", "value": "8.11"}, {"percentage": "2.6", "value": "1.30"}])
        doc, differences, _totals, _rate = isl._document("Sales Invoice", off, LOOKUPS)
        self.assertEqual(differences, ["tax 8.1% +0.01"])
        self.assertEqual(doc["taxes"][0]["tax_amount"], 8.1)

    def test_a_total_up_to_five_rappen_off_goes_into_the_last_tax_row(self):
        off = record(INVOICE, total="159.45")
        doc, differences, totals, _rate = isl._document("Sales Invoice", off, LOOKUPS)
        self.assertEqual(doc["taxes"][-1]["tax_amount"], 1.35)
        self.assertEqual(totals[2], Decimal("159.45"))
        self.assertIn("total +0.05 taken into the last tax row", differences)

    def test_a_total_more_than_five_rappen_off_is_unmapped(self):
        with self.assertRaises(isl.Unmapped):
            isl.sales_invoice(record(INVOICE, total="159.46"), LOOKUPS)


class Rounding(unittest.TestCase):
    def test_the_total_is_bexio_total_not_the_gross_before_the_discounts(self):
        # bexio's total_gross is the sum before the discounts; the total is what is owed
        before = record(INVOICE, total_gross="170.00", total_rounding_difference="-0.02")
        _doc, differences, totals, _rate = isl._document("Sales Invoice", before, LOOKUPS)
        self.assertEqual(differences, [])
        self.assertEqual(totals[2], Decimal("159.40"))


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
        positions[0]["type"] = "KbPositionSomethingNew"
        self.assertUnmapped(record(INVOICE, positions=positions))

    def test_article_without_an_item(self):
        positions = copy.deepcopy(INVOICE["positions"])
        positions[0]["article_id"] = 8
        self.assertUnmapped(record(INVOICE, positions=positions))

    def test_unknown_currency(self):
        self.assertUnmapped(record(INVOICE, currency_id=9))


    def test_net_prices_are_mapped(self):
        self.assertEqual(isl.sales_invoice(record(INVOICE, mwst_is_net=True), LOOKUPS)["bexio_id"], "500")


class PositionsAndDiscounts(unittest.TestCase):
    def test_a_subtotal_row_is_left_out(self):
        positions = copy.deepcopy(INVOICE["positions"])
        positions.insert(2, {"type": "KbPositionSubtotal", "text": "<strong>Subtotal</strong>"})
        doc = isl.sales_invoice(record(INVOICE, positions=positions), LOOKUPS)
        self.assertEqual(len(doc["items"]), 3)

    def test_a_position_discount_goes_to_the_discount_fields_and_the_rate_is_erpnexts(self):
        positions = copy.deepcopy(INVOICE["positions"])
        positions[0]["discount_in_percent"] = "10"   # 2 x 50.00 less 10 % = 90.00 for the line
        doc, differences, totals, _rate = isl._document("Sales Invoice", record(INVOICE, positions=positions,
            total_net="140.00", taxs=[{"percentage": "8.1", "value": "7.29"}, {"percentage": "2.6", "value": "1.30"}],
            total_taxes="8.59", total="148.59"), LOOKUPS)
        row = doc["items"][0]
        self.assertEqual((row["price_list_rate"], row["discount_percentage"], row["rate"]), (50.0, 10.0, 45.0))
        self.assertEqual(totals[0], Decimal("140.00"))
        self.assertEqual(differences, [])

    def test_a_document_discount_is_the_lines_less_bexios_net_and_the_taxes_are_on_what_is_left(self):
        positions = copy.deepcopy(INVOICE["positions"])
        positions.append({"type": "KbPositionDiscount", "text": "Rabatt"})
        # the lines are 150.00 and the discount takes 10.00: 93.33 at 8.1 % and 46.67 at 2.6 %
        doc, differences, totals, _rate = isl._document("Sales Invoice", record(INVOICE, positions=positions,
            total_net="140.00", taxs=[{"percentage": "8.1", "value": "7.56"}, {"percentage": "2.6", "value": "1.21"}],
            total_taxes="8.77", total="148.77"), LOOKUPS)
        self.assertEqual(doc["discount_amount"], 10.0)
        self.assertEqual(doc["apply_discount_on"], "Net Total")
        self.assertEqual([t["tax_amount"] for t in doc["taxes"]], [7.56, 1.21])
        self.assertEqual(totals, (Decimal("140.00"), Decimal("8.77"), Decimal("148.77")))
        self.assertEqual(differences, [])

    def test_a_document_discount_that_does_not_reduce_the_net_is_unmapped(self):
        positions = copy.deepcopy(INVOICE["positions"])
        positions.append({"type": "KbPositionDiscount", "text": "Discount"})
        with self.assertRaises(isl.Unmapped):
            isl.sales_invoice(record(INVOICE, positions=positions, total_net="200.00"), LOOKUPS)

    def test_a_line_without_a_tax_id_gets_no_tax_row(self):
        positions = copy.deepcopy(INVOICE["positions"])
        positions[1]["tax_id"] = None
        doc, _differences, _totals, _rate = isl._document("Sales Invoice", record(INVOICE, positions=positions,
            taxs=[{"percentage": "8.1", "value": "8.10"}], total_net="150.00", total_taxes="8.10", total="158.10"), LOOKUPS)
        self.assertEqual([t["description"] for t in doc["taxes"]], ["USt 8.1% Normal"])

    def test_prices_including_vat_take_the_tax_out_of_the_gross_and_are_marked_included(self):
        gross = record(INVOICE, mwst_is_net=False, positions=[
            {"type": "KbPositionCustom", "amount": "3", "unit_price": "108.10", "account_id": 30, "tax_id": 28, "text": "A"}],
            taxs=[{"percentage": "8.1", "value": "24.30"}], total_net="300.00", total_taxes="24.30", total="324.30")
        doc, differences, totals, _rate = isl._document("Sales Invoice", gross, LOOKUPS)
        # ERPNext takes the VAT out of the included prices itself: a rate row, not an amount
        self.assertEqual([(t["charge_type"], t["rate"], t["included_in_print_rate"]) for t in doc["taxes"]],
                         [("On Net Total", 8.1, 1)])
        self.assertNotIn("tax_amount", doc["taxes"][0])
        self.assertEqual(totals, (Decimal("300.00"), Decimal("24.30"), Decimal("324.30")))
        self.assertEqual(differences, [])

    def test_a_total_that_differs_with_included_prices_by_more_than_the_tolerance_is_unmapped(self):
        gross = record(INVOICE, mwst_is_net=False, positions=[
            {"type": "KbPositionCustom", "amount": "3", "unit_price": "108.10", "account_id": 30, "tax_id": 28, "text": "A"}],
            taxs=[{"percentage": "8.1", "value": "24.30"}], total_net="300.00", total_taxes="24.30", total="324.36")
        with self.assertRaisesRegex(isl.Unmapped, "total differs from bexio's by \\+0.06"):
            isl.sales_invoice(gross, LOOKUPS)

    def test_a_credit_note_with_included_prices_is_unmapped(self):
        credit = record(INVOICE, id=801, invoice_id=500, mwst_is_net=False, positions=[
            {"type": "KbPositionCustom", "amount": "1", "unit_price": "108.10", "account_id": 30, "tax_id": 28, "text": "A"}],
            taxs=[], total="108.10")
        with self.assertRaisesRegex(isl.Unmapped, "credit note with prices including the VAT"):
            isl.credit_note(credit, LOOKUPS)

    def test_a_zero_rate_row_keeps_its_zero_price_list_rate_so_ERPNext_does_not_fill_it(self):
        doc = isl.sales_invoice(INVOICE, LOOKUPS)
        self.assertEqual(doc["items"][2]["price_list_rate"], 0.0)
        self.assertNotIn("price_list_rate", doc["items"][0])

    def test_a_fraction_on_the_free_text_item_is_one_unit_at_its_amount_with_the_quantity_in_the_text(self):
        frac = record(INVOICE, positions=[
            {"type": "KbPositionCustom", "amount": "1.58", "unit_price": "1000.00", "account_id": 30, "tax_id": 28,
             "discount_in_percent": "10", "text": "Beratung"}],
            taxs=[{"percentage": "8.1", "value": "115.18"}], total_net="1422.00", total_taxes="115.18", total="1537.18")
        doc, differences, totals, _rate = isl._document("Sales Invoice", frac, LOOKUPS)
        self.assertEqual((doc["items"][0]["qty"], doc["items"][0]["rate"]), (1.0, 1422.0))
        self.assertEqual(doc["items"][0]["description"], "1.58 x 1000.00 less 10%: Beratung")
        self.assertNotIn("discount_percentage", doc["items"][0])
        self.assertEqual(totals, (Decimal("1422.00"), Decimal("115.18"), Decimal("1537.18")))
        self.assertEqual(differences, [])

    def test_a_discount_on_the_free_text_item_is_kept_in_the_text_not_in_the_discount_fields(self):
        # ERPNext would refill the free-text item's price on save and drop the discount: the rate is the net
        doc, differences, totals, _rate = isl._document("Sales Invoice", record(INVOICE, positions=[
            {"type": "KbPositionCustom", "amount": "2", "unit_price": "100.00", "discount_in_percent": "10",
             "account_id": 30, "tax_id": 28, "text": "Material"}],
            taxs=[{"percentage": "8.1", "value": "16.20"}], total_net="180.00", total_taxes="14.58", total="194.58"),
            LOOKUPS)
        self.assertEqual((doc["items"][0]["qty"], doc["items"][0]["rate"]), (1.0, 180.0))
        self.assertEqual(doc["items"][0]["description"], "2 x 100.00 less 10%: Material")
        self.assertNotIn("discount_percentage", doc["items"][0])
        self.assertIn("tax 8.1% +1.62", differences)

    def test_a_quantity_of_more_than_three_places_is_one_unit_at_its_amount(self):
        doc = isl.sales_invoice(record(INVOICE, positions=[
            {"type": "KbPositionArticle", "article_id": 7, "amount": "4.625346", "unit_price": "100.00",
             "account_id": 30, "tax_id": 28, "text": "Beratung"}],
            taxs=[{"percentage": "8.1", "value": "37.50"}], total_net="462.53", total_taxes="37.50", total="500.03"),
            LOOKUPS)
        self.assertEqual((doc["items"][0]["qty"], doc["items"][0]["rate"]), (1.0, 462.53))
        self.assertEqual(doc["items"][0]["description"], "4.625346 x 100.00: Beratung")

    def test_a_fraction_on_an_article_keeps_its_quantity(self):
        doc = isl.sales_invoice(record(INVOICE, positions=[
            {"type": "KbPositionArticle", "article_id": 7, "amount": "1.5", "unit_price": "50.00",
             "account_id": 30, "tax_id": 28, "text": "Beratung"}],
            taxs=[{"percentage": "8.1", "value": "6.08"}], total_net="75.00", total_taxes="6.08", total="81.08"), LOOKUPS)
        self.assertEqual(doc["items"][0]["qty"], 1.5)

    def test_a_credit_note_with_a_document_discount_is_unmapped(self):
        credit = record(INVOICE, id=801, invoice_id=500, positions=copy.deepcopy(INVOICE["positions"]) + [
            {"type": "KbPositionDiscount", "text": "x"}])
        with self.assertRaises(isl.Unmapped):
            isl.credit_note(credit, LOOKUPS)

    def test_the_invoice_carries_its_posting_time(self):
        self.assertEqual(isl.sales_invoice(INVOICE, LOOKUPS)["set_posting_time"], 1)


class FieldCheck(unittest.TestCase):
    def test_a_field_the_doctype_does_not_have_is_named(self):
        doc = isl.sales_invoice(INVOICE, LOOKUPS)
        metas = {
            "Sales Invoice": set(doc) - {"remarks", "doctype"} | {"items", "taxes"},
            "Sales Invoice Item": {"item_code", "description", "qty", "rate", "amount", "income_account", "price_list_rate"},
            "Sales Taxes and Charges": {"charge_type", "account_head", "description", "tax_amount"},
        }
        self.assertEqual(isl.unknown_fields(doc, metas), ["Sales Invoice.remarks"])

    def test_sales_order_and_quotation_carry_no_remarks_and_no_row_income_account(self):
        # ERPNext's Sales Order and Quotation have neither field; the bexio id keeps the link
        for doc in (isl.sales_order(INVOICE, LOOKUPS), isl.quotation(INVOICE, LOOKUPS)):
            self.assertNotIn("remarks", doc)
            self.assertTrue(all("income_account" not in row for row in doc["items"]))
        self.assertEqual(isl.sales_invoice(INVOICE, LOOKUPS)["remarks"], "bexio Nr. RE-1001")
        self.assertIn("income_account", isl.sales_invoice(INVOICE, LOOKUPS)["items"][0])


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


class FakeMeta:
    """Stands in for import_master.Erp: records the reads and answers the meta with invented fields."""

    def __init__(self, fields):
        self.fields = fields
        self.reads = []

    def meta(self, doctype):
        self.reads.append(("meta", doctype))
        return {"name": doctype, "fields": [{"fieldname": f} for f in self.fields]}

    def get(self, doctype, name):
        self.reads.append(("get", doctype))
        raise AssertionError("the DocType resource needs System Manager; the meta is read instead")

    def list(self, doctype, filters=None, fields=("name",)):
        self.reads.append(("list", doctype))
        raise AssertionError("the Custom Field list needs System Manager; the meta holds the custom fields")


class DoctypeFields(unittest.TestCase):
    def test_fields_come_from_the_meta_custom_fields_included(self):
        erp = FakeMeta(["customer", "remarks", "bexio_id"])
        self.assertEqual(isl.doctype_fields(erp, "Sales Invoice"), {"customer", "remarks", "bexio_id"})

    def test_no_read_of_the_doctype_resource_or_the_custom_field_list(self):
        erp = FakeMeta(["customer"])
        isl.doctype_fields(erp, "Sales Invoice")
        self.assertEqual(erp.reads, [("meta", "Sales Invoice")])


class EcbRate(unittest.TestCase):
    """A foreign invoice without bexio's rate takes the ECB's rate of its date, and says so."""

    def eur(self, **changes):
        return record(INVOICE, currency_id=2, **changes)

    def test_the_ecb_rate_of_the_document_date_is_used_and_listed(self):
        seen = []

        def rate_at(code, day):
            seen.append((code, day))
            return Decimal("0.90"), day

        doc, differences, _totals, rate = isl._document("Sales Invoice", self.eur(), lookups(rate_at=rate_at))
        self.assertEqual(seen, [("EUR", "2024-03-01")])
        self.assertEqual((doc["currency"], doc["conversion_rate"], doc["items"][0]["rate"]), ("CHF", 1.0, 45.0))
        self.assertIn("exchange rate 0.90 for EUR: bexio gives none, the ECB's of 2024-03-01 is used", differences)
        self.assertTrue(doc["remarks"].endswith("; bexio: EUR 159.40 @ 0.90 (ECB 2024-03-01)"))

    def test_no_ecb_rate_is_unmapped(self):
        def rate_at(code, day):
            raise isl.Unmapped("no ECB rate for {} on {}".format(code, day))

        with self.assertRaisesRegex(isl.Unmapped, "no ECB rate"):
            isl.sales_invoice(self.eur(), lookups(rate_at=rate_at))

    def test_bexio_rate_is_kept_and_the_ecb_is_not_asked(self):
        def rate_at(code, day):
            raise AssertionError("bexio gave a rate: no ECB lookup")

        doc = isl.sales_invoice(self.eur(exchange_rate="0.94"), lookups(rate_at=rate_at))
        self.assertEqual(doc["conversion_rate"], 1.0)
        self.assertTrue(doc["remarks"].endswith("; bexio: EUR 159.40 @ 0.94"))


class UnchargedInvoice(unittest.TestCase):
    """bexio charged no VAT on the document, though its positions carry codes: imported untaxed, as bexio charged it."""

    def setUp(self):
        self.uncharged = record(INVOICE, taxs=[], total_taxes="0.0000", total_gross="150.00", total="150.00")

    def test_untaxed_with_the_codes_listed_and_a_remark(self):
        doc, differences, totals, _rate = isl._document("Sales Invoice", self.uncharged, LOOKUPS)
        self.assertEqual(doc["taxes"], [])
        self.assertEqual(totals, (Decimal("150.00"), Decimal("0"), Decimal("150.00")))
        self.assertIn("tax 8.1% -8.10", differences)
        self.assertIn("bexio charged no VAT: imported untaxed, as bexio charged it", differences)
        self.assertTrue(doc["remarks"].endswith("; bexio: no VAT charged although positions carry a VAT code"))

    def test_a_document_bexio_charged_tax_on_is_not_touched(self):
        doc = isl.sales_invoice(INVOICE, LOOKUPS)
        self.assertNotIn("VAT charged", doc["remarks"])
        self.assertEqual(len(doc["taxes"]), 2)


class RundungAndGrandTotalDiscount(unittest.TestCase):
    """A total up to 5 rappen off, where no VAT row takes it: a Rundung line when bexio's total is larger, a
    grand-total discount when it is smaller. Both make ERPNext's grand total bexio's, to the rappen."""

    def gross(self, total):
        # two lines of 108.10 with the VAT in them: 324.30 with 8.1% VAT of 24.30 taken out
        return record(INVOICE, mwst_is_net=False, positions=[
            {"type": "KbPositionCustom", "amount": "3", "unit_price": "108.10", "account_id": 30, "tax_id": 28, "text": "A"}],
            taxs=[{"percentage": "8.1", "value": "24.30"}], total_net="300.00", total_taxes="24.30", total=total)

    def untaxed(self, total):
        return record(INVOICE, taxs=[], total_taxes="0.0000", total_gross="150.00", total=total)

    def discounted(self, total):
        # the lines are 150 untaxed; a document discount of 10 leaves a net of 140
        return record(self.untaxed(total), total_net="140.00", positions=INVOICE["positions"] + [
            {"type": "KbPositionDiscount", "text": "Rabatt"}])

    def test_included_prices_three_rappen_more_are_a_rundung_line_on_the_first_income_account(self):
        doc, differences, totals, _rate = isl._document("Sales Invoice", self.gross("324.33"), LOOKUPS)
        rundung = doc["items"][-1]
        self.assertEqual((rundung["item_code"], rundung["description"], rundung["qty"], rundung["rate"]),
                         ("bexio Position", "Rundung (bexio Total)", 1.0, 0.03))
        self.assertEqual(rundung["income_account"], "3200 - Honorare - bic")
        self.assertNotIn("item_tax_template", rundung)
        self.assertEqual(sum(Decimal(str(r["rate"])) * Decimal(str(r["qty"])) for r in doc["items"]), Decimal("324.33"))
        self.assertEqual([(t["charge_type"], t["rate"]) for t in doc["taxes"]], [("On Net Total", 8.1)])
        self.assertEqual(totals[2], Decimal("324.33"))
        self.assertIn("total +3 rappen absorbed in a Rundung line", differences)
        self.assertNotIn("discount_amount", doc)

    def test_included_prices_three_rappen_less_are_a_grand_total_discount(self):
        doc, differences, _totals, _rate = isl._document("Sales Invoice", self.gross("324.27"), LOOKUPS)
        self.assertEqual((doc["apply_discount_on"], doc["discount_amount"]), ("Grand Total", 0.03))
        self.assertEqual(len(doc["items"]), 1)
        self.assertIn("total -3 rappen absorbed as a grand-total discount", differences)

    def test_untaxed_two_rappen_less_is_a_grand_total_discount_and_no_rundung_line(self):
        doc, differences, totals, _rate = isl._document("Sales Invoice", self.untaxed("149.98"), LOOKUPS)
        self.assertEqual((doc["apply_discount_on"], doc["discount_amount"]), ("Grand Total", 0.02))
        self.assertEqual(len(doc["items"]), 3)
        self.assertEqual(doc["taxes"], [])
        self.assertEqual(totals[2], Decimal("149.98"))
        self.assertIn("total -2 rappen absorbed as a grand-total discount", differences)

    def test_untaxed_three_rappen_more_is_a_rundung_line(self):
        doc, differences, totals, _rate = isl._document("Sales Invoice", self.untaxed("150.03"), LOOKUPS)
        self.assertEqual(doc["items"][-1]["rate"], 0.03)
        self.assertNotIn("apply_discount_on", doc)
        self.assertEqual(totals[2], Decimal("150.03"))
        self.assertIn("total +3 rappen absorbed in a Rundung line", differences)

    def test_a_difference_above_the_tolerance_stays_unmapped_in_both_cases(self):
        with self.assertRaisesRegex(isl.Unmapped, "total differs from bexio's by \\+0.06"):
            isl.sales_invoice(self.untaxed("150.06"), LOOKUPS)
        with self.assertRaisesRegex(isl.Unmapped, "total differs from bexio's by -0.06"):
            isl.sales_invoice(self.gross("324.24"), LOOKUPS)

    def test_a_document_discount_with_a_grand_total_discount_is_unmapped(self):
        # the net is 140; bexio's total is two rappen less than that, and ERPNext has one discount field per document
        with self.assertRaisesRegex(isl.Unmapped, "document discount and a total difference"):
            isl.sales_invoice(self.discounted("139.98"), LOOKUPS)

    def test_a_document_discount_with_a_rundung_line_keeps_the_discount_and_adds_the_line(self):
        doc, _differences, totals, _rate = isl._document("Sales Invoice", self.discounted("140.03"), LOOKUPS)
        self.assertEqual((doc["apply_discount_on"], doc["discount_amount"]), ("Net Total", 10.0))
        self.assertEqual(doc["items"][-1]["rate"], 0.03)
        self.assertEqual(totals[2], Decimal("140.03"))

    def test_a_credit_note_with_a_difference_and_no_vat_row_is_unmapped(self):
        credit = record(INVOICE, id=802, invoice_id=500, taxs=[], total_taxes="0.0000", total="149.98")
        with self.assertRaisesRegex(isl.Unmapped, "nothing to take a total difference"):
            isl.credit_note(credit, LOOKUPS)

    def test_an_order_and_an_offer_take_a_rappen_difference_as_the_invoices_do(self):
        # no VAT row takes it, so the order and the offer get a grand-total discount or a Rundung item, as the invoices
        doc, differences, totals, _rate = isl._document("Sales Order", self.untaxed("149.98"), LOOKUPS)
        self.assertEqual((doc["apply_discount_on"], doc["discount_amount"]), ("Grand Total", 0.02))
        self.assertEqual(totals[2], Decimal("149.98"))
        self.assertIn("total -2 rappen absorbed as a grand-total discount", differences)
        self.assertNotIn("income_account", doc["items"][0])
        doc, differences, totals, _rate = isl._document("Quotation", self.gross("324.33"), LOOKUPS)
        rundung = doc["items"][-1]
        self.assertEqual((rundung["item_code"], rundung["description"], rundung["rate"]),
                         ("bexio Position", "Rundung (bexio Total)", 0.03))
        self.assertNotIn("income_account", rundung)
        self.assertEqual(totals[2], Decimal("324.33"))
        self.assertIn("total +3 rappen absorbed in a Rundung line", differences)

    def test_an_order_or_offer_with_a_difference_above_the_tolerance_stays_unmapped(self):
        # an offer whose bexio total is far from its lines stays listed: a plug that size is not rounding
        with self.assertRaisesRegex(isl.Unmapped, "total differs from bexio's by -4\\.80"):
            isl._document("Quotation", self.untaxed("145.20"), LOOKUPS)
        with self.assertRaisesRegex(isl.Unmapped, "total differs from bexio's by \\+0.06"):
            isl._document("Sales Order", self.untaxed("150.06"), LOOKUPS)

    def test_a_delivery_note_with_the_same_difference_stays_unmapped(self):
        with self.assertRaisesRegex(isl.Unmapped, "nothing to take a total difference"):
            isl._document("Delivery Note", self.untaxed("149.98"), LOOKUPS)

    def test_an_exact_untaxed_total_has_no_rundung_line_and_no_discount(self):
        doc, differences, _totals, _rate = isl._document("Sales Invoice", self.untaxed("150.00"), LOOKUPS)
        self.assertEqual(len(doc["items"]), 3)
        self.assertNotIn("apply_discount_on", doc)
        self.assertFalse([d for d in differences if "absorbed" in d])


class Drafts(unittest.TestCase):
    """What --apply hands the loader: invoices named by bexio's number, the ECB rates they use, nothing else."""

    def results(self, invoices, journal=(), **kinds):
        data = {"invoices": invoices, "credit_vouchers": [], "orders": [], "offers": [],
                "accounts": JournalCheck.ACCOUNTS, "journal": list(journal)}
        data.update(kinds)
        return isl.plan(data, lookups(rate_at=lambda code, day: (Decimal("0.90"), day)))

    def write(self, results):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "drafts.json")
            count = isl.write_drafts(results, path)
            with open(path, encoding="utf-8") as f:
                return count, json.load(f)

    def test_each_invoice_is_a_draft_named_by_bexios_number(self):
        count, out = self.write(self.results([INVOICE]))
        self.assertEqual(count, 1)
        (draft,) = out["documents"]
        self.assertEqual((draft["doctype"], draft["name"], draft["bexio_id"]), ("Sales Invoice", "RE-1001", "500"))
        self.assertNotIn("doctype", draft["values"])
        self.assertEqual(out["exchange_rates"], [])

    def test_a_foreign_invoice_is_a_chf_draft_and_hands_over_no_exchange_rate(self):
        journal = JournalCheck().journal(203.23)
        count, out = self.write(self.results([EUR_INVOICE], journal=journal))
        self.assertEqual(count, 1)
        (draft,) = out["documents"]
        self.assertEqual((draft["values"]["currency"], draft["values"]["conversion_rate"]), ("CHF", 1.0))
        self.assertEqual(out["exchange_rates"], [])

    def test_an_unmapped_invoice_is_left_out(self):
        bad = record(INVOICE, id=502, document_nr="RE-1003", contact_id=999)
        count, out = self.write(self.results([INVOICE, bad]))
        self.assertEqual(count, 1)
        self.assertEqual([d["name"] for d in out["documents"]], ["RE-1001"])

    def test_orders_and_offers_are_drafts_of_their_doctype_and_an_unmapped_one_is_left_out(self):
        # the order and the offer carry the invoice's positions; the unmapped order has a contact with no Customer
        order = record(INVOICE, id=900, document_nr="AB-1")
        order_bad = record(INVOICE, id=901, document_nr="AB-2", contact_id=999)
        offer = record(INVOICE, id=950, document_nr="AN-1")
        count, out = self.write(self.results([], orders=[order, order_bad], offers=[offer]))
        self.assertEqual(count, 2)
        self.assertEqual(sorted((d["doctype"], d["name"], d["bexio_id"]) for d in out["documents"]),
                         [("Quotation", "AN-1", "950"), ("Sales Order", "AB-1", "900")])

    def test_a_delivery_is_a_delivery_note_draft_named_by_its_number(self):
        delivery = record(INVOICE, id=960, document_nr="LI-1")
        delivery_bad = record(INVOICE, id=961, document_nr="LI-2", contact_id=999)
        count, out = self.write(self.results([], deliveries=[delivery, delivery_bad]))
        self.assertEqual(count, 1)
        (draft,) = out["documents"]
        self.assertEqual((draft["doctype"], draft["name"], draft["bexio_id"]), ("Delivery Note", "LI-1", "960"))
        self.assertEqual(draft["values"]["posting_date"], "2024-03-01")


if __name__ == "__main__":
    unittest.main()
