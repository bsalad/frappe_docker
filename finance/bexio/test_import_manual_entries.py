"""Offline tests for import_manual_entries.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from decimal import Decimal

import import_manual_entries as ime
import import_purchase as ip

CURRENCIES = {"1": "CHF", "2": "EUR"}


def lookups(taxes=None):
    return ime.Lookups(
        accounts={
            "11": ("1020 - Bank Test - bic", "Asset"),
            "12": ("1100 - Debitoren Test - bic", "Asset"),
            "21": ("5001 - Testaufwand - bic", "Expense"),
            "22": ("1170 - Vorsteuer Test - bic", "Asset"),
            "31": ("2200 - Umsatzsteuer Test - bic", "Liability"),
            "32": ("3000 - Testertrag - bic", "Income"),
            "41": ("9100 - Eroeffnung Test - bic", "Equity"),
            "99": ("9999 - Ohne Wurzel - bic", "Stock"),
        },
        taxes={"35": "Test MWST bexio 35", "22": "Test MWST bexio 22"} if taxes is None else dict(taxes),
    )


def line(debit, credit, amount, description="Testbuchung", **extra):
    return dict({"debit_account_id": debit, "credit_account_id": credit, "amount": amount,
                 "description": description, "currency_id": 1, "currency_factor": 1, "tax_id": None,
                 "tax_account_id": None}, **extra)


def entry(eid="m-1", kind="manual_single_entry", lines=None, **extra):
    return dict({"id": eid, "type": kind, "date": "2025-06-30", "reference_nr": "BU-1",
                 "entries": lines if lines is not None else [line(21, 11, 100)]}, **extra)


def side_totals(doc):
    debit = sum(Decimal(str(r.get("debit", 0))) for r in doc["accounts"])
    credit = sum(Decimal(str(r.get("credit", 0))) for r in doc["accounts"])
    return debit, credit


def rows_of(doc):
    """(account, debit, credit) per row, as floats, for comparing with the expected rows."""
    return [(r["account"], r.get("debit", 0.0), r.get("credit", 0.0)) for r in doc["accounts"]]


class MapEntryTest(unittest.TestCase):
    def test_single_entry_without_vat_is_one_debit_and_one_credit(self):
        doc = ime.map_entry(entry(), lookups(), CURRENCIES)
        self.assertEqual(doc["doctype"], "Journal Entry")
        self.assertEqual(doc["posting_date"], "2025-06-30")
        self.assertEqual(doc["bexio_id"], "m-1")
        self.assertEqual(rows_of(doc), [("5001 - Testaufwand - bic", 100.0, 0.0), ("1020 - Bank Test - bic", 0.0, 100.0)])
        self.assertEqual(side_totals(doc), (Decimal("100"), Decimal("100")))
        self.assertEqual(doc["multi_currency"], 0)

    def test_reference_goes_to_cheque_number_and_remark(self):
        doc = ime.map_entry(entry(reference_nr="BU-77"), lookups(), CURRENCIES)
        self.assertEqual((doc["cheque_no"], doc["user_remark"]), ("BU-77", "BU-77"))

    def test_compound_entry_puts_every_line_in_one_journal_entry(self):
        lines = [line(21, 11, 60, "Teil 1"), line(12, 32, 40, "Teil 2")]
        doc = ime.map_entry(entry(kind="manual_compound_entry", lines=lines), lookups(), CURRENCIES)
        self.assertEqual(len(doc["accounts"]), 4)
        self.assertEqual([r["user_remark"] for r in doc["accounts"]], ["Teil 1", "Teil 1", "Teil 2", "Teil 2"])
        self.assertEqual(side_totals(doc), (Decimal("100"), Decimal("100")))

    def test_group_entry_maps_like_a_compound_one(self):
        lines = [line(21, 11, 10), line(21, 11, 20), line(21, 11, 30)]
        doc = ime.map_entry(entry(kind="manual_group_entry", lines=lines), lookups(), CURRENCIES)
        self.assertEqual(len(doc["accounts"]), 6)
        self.assertEqual(side_totals(doc), (Decimal("60"), Decimal("60")))

    def test_purchase_vat_is_split_off_the_debit_side(self):
        # 108.10 incl. 8.1 %: net 100.00 on the expense, tax 8.10 on Vorsteuer (asset, debited), 108.10 credited to the bank
        doc = ime.map_entry(entry(lines=[line(21, 11, 108.10, tax_id=35, tax_account_id=22)]), lookups(), CURRENCIES)
        self.assertEqual(rows_of(doc), [
            ("5001 - Testaufwand - bic", 100.0, 0.0),
            ("1170 - Vorsteuer Test - bic", 8.1, 0.0),
            ("1020 - Bank Test - bic", 0.0, 108.1),
        ])
        self.assertEqual(side_totals(doc), (Decimal("108.10"), Decimal("108.10")))

    def test_sales_vat_is_split_off_the_credit_side(self):
        # 107.70 incl. 7.7 %: receivable debited 107.70, income 100.00 and Umsatzsteuer 7.70 credited
        doc = ime.map_entry(entry(lines=[line(12, 32, 107.70, tax_id=22, tax_account_id=31)]), lookups(), CURRENCIES)
        self.assertEqual(rows_of(doc), [
            ("1100 - Debitoren Test - bic", 107.7, 0.0),
            ("3000 - Testertrag - bic", 0.0, 100.0),
            ("2200 - Umsatzsteuer Test - bic", 0.0, 7.7),
        ])
        self.assertEqual(side_totals(doc), (Decimal("107.70"), Decimal("107.70")))

    def test_vat_rows_add_up_to_the_gross_on_odd_amounts(self):
        # 0.33 at 8.1 %: the net is rounded, the tax is the rest, so the gross is kept to the rappen
        doc = ime.map_entry(entry(lines=[line(21, 11, 0.33, tax_id=35, tax_account_id=22)]), lookups(), CURRENCIES)
        self.assertEqual(side_totals(doc), (Decimal("0.33"), Decimal("0.33")))
        self.assertEqual(round(doc["accounts"][0]["debit"] + doc["accounts"][1]["debit"], 2), 0.33)

    def test_zero_rate_code_has_no_split(self):
        doc = ime.map_entry(entry(lines=[line(21, 11, 30, tax_id=47)]), lookups(), CURRENCIES)
        self.assertEqual(len(doc["accounts"]), 2)

    def test_foreign_currency_keeps_the_amount_and_the_rate(self):
        # 100 EUR at 0.93: 93.00 CHF, the EUR amount in the account-currency column
        lines = [line(21, 11, 100, currency_id=2, currency_factor=0.93)]
        doc = ime.map_entry(entry(lines=lines), lookups(), CURRENCIES)
        debit = doc["accounts"][0]
        self.assertEqual((debit["debit"], debit["debit_in_account_currency"], debit["exchange_rate"]), (93.0, 100.0, 0.93))
        self.assertEqual(doc["multi_currency"], 1)
        self.assertEqual(side_totals(doc), (Decimal("93.00"), Decimal("93.00")))

    def test_foreign_currency_with_vat_balances_in_chf(self):
        lines = [line(21, 11, 108.10, currency_id=2, currency_factor=0.93, tax_id=35, tax_account_id=22)]
        doc = ime.map_entry(entry(lines=lines), lookups(), CURRENCIES)
        self.assertEqual(side_totals(doc), (Decimal("100.53"), Decimal("100.53")))

    def test_banking_entry_is_mapped_and_is_the_same_shape(self):
        doc = ime.map_entry(entry(kind=ime.BANKING), lookups(), CURRENCIES)
        self.assertEqual(len(doc["accounts"]), 2)


class MapEntryErrorTest(unittest.TestCase):
    def assertUnmapped(self, record, words, known=None):
        with self.assertRaisesRegex(ip.MappingError, words):
            ime.map_entry(record, known or lookups(), CURRENCIES)

    def test_unknown_type_is_reported(self):
        self.assertUnmapped(entry(kind=None), "unknown entry type None")

    def test_entry_without_lines_is_reported(self):
        self.assertUnmapped(entry(lines=[]), "no entries lines")

    def test_single_entry_with_two_lines_is_reported(self):
        self.assertUnmapped(entry(lines=[line(21, 11, 1), line(21, 11, 2)]), "single entry with 2 lines")

    def test_unknown_account_is_reported(self):
        self.assertUnmapped(entry(lines=[line(777, 11, 10)]), "no Account for account 777")

    def test_vat_code_without_template_is_reported_not_guessed(self):
        self.assertUnmapped(entry(lines=[line(21, 11, 10, tax_id=35, tax_account_id=22)]),
                            "no Item Tax Template with bexio_id 35", lookups(taxes={}))

    def test_unknown_vat_code_is_reported(self):
        self.assertUnmapped(entry(lines=[line(21, 11, 10, tax_id=99, tax_account_id=22)]), "unknown VAT code 99")

    def test_vat_without_tax_account_is_reported(self):
        self.assertUnmapped(entry(lines=[line(21, 11, 10, tax_id=35)]), "no Account for account None")

    def test_vat_account_of_an_unknown_root_is_reported(self):
        self.assertUnmapped(entry(lines=[line(21, 11, 10, tax_id=35, tax_account_id=99)]), "has root type Stock")

    def test_unknown_currency_is_reported(self):
        self.assertUnmapped(entry(lines=[line(21, 11, 10, currency_id=9)]), "unknown currency 9")

    def test_bad_date_is_reported(self):
        self.assertUnmapped(entry(date="30.06.2025"), "no valid date")


class DryRunTest(unittest.TestCase):
    def test_totals_per_year_with_banking_counted_apart(self):
        entries = [
            entry("m-1"),
            entry("m-2", kind=ime.BANKING, date="2025-07-01"),
            entry("m-3", date="2026-01-05"),
        ]
        totals = ime.dry_run(entries, lookups(), CURRENCIES)
        self.assertEqual(totals.rows[2025]["entries"], 2)
        self.assertEqual(totals.rows[2025]["banking"], 1)
        self.assertEqual(totals.rows[2025]["debit"], Decimal("200"))
        self.assertEqual(totals.rows[2025]["banking_debit"], Decimal("100"))
        self.assertEqual(totals.rows[2026]["mapped"], 1)
        self.assertEqual(totals.problems, [])

    def test_unmapped_entries_are_listed_by_bexio_id(self):
        totals = ime.dry_run([entry("m-9", lines=[line(777, 11, 10)])], lookups(), CURRENCIES)
        self.assertEqual(totals.rows[2025]["unmapped"], 1)
        self.assertEqual(totals.problems, ["manual entry m-9: unmapped, no Account for account 777"])

    def test_entry_with_bad_date_is_counted_under_no_year(self):
        totals = ime.dry_run([entry("m-8", date="x")], lookups(), CURRENCIES)
        self.assertEqual(totals.rows[None]["unmapped"], 1)

    def test_report_prints_totals_only(self):
        totals = ime.dry_run([entry("m-1")], lookups(), CURRENCIES)
        text = ime.report(totals, "/somewhere")
        self.assertIn("nothing was written", text)
        self.assertNotIn("Testaufwand", text)
        self.assertNotIn("m-1", text)


class LoadEntriesTest(unittest.TestCase):
    def test_no_file_means_not_exported_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ime.load_entries(tmp))

    def test_file_is_read_with_the_currency_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ime.ENTRIES_FILE), "w", encoding="utf-8") as f:
                json.dump([entry()], f)
            with open(os.path.join(tmp, ime.CURRENCIES_FILE), "w", encoding="utf-8") as f:
                json.dump([{"id": 1, "name": "CHF"}, {"id": 2, "name": "EUR"}], f)
            entries, currencies = ime.load_entries(tmp)
        self.assertEqual(entries[0]["id"], "m-1")
        self.assertEqual(currencies, {"1": "CHF", "2": "EUR"})


class MainTest(unittest.TestCase):
    def test_live_run_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ime.main(["--export", "/nonexistent"]), 2)

    def test_pending_export_says_not_exported_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(ime.main(["--dry-run", "--export", tmp]), 0)
        self.assertIn("not exported yet", out.getvalue())


if __name__ == "__main__":
    unittest.main()
