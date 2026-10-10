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
    NUMBERS = {127: "2200", 95: "1170", 96: "1171", 130: "2203"}

    def test_journal_lines_net_debit_minus_credit_per_quarter(self):
        journal = [
            line("2026-02-10T00:00:00+01:00", 100, 1, 127),
            line("2026-03-20T00:00:00+01:00", 40, 127, 1),
            line("2026-05-02T00:00:00+02:00", 7.5, 96, 1),
        ]
        net = mc.bexio_net(journal, self.NUMBERS)
        self.assertEqual(net, {("2026Q1", "2200"): Decimal("-60"), ("2026Q2", "1171"): Decimal("7.5")})

    def test_lines_on_other_accounts_are_left_out(self):
        journal = [line("2026-02-10", 100, 1, 2)]
        self.assertEqual(mc.bexio_net(journal, self.NUMBERS), {})

    def test_a_foreign_currency_line_counts_in_chf(self):
        journal = [line("2025-12-04", 250.5, 96, 1, base=204.17)]
        self.assertEqual(mc.bexio_net(journal, self.NUMBERS), {("2025Q4", "1171"): Decimal("204.17")})

    def test_each_line_is_rounded_to_the_rappen_as_erpnext_posts_it(self):
        journal = [line("2021-04-01", 7.777777, 96, 1), line("2021-04-02", 7.777777, 96, 1)]
        self.assertEqual(mc.bexio_net(journal, self.NUMBERS), {("2021Q2", "1171"): Decimal("15.56")})

    def test_a_carry_forward_on_1_january_is_left_out(self):
        journal = [line("2021-01-01", 88.88, 96, 273, description="provisorischer Saldovortrag")]
        self.assertEqual(mc.bexio_net(journal, self.NUMBERS), {})

    def test_a_1_january_line_that_is_no_carry_forward_is_kept(self):
        journal = [line("2021-01-01", 10, 96, 1, description="Invented reclassification")]
        self.assertEqual(mc.bexio_net(journal, self.NUMBERS), {("2021Q1", "1171"): Decimal("10")})


class ErpSideTest(unittest.TestCase):
    NAMES = {"2200 - Geschuldete MWST - bic": "2200", "1170 - Vorsteuer - bic": "1170"}

    def test_gl_entries_net_per_quarter_and_account(self):
        entries = [
            {"account": "2200 - Geschuldete MWST - bic", "posting_date": "2026-02-28", "debit": 0, "credit": 100},
            {"account": "2200 - Geschuldete MWST - bic", "posting_date": "2026-03-01", "debit": 20, "credit": 0},
            {"account": "1170 - Vorsteuer - bic", "posting_date": "2026-07-15", "debit": 5, "credit": 0},
            {"account": "9999 - Other - bic", "posting_date": "2026-07-15", "debit": 5, "credit": 0},
        ]
        net = mc.erp_net(entries, self.NAMES)
        self.assertEqual(net, {("2026Q1", "2200"): Decimal("-80"), ("2026Q3", "1170"): Decimal("5")})


class CompareTest(unittest.TestCase):
    def test_equal_sides_have_no_difference(self):
        rows = mc.compare({("2026Q1", "2200"): Decimal("-80")}, {("2026Q1", "2200"): Decimal("-80.00")})
        self.assertEqual(mc.differences(rows), [])

    def test_a_missing_side_is_a_difference_on_its_quarter(self):
        bexio = {("2026Q1", "2200"): Decimal("-80")}
        erp = {("2026Q2", "1170"): Decimal("5")}
        rows = mc.compare(bexio, erp)
        self.assertEqual(rows, [
            ("2026Q1", "2200", Decimal("-80.00"), Decimal("0.00"), Decimal("80.00")),
            ("2026Q2", "1170", Decimal("0.00"), Decimal("5.00"), Decimal("5.00")),
        ])
        self.assertEqual(len(mc.differences(rows)), 2)

    def test_a_rappen_is_a_difference(self):
        rows = mc.compare({("2026Q1", "2200"): Decimal("-80.00")}, {("2026Q1", "2200"): Decimal("-80.01")})
        self.assertEqual(mc.differences(rows), [("2026Q1", "2200", Decimal("-80.00"), Decimal("-80.01"), Decimal("-0.01"))])

    def test_summary_names_no_amounts(self):
        rows = mc.compare({("2026Q1", "2200"): Decimal("-80")}, {("2026Q1", "2200"): Decimal("-79")})
        text = "\n".join(mc.summary_lines(rows))
        self.assertIn("account-quarters with a difference: 1", text)
        self.assertNotIn("80", text)


if __name__ == "__main__":
    unittest.main()
