"""Tests of mwst_compare with invented figures: the quarter of a date, the two sides, and the comparison."""

import unittest
from decimal import Decimal

import mwst_compare as mc


class QuarterTest(unittest.TestCase):
    def test_quarter_of_a_date(self):
        self.assertEqual(mc.quarter_of("2026-01-01"), "2026Q1")
        self.assertEqual(mc.quarter_of("2026-03-31"), "2026Q1")
        self.assertEqual(mc.quarter_of("2026-04-01"), "2026Q2")
        self.assertEqual(mc.quarter_of("2026-12-31"), "2026Q4")


def line(date, amount, debit, credit, base=None, description="Invented posting"):
    return {"date": date, "amount": amount, "base_currency_amount": amount if base is None else base,
            "debit_account_id": debit, "credit_account_id": credit, "description": description}


class BexioSideTest(unittest.TestCase):
    NUMBERS = {127: "2200", 95: "1170", 96: "1171", 130: "2203", 200: "2202", 201: "1172"}

    def test_journal_lines_net_debit_minus_credit_per_quarter_and_gross_per_side(self):
        journal = [
            line("2026-02-10T00:00:00+01:00", 100, 1, 127),
            line("2026-03-20T00:00:00+01:00", 40, 127, 1),
            line("2026-05-02T00:00:00+02:00", 7.5, 96, 1),
        ]
        balances = mc.bexio_balances(journal, self.NUMBERS)
        self.assertEqual(balances, {
            ("2026Q1", "2200", "net"): Decimal("-60"),
            ("2026Q1", "2200", "gross"): Decimal("100"),
            ("2026Q2", "1171", "net"): Decimal("7.5"),
            ("2026Q2", "1171", "gross"): Decimal("7.5"),
        })

    def test_lines_on_other_accounts_are_left_out(self):
        journal = [line("2026-02-10", 100, 1, 2)]
        self.assertEqual(mc.bexio_balances(journal, self.NUMBERS), {})

    def test_transit_accounts_count_their_gross_moves_only(self):
        journal = [
            line("2026-02-10", 30, 200, 1),
            line("2026-02-10", 30, 1, 200),
            line("2026-02-11", 12, 96, 201),
        ]
        self.assertEqual(mc.bexio_balances(journal, self.NUMBERS), {
            ("2026Q1", "2202", "gross"): Decimal("30"),
            ("2026Q1", "1171", "net"): Decimal("12"),
            ("2026Q1", "1171", "gross"): Decimal("12"),
            ("2026Q1", "1172", "gross"): Decimal("12"),
        })

    def test_a_foreign_currency_line_counts_in_chf(self):
        journal = [line("2025-12-04", 250.5, 96, 1, base=204.17)]
        self.assertEqual(mc.bexio_balances(journal, self.NUMBERS), {
            ("2025Q4", "1171", "net"): Decimal("204.17"),
            ("2025Q4", "1171", "gross"): Decimal("204.17"),
        })

    def test_each_line_is_rounded_to_the_rappen_as_erpnext_posts_it(self):
        journal = [line("2021-04-01", 7.777777, 96, 1), line("2021-04-02", 7.777777, 96, 1)]
        self.assertEqual(mc.bexio_balances(journal, self.NUMBERS), {
            ("2021Q2", "1171", "net"): Decimal("15.56"),
            ("2021Q2", "1171", "gross"): Decimal("15.56"),
        })

    def test_a_carry_forward_on_1_january_is_left_out(self):
        journal = [line("2021-01-01", 88.88, 96, 273, description="provisorischer Saldovortrag")]
        self.assertEqual(mc.bexio_balances(journal, self.NUMBERS), {})

    def test_a_1_january_line_that_is_no_carry_forward_is_kept(self):
        journal = [line("2021-01-01", 10, 96, 1, description="Invented reclassification")]
        self.assertEqual(mc.bexio_balances(journal, self.NUMBERS), {
            ("2021Q1", "1171", "net"): Decimal("10"),
            ("2021Q1", "1171", "gross"): Decimal("10"),
        })


