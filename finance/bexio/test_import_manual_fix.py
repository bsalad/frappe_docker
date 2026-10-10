"""Offline tests for import_manual_fix.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

import import_manual_fix as imf
import import_manual_entries as ime
import import_master as im
import import_purchase as ip
from test_import_manual_entries import CURRENCIES, entry, line, lookups

PURCHASE = "5001 - Testaufwand - bic"       # bexio 21
BANK = "1020 - Bank Test - bic"             # bexio 11
VORSTEUER = "1170 - Vorsteuer Test - bic"   # the VAT account of a Material code (35)


def voucher(name, bexio_id, day="2025-06-30"):
    return {"name": name, "bexio_id": bexio_id, "posting_date": day}


def gl(voucher_no, account, debit=0, credit=0):
    return {"voucher_no": voucher_no, "account": account, "debit": debit, "credit": credit}


class GroupTest(unittest.TestCase):
    def test_entry_and_its_corrections_are_one_group(self):
        self.assertEqual(imf.group_of("manual-1099"), "1099")
        self.assertEqual(imf.group_of("manual-1099-correction"), "1099")
        self.assertEqual(imf.group_of("vatfix-manual-1099"), "1099")

    def test_other_keys_are_no_group(self):
        self.assertIsNone(imf.group_of("vatfix-invoice-7"))
        self.assertIsNone(imf.group_of("7"))


class LiveGroupsTest(unittest.TestCase):
    def test_gl_of_the_entry_and_its_corrections_is_summed_per_group(self):
        vouchers = [voucher("JV-1", "manual-5"), voucher("JV-2", "manual-5-correction"), voucher("JV-3", "vatfix-manual-5"),
                    voucher("JV-4", "manual-6")]
        rows = [gl("JV-1", PURCHASE, debit=108.10), gl("JV-1", BANK, credit=108.10),
                gl("JV-2", PURCHASE, credit=8.10), gl("JV-2", VORSTEUER, debit=8.10), gl("JV-9", PURCHASE, debit=1)]
        groups = imf.live_groups(vouchers, rows)
        self.assertEqual(sorted(groups), ["5", "6"])
        self.assertEqual(groups["5"]["name"], "JV-1")
        self.assertEqual(groups["5"]["live"][PURCHASE], Decimal("100.00"))
        self.assertEqual(groups["5"]["live"][VORSTEUER], Decimal("8.10"))
        self.assertEqual(groups["5"]["keys"], ["manual-5", "manual-5-correction", "vatfix-manual-5"])
        self.assertEqual(groups["6"]["live"], {})


class PlanTest(unittest.TestCase):
    def run_plan(self, live_rows, known=None, export=None):
        export = export if export is not None else [entry("5", lines=[line(21, 11, 108.10, tax_id=35, tax_account_id=21)])]
        vouchers = [voucher("JV-1", "manual-5")]
        return imf.plan(export, vouchers, live_rows, known or lookups(), CURRENCIES)

    def test_vat_on_the_wrong_account_is_corrected_by_one_journal_entry(self):
        # the live entry books the gross on the expense: the mapping has 8.10 of it on the Vorsteuer account
        live = [gl("JV-1", PURCHASE, debit=108.10), gl("JV-1", BANK, credit=108.10)]
        documents, problems, _, counts = self.run_plan(live)
        self.assertEqual(problems, [])
        self.assertEqual(counts["corrections"], 1)
        self.assertEqual(len(documents), 1)
        document = documents[0]
        self.assertEqual(document["bexio_id"], "vatfix-manual-5")
        self.assertEqual(document["values"]["posting_date"], "2025-06-30")
        self.assertEqual(document["values"]["voucher_type"], "Journal Entry")
        self.assertEqual(document["values"]["accounts"], [
            {"account": VORSTEUER, "debit_in_account_currency": 8.1},
            {"account": PURCHASE, "credit_in_account_currency": 8.1},
        ])

    def test_an_entry_already_booked_as_the_mapping_has_no_correction(self):
        live = [gl("JV-1", PURCHASE, debit=100), gl("JV-1", VORSTEUER, debit=8.10), gl("JV-1", BANK, credit=108.10)]
        documents, problems, _, counts = self.run_plan(live)
        self.assertEqual((documents, problems, counts["corrections"], counts["unchanged"]), ([], [], 0, 1))

    def test_a_correction_entry_of_the_group_counts_towards_its_entry(self):
        # the entry booked 165.66 too much on the expense and bank; its correction takes it back: the group is then right
        live = [gl("JV-1", PURCHASE, debit=265.66), gl("JV-1", VORSTEUER, debit=8.10), gl("JV-1", BANK, credit=273.76),
                gl("JV-2", PURCHASE, credit=165.66), gl("JV-2", BANK, debit=165.66)]
        vouchers = [voucher("JV-1", "manual-5"), voucher("JV-2", "manual-5-correction")]
        export = [entry("5", lines=[line(21, 11, 108.10, tax_id=35, tax_account_id=21)])]
        documents, problems, _, counts = imf.plan(export, vouchers, live, lookups(), CURRENCIES)
        self.assertEqual((documents, problems), ([], []))
        self.assertEqual(counts["unchanged"], 1)

    def test_a_group_with_its_own_correction_is_corrected_on_the_entry_date(self):
        # the entry lacks the VAT (100.00 on the expense, bank 100.00); its correction of 8.10 is on another day, and
        # the group's correction is still on the entry's date: VAT 8.10 debited, expense credited
        live = [gl("JV-1", PURCHASE, debit=100), gl("JV-1", BANK, credit=100), gl("JV-2", PURCHASE, debit=8.10), gl("JV-2", BANK, credit=8.10)]
        vouchers = [voucher("JV-1", "manual-5"), voucher("JV-2", "manual-5-correction", day="2025-07-01")]
        export = [entry("5", lines=[line(21, 11, 108.10, tax_id=35, tax_account_id=21)])]
        documents, problems, _, _ = imf.plan(export, vouchers, live, lookups(), CURRENCIES)
        self.assertEqual(problems, [])
        self.assertEqual([d["bexio_id"] for d in documents], ["vatfix-manual-5"])
        self.assertEqual(documents[0]["values"]["posting_date"], "2025-06-30")
        self.assertEqual(documents[0]["values"]["accounts"][0]["account"], VORSTEUER)

    def test_depreciation_account_makes_a_depreciation_entry(self):
        live = [gl("JV-1", PURCHASE, debit=108.10), gl("JV-1", BANK, credit=108.10)]
        documents, _, _, _ = self.run_plan(live, known=lookups(depreciation={PURCHASE}))
        self.assertEqual(documents[0]["values"]["voucher_type"], "Depreciation Entry")

    def test_correction_on_an_account_not_in_chf_is_listed_not_written(self):
        # a stray live row on a EUR account the mapping never books: the correction would have to be in EUR, so it is listed
        stray = "2200 - Umsatzsteuer Test - bic"
        live = [gl("JV-1", PURCHASE, debit=100), gl("JV-1", VORSTEUER, debit=8.10), gl("JV-1", BANK, credit=108.10),
                gl("JV-1", stray, debit=1), gl("JV-1", BANK, credit=1)]
        documents, problems, _, _ = self.run_plan(live, known=lookups(currencies={stray: "EUR"}))
        self.assertEqual(documents, [])
        self.assertEqual(problems, [("5", "the correction touches an account not in CHF: {}".format(stray))])

    def test_entry_that_no_longer_maps_is_listed_not_corrected(self):
        live = [gl("JV-1", PURCHASE, debit=108.10), gl("JV-1", BANK, credit=108.10)]
        export = [entry("5", lines=[line(777, 11, 108.10, tax_id=35, tax_account_id=21)])]
        documents, problems, _, _ = self.run_plan(live, export=export)
        self.assertEqual(documents, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("the entry does not map", problems[0][1])

    def test_live_entry_the_export_does_not_hold_is_listed_not_corrected(self):
        live = [gl("JV-1", PURCHASE, debit=108.10), gl("JV-1", BANK, credit=108.10)]
        documents, problems, _, _ = self.run_plan(live, export=[entry("9", lines=[line(21, 11, 10)])])
        self.assertEqual(documents, [])
        self.assertEqual(problems, [("5", "live Journal Entries of a bexio entry the export does not hold")])

    def test_export_entry_that_is_not_live_is_counted(self):
        documents, problems, _, counts = imf.plan([entry("5", lines=[line(21, 11, 108.10, tax_id=35, tax_account_id=21)])], [], [], lookups(), CURRENCIES)
        self.assertEqual((documents, problems, counts["not live"]), ([], [], 1))


def journal_line(line_id, debit, credit, amount, description="Testbuchung"):
    return {"id": line_id, "ref_class": "ManualEntry", "date": "2025-06-30", "description": description,
            "debit_account_id": debit, "credit_account_id": credit, "base_currency_amount": amount}


class JournalWinsTest(unittest.TestCase):
    def test_a_listed_entry_takes_bexios_journal_as_its_expected_side(self):
        # the mapping books the VAT on the Vorsteuer account; bexio's journal books the gross on the expense, so the
        # listed entry's correction moves the 8.10 from the Vorsteuer account to the expense
        live = [gl("JV-1", PURCHASE, debit=100), gl("JV-1", VORSTEUER, debit=8.10), gl("JV-1", BANK, credit=108.10)]
        export = [entry("5", lines=[line(21, 11, 108.10, tax_id=35, tax_account_id=21)])]
        journal = [journal_line(1, 21, 11, 108.10)]
        documents, problems, details, counts = imf.plan(export, [voucher("JV-1", "manual-5")], live, lookups(), CURRENCIES,
                                                         journal, {"5": "test reason"})
        self.assertEqual(problems, [])
        self.assertEqual(counts["journal wins"], 1)
        self.assertEqual([d["bexio_id"] for d in documents], ["vatfix-manual-5"])
        self.assertEqual(documents[0]["values"]["accounts"], [
            {"account": VORSTEUER, "credit_in_account_currency": 8.1},
            {"account": PURCHASE, "debit_in_account_currency": 8.1},
        ])
        self.assertIn("journal wins: test reason", details[0])

    def test_an_entry_not_listed_keeps_the_mapping(self):
        live = [gl("JV-1", PURCHASE, debit=100), gl("JV-1", VORSTEUER, debit=8.10), gl("JV-1", BANK, credit=108.10)]
        export = [entry("5", lines=[line(21, 11, 108.10, tax_id=35, tax_account_id=21)])]
        journal = [journal_line(1, 21, 11, 108.10)]
        documents, problems, _, counts = imf.plan(export, [voucher("JV-1", "manual-5")], live, lookups(), CURRENCIES, journal, {})
        self.assertEqual((documents, problems, counts["unchanged"], counts.get("journal wins", 0)), ([], [], 1, 0))

    def test_journal_line_on_an_account_without_erp_account_lists_the_entry(self):
        live = [gl("JV-1", PURCHASE, debit=108.10), gl("JV-1", BANK, credit=108.10)]
        export = [entry("5", lines=[line(21, 11, 108.10, tax_id=35, tax_account_id=21)])]
        journal = [journal_line(1, 21, 999, 108.10)]
        documents, problems, _, _ = imf.plan(export, [voucher("JV-1", "manual-5")], live, lookups(), CURRENCIES, journal, {"5": "r"})
        self.assertEqual(documents, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("no Account in ERPNext", problems[0][1])

    def test_a_group_already_corrected_gets_the_next_free_key(self):
        # the group's first correction is live (vatfix-manual-5, submitted); the loader would skip that key, so the new one is -2
        live = [gl("JV-1", PURCHASE, debit=100), gl("JV-1", VORSTEUER, debit=8.10), gl("JV-1", BANK, credit=108.10),
                gl("JV-2", PURCHASE, debit=0)]
        vouchers = [voucher("JV-1", "manual-5"), voucher("JV-2", "vatfix-manual-5")]
        journal = [journal_line(1, 21, 11, 108.10)]
        export = [entry("5", lines=[line(21, 11, 108.10, tax_id=35, tax_account_id=21)])]
        documents, problems, _, _ = imf.plan(export, vouchers, live, lookups(), CURRENCIES, journal, {"5": "r"})
        self.assertEqual(problems, [])
        self.assertEqual([d["bexio_id"] for d in documents], ["vatfix-manual-5-2"])

    def test_correction_key_is_the_first_free_one(self):
        self.assertEqual(imf.correction_key("5", []), "vatfix-manual-5")
        self.assertEqual(imf.correction_key("5", ["manual-5", "vatfix-manual-5"]), "vatfix-manual-5-2")
        self.assertEqual(imf.correction_key("5", ["vatfix-manual-5", "vatfix-manual-5-2"]), "vatfix-manual-5-3")

    def test_reasons_file_lists_bexio_ids_with_their_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "wins.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# entries bexio books differently\n\n7001 test reason, with a comma\n")
            self.assertEqual(imf.load_journal_wins(path), {"7001": "test reason, with a comma"})
            self.assertEqual(imf.load_journal_wins(os.path.join(tmp, "missing.txt")), {})

    def test_reasons_file_line_without_reason_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "wins.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("7001\n")
            with self.assertRaises(ValueError):
                imf.load_journal_wins(path)


class ReportTest(unittest.TestCase):
    def test_report_prints_counts_and_totals_per_year_and_account(self):
        documents = [{"values": {"posting_date": "2025-06-30", "accounts": [
            {"account": VORSTEUER, "debit_in_account_currency": 8.1}, {"account": PURCHASE, "credit_in_account_currency": 8.1}]}}]
        counts = {"live groups": 1, "corrections": 1, "unchanged": 0, "not live": 0}
        text = imf.report(counts, documents, {VORSTEUER: "1170", PURCHASE: "5001"})
        self.assertIn("nothing was written", text)
        self.assertIn("2025", text)
        self.assertIn("1170", text)
        self.assertNotIn("Testaufwand", text)


class MainTest(unittest.TestCase):
    def test_write_puts_the_loader_plan_in_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ime.ENTRIES_FILE), "w", encoding="utf-8") as f:
                json.dump([entry("5", lines=[line(21, 11, 108.10, tax_id=35, tax_account_id=21)])], f)
            with open(os.path.join(tmp, ime.CURRENCIES_FILE), "w", encoding="utf-8") as f:
                json.dump([{"id": 1, "name": "CHF"}], f)
            out_file = os.path.join(tmp, "plan.json")
            vouchers = [voucher("JV-1", "manual-5")]
            rows = [gl("JV-1", PURCHASE, debit=108.10), gl("JV-1", BANK, credit=108.10)]
            with mock.patch.object(im, "PRIVATE", tmp), mock.patch.object(im.Erp, "from_file", return_value=None), \
                    mock.patch.object(ime.Lookups, "from_erp", return_value=lookups()), \
                    mock.patch.object(imf, "read_vouchers_and_gl", return_value=(vouchers, rows, {VORSTEUER: "1170", PURCHASE: "5001"})), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(imf.main(["--write", out_file, "--export", tmp]), 0)
            with open(out_file, encoding="utf-8") as f:
                plan = json.load(f)
        self.assertEqual([d["bexio_id"] for d in plan["documents"]], ["vatfix-manual-5"])
        self.assertEqual(plan["exchange_rates"], [])

    def test_no_mode_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                imf.main(["--export", "/nonexistent"])
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
