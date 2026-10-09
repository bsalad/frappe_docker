"""Offline tests for import_purchase.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import io
import unittest
from decimal import Decimal

import import_master as im
import import_purchase as ip
from test_import_master import FakeErp

# Item Tax Templates by bexio tax id (invented names; one template per bexio code, keyed by its bexio_id)
TAXES = {
    "35": "Test MWST bexio 35", "38": "Test MWST bexio 38", "37": "Test MWST bexio 37",
    "22": "Test MWST bexio 22",
}


def lookups(taxes=TAXES):
    return ip.Lookups(
        suppliers={"901": "Lieferant Test AG"},
        accounts={"5001": "5001 - Testaufwand - bic", "5002": "5002 - Testmaterial - bic"},
        account_by_number={"1170": "1170 - Vorsteuer Material/DL - bic", "1171": "1171 - Vorsteuer Invest. - bic"},
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
            ("1170 - Vorsteuer Material/DL - bic", "Test MWST bexio 35"): 16.2,
            ("1171 - Vorsteuer Invest. - bic", "Test MWST bexio 38"): 4.05,
            ("1171 - Vorsteuer Invest. - bic", "Test MWST bexio 37"): 2.6,
        })
        self.assertEqual(ip.document_totals(doc), (Decimal("380.00"), Decimal("22.85"), Decimal("402.85")))

    def test_attachments_are_kept_for_the_file_step(self):
        doc = ip.map_bill(bill(), lookups())
        self.assertEqual(doc[ip.ATTACHMENTS_FIELD], "a-1,a-2")

    def test_foreign_currency_keeps_currency_and_rate(self):
        doc = ip.map_bill(bill(currency_code="EUR", exchange_rate=0.93, positions=[pos(1, 200, 35)]), lookups())
        self.assertEqual(doc["currency"], "EUR")
        self.assertEqual(doc["conversion_rate"], 0.93)
        self.assertEqual(ip.document_totals(doc), (Decimal("200.00"), Decimal("16.20"), Decimal("216.20")))

    def test_zero_rate_line_has_no_tax_row(self):
        doc = ip.map_bill(bill(positions=[pos(1, 30, 47)]), lookups())
        self.assertEqual(doc["taxes"], [])
        self.assertEqual(ip.document_totals(doc), (Decimal("30.00"), Decimal("0"), Decimal("30.00")))

    def test_tax_is_worked_out_per_line_to_the_rappen(self):
        # 0.333 x 8.1 % = 0.027 per line, which is 0.03 when rounded per line
        doc = ip.map_bill(bill(positions=[pos("0.333", 1, 35), pos("0.333", 1, 35)]), lookups())
        self.assertEqual(taxes(doc), {("1170 - Vorsteuer Material/DL - bic", "Test MWST bexio 35"): 0.06})


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

    def test_expense_with_vat_needs_a_net_split_and_is_reported(self):
        with self.assertRaisesRegex(ip.MappingError, "net split"):
            ip.map_expense(self.expense(gross=108.1, status="paid", tax_id=35), lookups())


class DryRunTest(unittest.TestCase):
    def test_totals_per_year_and_currency_and_differences(self):
        good = bill("b-1", net=380, gross=402.85)
        off = bill("b-2", net=380, gross=402.86)  # bexio gross one rappen above what the lines give
        broken = bill("b-3", positions=[pos(1, 10, 16)], currency_code="EUR", net=10, gross=10.81)
        totals = ip.dry_run([good, off, broken], [self.draft_expense()], lookups())
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

    def test_main_takes_the_dry_run_only(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ip.main([]), 2)


class UpsertTest(unittest.TestCase):
    def setUp(self):
        self.erp = FakeErp({"Purchase Invoice": []})

    def test_first_run_creates_and_second_run_changes_nothing(self):
        doc = ip.map_bill(bill(), lookups())
        name = ip.upsert_purchase_invoice(self.erp, doc)
        self.assertIsNotNone(name)
        self.assertEqual(self.erp.writes, 1)
        self.assertEqual(ip.upsert_purchase_invoice(self.erp, ip.map_bill(bill(), lookups())), name)
        self.assertEqual(self.erp.writes, 1)

    def test_a_changed_bill_updates_the_same_document(self):
        name = ip.upsert_purchase_invoice(self.erp, ip.map_bill(bill(), lookups()))
        changed = ip.map_bill(bill(positions=[pos(1, 10, 35)]), lookups())
        self.assertEqual(ip.upsert_purchase_invoice(self.erp, changed), name)
        self.assertEqual(self.erp.docs["Purchase Invoice"][name]["items"][0]["amount"], 10.0)


if __name__ == "__main__":
    unittest.main()
