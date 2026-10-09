"""Offline tests for import_opening.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import json
import os
import tempfile
import unittest

import import_opening as op

# invented accounts of the chart, by number
BY_NUMBER = {
    "1100": "1100 - Testbank - bic", "2200": "2200 - Testkapital - bic",
    "9100": "9100 - Testeröffnung - bic", "9900": "9900 - Testkorrektur - bic",
}


def year(yid, start, end, status="open"):
    return {"id": yid, "start": start, "end": end, "status": status, "closed_at": None}


YEARS = [
    year(9, "2026-01-01", "2026-12-31"),
    year(18, "2019-01-01", "2019-12-31", "closed"),  # the earliest by start, with the highest id
    year(1, "2020-01-01", "2020-12-31"),
]
FIRST_YEAR = year(18, "2019-01-01", "2019-12-31", "closed")


class FirstYearTest(unittest.TestCase):
    def test_earliest_start_wins_over_id_order(self):
        self.assertEqual(op.first_year(YEARS)["id"], 18)

    def test_no_years_is_unmapped(self):
        with self.assertRaises(op.Unmapped):
            op.first_year([])


class MapAccountTest(unittest.TestCase):
    def test_accounts_by_number_clearing_ones_included(self):
        self.assertEqual(op.map_account("1100", BY_NUMBER), "1100 - Testbank - bic")
        self.assertEqual(op.map_account("9100", BY_NUMBER), "9100 - Testeröffnung - bic")

    def test_account_missing_in_erpnext_is_unmapped(self):
        with self.assertRaises(op.Unmapped):
            op.map_account("4444", BY_NUMBER)


class OpeningEntryTest(unittest.TestCase):
    def test_balanced_lines_make_one_opening_entry(self):
        lines = [("1100", 500, 0), ("2200", 0, 300), ("9100", 0, 200)]
        doc = op.opening_entry(lines, year(18, "2019-01-01", "2019-12-31"), BY_NUMBER)
        self.assertEqual(doc["doctype"], "Journal Entry")
        self.assertEqual(doc["voucher_type"], "Opening Entry")
        self.assertEqual(doc["is_opening"], "Yes")
        self.assertEqual(doc["posting_date"], "2019-01-01")
        self.assertEqual(doc["bexio_id"], "opening-18")
        self.assertEqual(doc["accounts"], [
            {"account": "1100 - Testbank - bic", "debit_in_account_currency": 500.0, "credit_in_account_currency": 0},
            {"account": "2200 - Testkapital - bic", "debit_in_account_currency": 0, "credit_in_account_currency": 300.0},
            {"account": "9100 - Testeröffnung - bic", "debit_in_account_currency": 0, "credit_in_account_currency": 200.0},
        ])

    def test_lines_of_one_account_are_added_up(self):
        lines = [("1100", 100, 0), ("9100", 0, 60), ("9100", 0, 40), ("1100", 0, 0)]
        doc = op.opening_entry(lines, year(18, "2019-01-01", "2019-12-31"), BY_NUMBER)
        self.assertEqual(doc["accounts"], [
            {"account": "1100 - Testbank - bic", "debit_in_account_currency": 100.0, "credit_in_account_currency": 0},
            {"account": "9100 - Testeröffnung - bic", "debit_in_account_currency": 0, "credit_in_account_currency": 100.0},
        ])

    def test_no_lines_means_no_entry(self):
        self.assertIsNone(op.opening_entry([], year(18, "2019-01-01", "2019-12-31"), BY_NUMBER))

    def test_lines_without_amounts_mean_no_entry(self):
        lines = [("1100", 0, 0)]
        self.assertIsNone(op.opening_entry(lines, year(18, "2019-01-01", "2019-12-31"), BY_NUMBER))

    def test_unbalanced_lines_are_unmapped(self):
        lines = [("1100", 500, 0), ("2200", 0, 499)]
        with self.assertRaises(op.Unmapped):
            op.opening_entry(lines, year(18, "2019-01-01", "2019-12-31"), BY_NUMBER)

    def test_unmapped_account_is_unmapped(self):
        lines = [("4444", 10, 0), ("9100", 0, 10)]
        with self.assertRaises(op.Unmapped):
            op.opening_entry(lines, year(18, "2019-01-01", "2019-12-31"), BY_NUMBER)


# invented bexio accounts: export ids and account numbers
BEXIO_ACCOUNTS = [
    {"id": 71, "account_no": 1100}, {"id": 72, "account_no": 2200},
    {"id": 73, "account_no": 9100}, {"id": 74, "account_no": 6000},
]


def line(lid, date, debit, credit, amount):
    return {"id": lid, "date": date, "debit_account_id": debit, "credit_account_id": credit, "base_currency_amount": amount}


class OpeningLinesTest(unittest.TestCase):
    def test_no_journal_lines_in_the_first_year_means_no_rows(self):
        journal = [line(1, "2020-04-02T00:00:00+02:00", 71, 72, 500)]
        self.assertEqual(op.opening_lines(journal, BEXIO_ACCOUNTS, FIRST_YEAR), [])

    def test_line_touching_9100_gives_both_sides(self):
        journal = [line(1, "2019-01-01T00:00:00+01:00", 71, 73, 500)]
        self.assertEqual(op.opening_lines(journal, BEXIO_ACCOUNTS, FIRST_YEAR), [("1100", 500, 0), ("9100", 0, 500)])

    def test_ordinary_posting_in_the_first_year_is_not_an_opening_booking(self):
        journal = [line(1, "2019-06-30T00:00:00+02:00", 71, 74, 80)]
        self.assertEqual(op.opening_lines(journal, BEXIO_ACCOUNTS, FIRST_YEAR), [])

    def test_opening_account_line_in_another_year_is_not_counted(self):
        journal = [line(1, "2020-01-01T00:00:00+01:00", 71, 73, 500)]
        self.assertEqual(op.opening_lines(journal, BEXIO_ACCOUNTS, FIRST_YEAR), [])

    def test_account_id_missing_from_the_export_is_unmapped(self):
        journal = [line(1, "2019-01-01T00:00:00+01:00", 999, 73, 500)]
        with self.assertRaises(op.Unmapped):
            op.opening_lines(journal, BEXIO_ACCOUNTS, FIRST_YEAR)

    def test_rows_make_one_balanced_opening_entry(self):
        journal = [
            line(1, "2019-01-01T00:00:00+01:00", 71, 73, 500),
            line(2, "2019-01-01T00:00:00+01:00", 73, 72, 200),
        ]
        doc = op.opening_entry(op.opening_lines(journal, BEXIO_ACCOUNTS, FIRST_YEAR), FIRST_YEAR, BY_NUMBER)
        self.assertEqual(doc["accounts"], [
            {"account": "1100 - Testbank - bic", "debit_in_account_currency": 500.0, "credit_in_account_currency": 0},
            {"account": "2200 - Testkapital - bic", "debit_in_account_currency": 0, "credit_in_account_currency": 200.0},
            {"account": "9100 - Testeröffnung - bic", "debit_in_account_currency": 0, "credit_in_account_currency": 300.0},
        ])


class SurveyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def export(self, manifest_journal=None, journal=False):
        d = self.tmp.name
        with open(os.path.join(d, "business_years.json"), "w", encoding="utf-8") as f:
            json.dump(YEARS, f)
        entities = {"business_years": {"status": "ok", "count": 3}}
        if manifest_journal is not None:
            entities["journal"] = manifest_journal
        with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"entities": entities}, f)
        if journal:
            with open(os.path.join(d, "journal.json"), "w", encoding="utf-8") as f:
                json.dump([], f)
        return d

    def test_counts_and_first_year(self):
        s = op.survey(self.export())
        self.assertEqual((s["years"], s["closed"]), (3, 1))
        self.assertEqual(s["first"]["id"], 18)

    def test_journal_refused_by_bexio_is_reported(self):
        d = self.export({"status": "error", "error": "HTTP 403 on /3.0/accounting/journal"})
        self.assertEqual(op.survey(d)["journal"], "HTTP 403 on /3.0/accounting/journal")

    def test_journal_absent_from_manifest_is_reported(self):
        self.assertEqual(op.survey(self.export())["journal"], "not in the export")

    def test_journal_file_present(self):
        self.assertEqual(op.survey(self.export(journal=True))["journal"], "exported")


if __name__ == "__main__":
    unittest.main()
