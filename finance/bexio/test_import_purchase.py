"""Offline tests for import_purchase.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import io
import unittest
from decimal import Decimal

import import_master as im
import import_purchase as ip

# Item Tax Templates by bexio tax id (invented names; one template per bexio code, keyed by its bexio_id)
TAXES = {
    "35": "Test MWST bexio 35", "38": "Test MWST bexio 38", "37": "Test MWST bexio 37",
    "22": "Test MWST bexio 22", "32": "Test BZB81 bexio 32",
}


def lookups(taxes=TAXES):
    return ip.Lookups(
        suppliers={"901": "Lieferant Test AG"},
        accounts={"5001": "5001 - Testaufwand - bic", "5002": "5002 - Testmaterial - bic"},
        account_by_number={"1170": "1170 - Vorsteuer Material/DL - bic", "1171": "1171 - Vorsteuer Invest. - bic", "1172": "1172 - Vorsteuer transitorisch - bic",
                           "2202": "2202 - Umsatzsteuerausgleich - bic"},
        taxes=dict(taxes),
    )


def pos(amount, price, tax_id, account="5001", text="Testposition"):
    return {"text": text, "amount": amount, "unit_price": price, "tax_id": tax_id, "booking_account_id": account}


def bill(bid="b-1", positions=None, **extra):
    return dict({
        "id": bid, "contact_id": 901, "currency_code": "CHF", "bill_date": "2025-03-10", "due_date": "2025-04-09",
        "document_no": "R-1", "vendor_ref": "LF-1", "net": 380, "gross": 402.85, "attachment_ids": ["a-1", "a-2"],
        "positions": positions if positions is not None else [
            pos(2, 100, 35),                      # 8.1 % Material/DL: 200.00, tax 16.20
            pos(1, 50, 38, account="5002"),       # 8.1 % Invest./Aufwand: 50.00, tax 4.05
            pos(1, 30, 47),                       # 0 %: 30.00, no tax row
            pos(1, 100, 37, account="5002"),      # 2.6 % Invest./Aufwand: 100.00, tax 2.60
        ],
    }, **extra)


def taxes(doc):
    return {(t["account_head"], t["description"]): t["tax_amount"] for t in doc["taxes"]}


class MapBillTest(unittest.TestCase):
    def test_multi_line_bill_with_mixed_vat(self):
        doc = ip.map_bill(bill(), lookups())
        self.assertEqual(doc["doctype"], "Purchase Invoice")
        self.assertEqual(doc["supplier"], "Lieferant Test AG")
        self.assertEqual(doc["bill_no"], "LF-1")
        self.assertEqual((doc["posting_date"], doc["due_date"]), ("2025-03-10", "2025-04-09"))
        self.assertEqual(doc["bexio_id"], "b-1")
        self.assertEqual([r["amount"] for r in doc["items"]], [200.0, 50.0, 30.0, 100.0])
        self.assertEqual([r["expense_account"] for r in doc["items"]],
                         ["5001 - Testaufwand - bic", "5002 - Testmaterial - bic", "5001 - Testaufwand - bic", "5002 - Testmaterial - bic"])
        self.assertEqual(taxes(doc), {
            ("1172 - Vorsteuer transitorisch - bic", "Test MWST bexio 35"): 16.2,
            ("1172 - Vorsteuer transitorisch - bic", "Test MWST bexio 38"): 4.05,
            ("1172 - Vorsteuer transitorisch - bic", "Test MWST bexio 37"): 2.6,
        })
        self.assertEqual(ip.document_totals(doc), (Decimal("380.00"), Decimal("22.85"), Decimal("402.85")))

    def test_attachments_are_kept_for_the_file_step(self):
        doc = ip.map_bill(bill(), lookups())
        self.assertEqual(doc[ip.ATTACHMENTS_FIELD], "a-1,a-2")

    def test_foreign_currency_is_booked_in_chf_at_bexios_rate_with_the_original_in_the_remarks(self):
        doc = ip.map_bill(bill(currency_code="EUR", exchange_rate=0.93, gross=216.20, positions=[pos(1, 200, 35)]), lookups())
        self.assertEqual((doc["currency"], doc["conversion_rate"]), ("CHF", 1.0))
        self.assertEqual(doc["remarks"], "bexio: EUR 216.20 @ 0.93")
        self.assertEqual(ip.document_totals(doc), (Decimal("186.00"), Decimal("15.07"), Decimal("201.07")))
        self.assertEqual(taxes(doc), {("1172 - Vorsteuer transitorisch - bic", "Test MWST bexio 35"): 15.07})

    def test_chf_bill_has_no_remarks(self):
        self.assertNotIn("remarks", ip.map_bill(bill(), lookups()))

    def test_zero_rate_line_has_no_tax_row(self):
        doc = ip.map_bill(bill(positions=[pos(1, 30, 47)]), lookups())
        self.assertEqual(doc["taxes"], [])
        self.assertEqual(ip.document_totals(doc), (Decimal("30.00"), Decimal("0"), Decimal("30.00")))

    def test_tax_is_worked_out_per_line_to_the_rappen(self):
        # 0.333 x 8.1 % = 0.027 per line, which is 0.03 when rounded per line
        doc = ip.map_bill(bill(positions=[pos("0.333", 1, 35), pos("0.333", 1, 35)]), lookups())
        self.assertEqual(taxes(doc), {("1172 - Vorsteuer transitorisch - bic", "Test MWST bexio 35"): 0.06})


class MapBillErrorTest(unittest.TestCase):
    def assertUnmapped(self, record, words, known=lookups()):
        with self.assertRaisesRegex(ip.MappingError, words):
            ip.map_bill(record, known)

    def test_unknown_vat_code_is_reported(self):
        self.assertUnmapped(bill(positions=[pos(1, 10, 16)]), "unknown purchase VAT code 16")

    def test_template_is_found_by_bexio_id_and_never_by_rate(self):
        # only code 22 (7.7 %) has a template: a line with code 35 (8.1 %) must not take it by its rate
        self.assertUnmapped(bill(positions=[pos(1, 10, 35)]), "no Item Tax Template with bexio_id 35",
                            lookups(taxes={"22": "Test MWST bexio 22"}))

    def test_missing_item_tax_template_is_reported(self):
        self.assertUnmapped(bill(positions=[pos(1, 10, 35)]), "no Item Tax Template with bexio_id 35", lookups(taxes={}))

    def test_unknown_booking_account_is_reported(self):
        self.assertUnmapped(bill(positions=[pos(1, 10, 35, account="9999")]), "no Account for booking account 9999")

    def test_unknown_supplier_is_reported(self):
        self.assertUnmapped(bill(contact_id=777), "no Supplier for contact 777")

    def test_bill_without_positions_is_reported(self):
        self.assertUnmapped(bill(positions=[]), "no positions")


def line(amount, tax_calc, tax_id, account=5001, title="Testzeile", **extra):
    """A bexio bill line as the full export carries it: gross amount when the prices include VAT, the VAT in tax_calc."""
    return dict({"amount": amount, "tax_calc": tax_calc, "tax_id": tax_id, "booking_account_id": account,
                 "title": title, "id": "l-1", "position": 0, "tax_man": 0.0}, **extra)


def full_bill(bid="b-9", lines=None, **extra):
    """A bill as bills.json has it: no contact_id but supplier_id, and the lines in line_items."""
    base = {"id": bid, "supplier_id": 901, "currency_code": "CHF", "bill_date": "2026-09-27",
            "due_date": "2026-10-07", "document_no": "R-2", "vendor_ref": "LF-2", "item_net": False,
            "attachment_ids": [], "exchange_rate": None,
            "line_items": lines if lines is not None else [line(129.7, 9.72, 38)],
            "net": 119.98, "gross": 129.7}
    base.update(extra)
    return base


class MapBillFromLineItemsTest(unittest.TestCase):
    def test_gross_lines_give_net_and_the_bexio_vat(self):
        doc = ip.map_bill(full_bill(), lookups())
        self.assertEqual(doc["supplier"], "Lieferant Test AG")
        self.assertEqual([r["amount"] for r in doc["items"]], [119.98])
        self.assertEqual(taxes(doc), {("1172 - Vorsteuer transitorisch - bic", "Test MWST bexio 38"): 9.72})
        self.assertEqual(ip.document_totals(doc), (Decimal("119.98"), Decimal("9.72"), Decimal("129.70")))

    def test_the_document_number_of_bexio_is_kept_and_the_vendor_ref_is_the_bill_number(self):
        doc = ip.map_bill(full_bill(), lookups())
        self.assertEqual(doc["bill_no"], "LF-2")
        self.assertEqual(doc["set_posting_time"], 1)

    def test_a_line_without_tax_id_has_no_tax_row(self):
        doc = ip.map_bill(full_bill(lines=[line(4720, 0, None)], net=4720, gross=4720), lookups())
        self.assertEqual(doc["taxes"], [])
        self.assertEqual(ip.document_totals(doc), (Decimal("4720.00"), Decimal("0"), Decimal("4720.00")))

    def test_a_zero_rate_import_line_has_no_tax_row(self):
        doc = ip.map_bill(full_bill(lines=[line(1867.5, 0, 10)]), lookups(taxes={}))
        self.assertEqual(doc["taxes"], [])

    def test_vat_that_does_not_match_the_rate_is_reported(self):
        with self.assertRaisesRegex(ip.MappingError, "is not 8.1%"):
            ip.map_bill(full_bill(lines=[line(129.7, 12.0, 38)]), lookups())

    def test_net_amounts_with_vat_other_than_reverse_charge_are_not_mapped(self):
        with self.assertRaisesRegex(ip.MappingError, "other than reverse charge"):
            ip.map_bill(full_bill(lines=[line(18000, 1458.0, 35)], item_net=True), lookups())

    def test_reverse_charge_is_the_vat_booked_twice_so_the_total_is_the_net(self):
        doc = ip.map_bill(full_bill(lines=[line(18000, 1458.0, 32)], item_net=True, net=18000, gross=18000), lookups())
        self.assertEqual([r["amount"] for r in doc["items"]], [18000.0])
        self.assertEqual(
            [(r["account_head"], r["add_deduct_tax"], r["tax_amount"]) for r in doc["taxes"]],
            [("1172 - Vorsteuer transitorisch - bic", "Add", 1458.0), ("2202 - Umsatzsteuerausgleich - bic", "Deduct", 1458.0)])
        self.assertEqual(ip.document_totals(doc), (Decimal("18000"), Decimal("0"), Decimal("18000")))

    def test_reverse_charge_without_the_template_is_reported(self):
        with self.assertRaisesRegex(ip.MappingError, "no Item Tax Template with bexio_id 33"):
            ip.map_bill(full_bill(lines=[line(1000, 81.0, 33)], item_net=True), lookups())

    def test_net_amounts_without_vat_are_mapped_as_they_are(self):
        doc = ip.map_bill(full_bill(lines=[line(18000, 0, None)], item_net=True), lookups())
        self.assertEqual(ip.document_totals(doc)[0], Decimal("18000"))

    def test_foreign_bill_needs_its_exchange_rate(self):
        with self.assertRaisesRegex(ip.MappingError, "no exchange rate for EUR"):
            ip.map_bill(full_bill(currency_code="EUR"), lookups())
        doc = ip.map_bill(full_bill(currency_code="EUR", exchange_rate=0.99, gross=129.7), lookups())
        self.assertEqual((doc["currency"], doc["conversion_rate"]), ("CHF", 1.0))
        self.assertEqual(doc["remarks"], "bexio: EUR 129.70 @ 0.99")

    def test_detail_positions_are_the_fallback_for_a_bill_without_line_items(self):
        record = bill()
        record.pop("line_items", None)
        self.assertEqual(len(ip.map_bill(record, lookups())["items"]), 4)


class MapExpenseTest(unittest.TestCase):
    def expense(self, **extra):
        return dict({"id": "e-1", "contact_id": 901, "currency_code": "CHF", "paid_on": "2025-05-02",
                     "gross": 0, "status": "draft", "booking_account_id": 5001, "tax_id": 47}, **extra)

    def test_draft_with_gross_zero_is_skipped(self):
        with self.assertRaisesRegex(ip.Skipped, "draft with gross 0"):
            ip.map_expense(self.expense(), lookups())

    def test_expense_with_gross_is_mapped(self):
        doc = ip.map_expense(self.expense(gross=45.5, status="paid"), lookups())
        self.assertEqual(doc["bill_date"], "2025-05-02")
        self.assertEqual(ip.document_totals(doc), (Decimal("45.5"), Decimal("0"), Decimal("45.5")))

    def test_expense_with_vat_is_skipped_for_the_expense_step(self):
        with self.assertRaisesRegex(ip.Skipped, "left to erp-a2ma"):
            ip.map_expense(self.expense(gross=108.1, status="paid", tax_id=35), lookups())


class DryRunTest(unittest.TestCase):
    def test_totals_per_year_and_currency_and_differences(self):
        good = bill("b-1", net=380, gross=402.85)
        off = bill("b-2", net=380, gross=402.86)  # bexio gross one rappen above what the lines give
        broken = bill("b-3", positions=[pos(1, 10, 16)], currency_code="EUR", net=10, gross=10.81)
        totals = ip.dry_run([good, off, broken], [self.draft_expense()], lookups(), {})
        bill_row = totals.rows[(2025, "CHF")]
        # the CHF row counts the skipped expense too: 2 bills and 1 expense
        self.assertEqual((bill_row["records"], bill_row["mapped"], bill_row["unmapped"], bill_row["differ"]), (3, 2, 0, 1))
        self.assertEqual(bill_row["gross"], Decimal("805.71"))
        self.assertEqual(bill_row["erp_gross"], Decimal("805.70"))
        eur = totals.rows[(2025, "EUR")]
        self.assertEqual((eur["mapped"], eur["unmapped"]), (0, 1))
        self.assertEqual(totals.rows[(2025, "CHF")]["skipped"], 1)
        self.assertTrue(any("bill b-2: differs" in p for p in totals.problems))
        self.assertTrue(any("bill b-3: unmapped" in p for p in totals.problems))

    def draft_expense(self):
        return {"id": "e-1", "contact_id": 901, "currency_code": "CHF", "paid_on": "2025-05-02",
                "gross": 0, "status": "draft", "booking_account_id": 5001, "tax_id": 47}

    def test_apply_and_dry_run_together_are_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ip.main(["--apply", "--dry-run"])


class DraftsTest(unittest.TestCase):
    def test_each_bill_that_maps_is_a_draft_named_by_bexios_document_number(self):
        (draft,) = ip.drafts([bill(bid="b-1", document_no="R-77")], lookups(), {})
        self.assertEqual((draft["doctype"], draft["name"], draft["bexio_id"]), ("Purchase Invoice", "R-77", "b-1"))
        self.assertEqual(draft["values"]["bill_no"], "LF-1")
        self.assertNotIn("doctype", draft["values"])

    def test_a_bill_that_does_not_map_or_whose_total_differs_is_left_out(self):
        good = bill(bid="b-1", document_no="R-1")
        off = bill(bid="b-2", document_no="R-2", net=380, gross=402.86)  # one rappen above what its lines give
        broken = bill(bid="b-3", document_no="R-3", positions=[pos(1, 10, 16)])
        self.assertEqual([d["name"] for d in ip.drafts([good, off, broken], lookups(), {})], ["R-1"])

    def test_a_foreign_bill_is_a_draft_when_its_chf_total_is_bexios_booking(self):
        foreign = bill(bid="b-f", document_no="R-F", currency_code="EUR", exchange_rate=0.93, gross=216.20,
                       positions=[pos(1, 200, 35)])
        (draft,) = ip.drafts([foreign], lookups(), {("KbBill", "b-f"): Decimal("-201.07")})
        self.assertEqual((draft["name"], draft["values"]["currency"]), ("R-F", "CHF"))

    def test_a_foreign_bill_whose_chf_total_is_not_bexios_booking_is_left_out(self):
        foreign = bill(bid="b-f", document_no="R-F", currency_code="EUR", exchange_rate=0.93, gross=216.20,
                       positions=[pos(1, 200, 35)])
        self.assertEqual(ip.drafts([foreign], lookups(), {("KbBill", "b-f"): Decimal("-201.20")}), [])
        self.assertEqual(ip.drafts([foreign], lookups(), {}), [])

    def test_the_dry_run_lists_a_foreign_bill_that_differs_from_bexios_chf_booking(self):
        foreign = bill(bid="b-f", currency_code="EUR", exchange_rate=0.93, gross=216.20, positions=[pos(1, 200, 35)])
        totals = ip.dry_run([foreign], [], lookups(), {("KbBill", "b-f"): Decimal("-201.20")})
        self.assertEqual(totals.rows[(2025, "EUR")]["differ"], 1)
        self.assertEqual(totals.problems, ["bill b-f: differs, CHF total differs from bexio's CHF booking on 2000 by -0.13"])

    def test_the_dry_run_passes_a_foreign_bill_that_is_bexios_chf_booking(self):
        foreign = bill(bid="b-f", currency_code="EUR", exchange_rate=0.93, gross=216.20, positions=[pos(1, 200, 35)])
        totals = ip.dry_run([foreign], [], lookups(), {("KbBill", "b-f"): Decimal("-201.07")})
        self.assertEqual((totals.rows[(2025, "EUR")]["mapped"], totals.rows[(2025, "EUR")]["differ"]), (1, 0))

    def test_two_bills_with_one_document_number_are_refused(self):
        with self.assertRaisesRegex(ip.MappingError, "document number R-1"):
            ip.drafts([bill(bid="b-1", document_no="R-1"), bill(bid="b-2", document_no="R-1")], lookups(), {})


if __name__ == "__main__":
    unittest.main()
