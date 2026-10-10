"""Offline tests for check_trial_balance.py: the bexio and ERPNext figures per year and account, and the comparison.

All data is invented. Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import csv
import os
import tempfile
import unittest
from decimal import Decimal

import check_trial_balance as ctb

YEARS = [
    {"id": 1, "start": "2020-01-01", "end": "2020-12-31"},
    {"id": 2, "start": "2021-01-01", "end": "2021-12-31"},
]
ACCOUNTS = [
    {"id": 10, "account_no": "1020"},  # balance sheet: bank
    {"id": 11, "account_no": "3200"},  # P&L: income
    {"id": 12, "account_no": "4000"},  # P&L: expense
    {"id": 13, "account_no": "9100"},  # bexio's closing account
]
ACCOUNT_NO = {str(a["id"]): a["account_no"] for a in ACCOUNTS}


def line(i, date, debit, credit, amount, description="line", ref_class=None):
    return {
        "id": i, "date": date + "T00:00:00+02:00", "description": description,
        "debit_account_id": debit, "credit_account_id": credit,
        "amount": amount, "base_currency_amount": amount, "ref_class": ref_class, "ref_id": None,
    }


def journal():
    return [
        line(1, "2020-03-01", 10, 11, Decimal("100")),             # bank in, income 100
        line(2, "2020-06-01", 4000, 10, Decimal("30")),             # expense paid from bank 30
        line(3, "2020-12-31", 11, 13, Decimal("70")),               # year-end closing (account 13 is 9100): left out
        line(4, "2021-01-01", 13, 11, Decimal("70"), "Saldovortrag 2020"),   # carry-forward: left out
        line(5, "2021-02-01", 10, 11, Decimal("50")),               # bank in, income 50
    ]


def buckets_for(lines):
    return {line["id"]: "sales_invoice" for line in lines}


class BexioEntriesTest(unittest.TestCase):
    def setUp(self):
        self.lines = journal()
        self.data = {"journal": self.lines}
        self.buckets = buckets_for(self.lines)
        self.buckets[4] = "carry_forward"

    def test_closing_lines_and_carry_forward_are_left_out(self):
        entries = ctb.bexio_entries(self.data, self.buckets, ACCOUNTS)
        self.assertEqual(len(entries), 2 * 3)  # lines 1, 2 and 5 only, each with a debit and a credit side
        self.assertFalse(any(key == "13" for _, key, _ in entries))

    def test_debit_positive_credit_negative(self):
        entries = ctb.bexio_entries(self.data, self.buckets, ACCOUNTS)
        self.assertIn(("2020-03-01T00:00:00+02:00", "10", Decimal("100")), entries)
        self.assertIn(("2020-03-01T00:00:00+02:00", "11", Decimal("-100")), entries)


class BalancesTest(unittest.TestCase):
    def test_movement_per_year_and_running_closing(self):
        entries = [
            ("2020-03-01", "10", Decimal("100")),
            ("2020-06-01", "10", Decimal("-30")),
            ("2021-02-01", "10", Decimal("50")),
        ]
        result, outside = ctb.balances(entries, YEARS)
        self.assertEqual(result[(1, "10")], (Decimal("70"), Decimal("70")))
        self.assertEqual(result[(2, "10")], (Decimal("50"), Decimal("120")))
        self.assertEqual(outside, [])

    def test_a_date_outside_every_business_year_is_reported(self):
        result, outside = ctb.balances([("2019-12-31", "10", Decimal("1"))], YEARS)
        self.assertEqual(outside, ["2019-12-31"])
        self.assertEqual(result, {})


class ErpEntriesTest(unittest.TestCase):
    def test_unmapped_account_is_keyed_by_its_name(self):
        rows = [
            {"account": "1020 - Bank - bic", "debit": 100, "credit": 0, "posting_date": "2020-03-01", "voucher_no": "A"},
            {"account": "Old - bic", "debit": 0, "credit": 5, "posting_date": "2020-03-02", "voucher_no": "B"},
        ]
        entries = ctb.erp_entries(rows, {"1020 - Bank - bic": "10"}, {"13"})
        self.assertEqual(entries[0], ("2020-03-01", "10", Decimal("100")))
        self.assertEqual(entries[1], ("2020-03-02", ("erp", "Old - bic"), Decimal("-5")))

    def test_a_voucher_with_a_leg_on_the_closing_account_is_left_out_whole(self):
        # a year-start carry-forward Journal Entry: 2970 debited, 9100 credited (bexio id 13)
        rows = [
            {"account": "2970 - Gewinnvortrag - bic", "debit": 50, "credit": 0, "posting_date": "2022-01-01", "voucher_no": "JV-1"},
            {"account": "9100 - Eroeffnung - bic", "debit": 0, "credit": 50, "posting_date": "2022-01-01", "voucher_no": "JV-1"},
            {"account": "1020 - Bank - bic", "debit": 100, "credit": 0, "posting_date": "2022-02-01", "voucher_no": "JV-2"},
        ]
        erp_bexio_id = {"2970 - Gewinnvortrag - bic": "20", "9100 - Eroeffnung - bic": "13", "1020 - Bank - bic": "10"}
        entries = ctb.erp_entries(rows, erp_bexio_id, {"13"})
        self.assertEqual(entries, [("2022-02-01", "10", Decimal("100"))])


class CompareTest(unittest.TestCase):
    def test_balance_sheet_compares_closing_and_profit_and_loss_compares_movement(self):
        bexio = {
            (1, "10"): (Decimal("70"), Decimal("70")),
            (2, "10"): (Decimal("50"), Decimal("120")),
            (1, "11"): (Decimal("-100"), Decimal("-100")),
            (2, "11"): (Decimal("-50"), Decimal("-150")),
        }
        # ERPNext's income accumulates (no closing voucher): its 2021 closing is -150 too, but its movement is what counts.
        erp = {
            (1, "10"): (Decimal("70"), Decimal("70")),
            (2, "10"): (Decimal("50"), Decimal("120")),
            (1, "11"): (Decimal("-100"), Decimal("-100")),
            (2, "11"): (Decimal("-50"), Decimal("-150")),
        }
        rows = ctb.compare(bexio, erp, YEARS, ACCOUNT_NO_BY_BEXIO_ID)
        self.assertEqual(len(rows), 4)  # every year and account with a figure is a row
        self.assertEqual([r for r in rows if r["difference"] != 0], [])
        self.assertEqual({(r["account_no"], r["basis"]) for r in rows}, {("1020", "closing"), ("3200", "movement")})

    def test_a_difference_in_a_closing_balance_is_reported_with_its_year(self):
        bexio = {(2, "10"): (Decimal("50"), Decimal("120"))}
        erp = {(2, "10"): (Decimal("50"), Decimal("121"))}
        rows = ctb.compare(bexio, erp, YEARS, ACCOUNT_NO_BY_BEXIO_ID)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["basis"], "closing")
        self.assertEqual(rows[0]["year"], 2)
        self.assertEqual(rows[0]["difference"], Decimal("1"))

    def test_an_erp_account_without_bexio_id_is_compared_against_zero(self):
        erp = {(1, ("erp", "Old - bic")): (Decimal("5"), Decimal("5"))}
        rows = ctb.compare({}, erp, YEARS, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["erp_account"], "Old - bic")
        self.assertEqual(rows[0]["bexio"], Decimal("0"))
        self.assertEqual(rows[0]["difference"], Decimal("5"))


ACCOUNT_NO_BY_BEXIO_ID = {"10": "1020", "11": "3200", "12": "4000"}


class SummaryTest(unittest.TestCase):
    def test_counts_and_the_band_of_the_largest_difference(self):
        rows = [
            {"year": 1, "bexio_id": "10", "erp_account": "", "difference": Decimal("0.004")},
            {"year": 2, "bexio_id": "10", "erp_account": "", "difference": Decimal("0.02")},
            {"year": 2, "bexio_id": "11", "erp_account": "", "difference": Decimal("-3")},
        ]
        text = ctb.summary(rows, YEARS, 2)
        self.assertIn("years compared: 2", text)
        self.assertIn("accounts compared: 2", text)
        self.assertIn("rows with a difference: 2", text)
        self.assertIn("accounts with a difference: 2", text)
        self.assertIn("largest difference: above 1 CHF (rows in that band: 1)", text)

    def test_no_difference(self):
        text = ctb.summary([{"year": 1, "bexio_id": "10", "erp_account": "", "difference": Decimal("0")}], YEARS, 1)
        self.assertIn("rows with a difference: 0", text)
        self.assertIn("largest difference: none", text)


class YearOfTest(unittest.TestCase):
    def test_a_timestamp_and_the_year_bounds(self):
        self.assertEqual(ctb.year_of("2020-12-31T23:00:00+02:00", YEARS), 1)
        self.assertEqual(ctb.year_of("2021-01-01", YEARS), 2)
        self.assertIsNone(ctb.year_of("2022-01-01", YEARS))


class WriteCsvTest(unittest.TestCase):
    def test_rows_are_written_with_plain_decimals(self):
        rows = [{"year": 2, "account_no": "1020", "bexio_id": "10", "erp_account": "", "basis": "closing",
                 "bexio": Decimal("120"), "erpnext": Decimal("120.5"), "difference": Decimal("0.5")}]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "check.csv")
            ctb.write_csv(path, rows)
            with open(path, encoding="utf-8") as f:
                read = list(csv.DictReader(f))
        self.assertEqual(read[0]["erpnext"], "120.5")
        self.assertEqual(read[0]["difference"], "0.5")
        self.assertEqual(list(read[0].keys()), ctb.FIELDS)


if __name__ == "__main__":
    unittest.main()