class ErpSideTest(unittest.TestCase):
    NAMES = {"2200 - Geschuldete MWST - bic": "2200", "1170 - Vorsteuer - bic": "1170",
             "2202 - Transit - bic": "2202"}

    KEYS = {("Journal Entry", "JE-1"): "manual-7", ("Journal Entry", "JE-2"): "vatfix-invoice-3",
            ("Journal Entry", "JE-3"): "manual-9", ("Journal Entry", "JE-4"): "journal-12",
            ("Journal Entry", "JE-5"): "vatfix-manual-9", ("Sales Invoice", "CN-1"): "credit-5"}

    def test_gl_entries_net_gross_and_erpnext_only_per_quarter_and_account(self):
        entries = [
            {"account": "2200 - Geschuldete MWST - bic", "voucher_type": "Sales Invoice", "voucher_no": "SI-1",
             "posting_date": "2026-02-28", "debit": 0, "credit": 100},
            {"account": "2200 - Geschuldete MWST - bic", "voucher_type": "Journal Entry", "voucher_no": "JE-1",
             "posting_date": "2026-02-28", "debit": 0, "credit": 40},
            {"account": "2200 - Geschuldete MWST - bic", "voucher_type": "Journal Entry", "voucher_no": "JE-2",
             "posting_date": "2026-03-01", "debit": 20, "credit": 0},
            {"account": "1170 - Vorsteuer - bic", "voucher_type": "Purchase Invoice", "voucher_no": "PI-1",
             "posting_date": "2026-07-15", "debit": 5, "credit": 0},
            {"account": "1170 - Vorsteuer - bic", "voucher_type": "Bank Entry", "voucher_no": "BE-1",
             "posting_date": "2026-07-15", "debit": 3, "credit": 0},
            {"account": "2202 - Transit - bic", "voucher_type": "Journal Entry", "voucher_no": "JE-3",
             "posting_date": "2026-02-10", "debit": 30, "credit": 0},
            {"account": "2202 - Transit - bic", "voucher_type": "Journal Entry", "voucher_no": "JE-4",
             "posting_date": "2026-02-10", "debit": 30, "credit": 0},
            {"account": "9999 - Other - bic", "voucher_type": "Journal Entry", "voucher_no": "JE-9",
             "posting_date": "2026-07-15", "debit": 5, "credit": 0},
        ]
        balances = mc.erp_balances(entries, self.NAMES, self.KEYS)
        self.assertEqual(balances, {
            ("2026Q1", "2200", "net"): Decimal("-120"),
            ("2026Q1", "2200", "gross"): Decimal("40"),
            ("2026Q1", "2200", "erpnext_only"): Decimal("-80"),
            ("2026Q3", "1170", "net"): Decimal("8"),
            ("2026Q3", "1170", "erpnext_only"): Decimal("8"),
            ("2026Q1", "2202", "gross"): Decimal("30"),
            ("2026Q1", "2202", "erpnext_only"): Decimal("30"),
        })

    def test_a_manual_entry_transit_line_is_erpnext_only_but_its_sales_tax_is_bexios(self):
        self.assertFalse(mc.bexio_keyed({"voucher_type": "Journal Entry", "voucher_no": "JE-3"}, "2202", self.KEYS))
        self.assertTrue(mc.bexio_keyed({"voucher_type": "Journal Entry", "voucher_no": "JE-1"}, "2200", self.KEYS))

    def test_a_vatfix_manual_line_on_1171_is_bexios_but_on_a_transit_account_it_is_not(self):
        self.assertTrue(mc.bexio_keyed({"voucher_type": "Journal Entry", "voucher_no": "JE-5"}, "1171", self.KEYS))
        self.assertFalse(mc.bexio_keyed({"voucher_type": "Journal Entry", "voucher_no": "JE-5"}, "1172", self.KEYS))

    def test_an_invoice_and_its_vatfix_reversal_are_erpnext_only_on_the_invoice_accounts(self):
        self.assertFalse(mc.bexio_keyed({"voucher_type": "Journal Entry", "voucher_no": "JE-2"}, "2200", self.KEYS))
        self.assertFalse(mc.bexio_keyed({"voucher_type": "Sales Invoice", "voucher_no": "SI-1"}, "2200", self.KEYS))

    def test_a_vatfix_invoice_line_on_a_transit_account_is_bexios(self):
        self.assertTrue(mc.bexio_keyed({"voucher_type": "Journal Entry", "voucher_no": "JE-2"}, "2202", self.KEYS))

    def test_a_credit_note_posts_its_transit_vat_as_bexio_does(self):
        self.assertTrue(mc.bexio_keyed({"voucher_type": "Sales Invoice", "voucher_no": "CN-1"}, "2202", self.KEYS))
        self.assertFalse(mc.bexio_keyed({"voucher_type": "Sales Invoice", "voucher_no": "CN-1"}, "2200", self.KEYS))


class CompareTest(unittest.TestCase):
    def test_equal_sides_have_no_difference(self):
        rows = mc.compare({("2026Q1", "2200", "net"): Decimal("-80")},
                          {("2026Q1", "2200", "net"): Decimal("-80.00")})
        self.assertEqual(mc.differences(rows), [])

    def test_a_missing_side_is_a_difference_on_its_quarter(self):
        bexio = {("2026Q1", "2200", "net"): Decimal("-80")}
        erp = {("2026Q2", "1170", "gross"): Decimal("5")}
        rows = mc.compare(bexio, erp)
        self.assertEqual(rows, [
            ("2026Q1", "2200", "net", Decimal("-80.00"), Decimal("0.00"), Decimal("80.00")),
            ("2026Q2", "1170", "gross", Decimal("0.00"), Decimal("5.00"), Decimal("5.00")),
        ])
        self.assertEqual(len(mc.differences(rows)), 2)

    def test_a_rappen_is_a_difference(self):
        rows = mc.compare({("2026Q1", "2200", "gross"): Decimal("-80.00")},
                          {("2026Q1", "2200", "gross"): Decimal("-80.01")})
        self.assertEqual(mc.differences(rows), [
            ("2026Q1", "2200", "gross", Decimal("-80.00"), Decimal("-80.01"), Decimal("-0.01")),
        ])

    def test_summary_names_no_amounts(self):
        rows = mc.compare({("2026Q1", "2200", "net"): Decimal("-80")},
                          {("2026Q1", "2200", "net"): Decimal("-79")})
        text = "\n".join(mc.summary_lines(rows))
        self.assertIn("comparisons with a difference: 1", text)
        self.assertNotIn("80", text)


if __name__ == "__main__":
    unittest.main()
