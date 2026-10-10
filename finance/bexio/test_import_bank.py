"""Offline tests for import_bank.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import import_bank as ib
import import_master as im

# the lookups the mapping reads; the ids and names are invented
LOOKUPS = {
    "currency": {"1": "CHF", "2": "EUR"},
    "bank_account": {
        "11": {"name": "Hauptkonto - Testbank AG", "currency": "CHF"},
        "12": {"name": "Fremdwaehrung - Zweitbank AG", "currency": "EUR"},
    },
}

# the export's shape: amount unsigned, type gives the direction, status says whether bexio booked it
CHF_IN = {"id": 9001, "bank_account_id": 11, "currency_id": 1, "value_date": "2026-03-31", "book_date": "2026-03-31",
          "amount": 1500.0, "type": "CREDIT", "status": "reconciled",
          "description": "PMNT.RCDT.VCOM", "title": "Zahlung Rechnung 1001"}
CHF_OUT = {"id": 9002, "bank_account_id": 11, "currency_id": 1, "value_date": "2026-04-02", "book_date": "2026-04-02",
           "amount": 250.5, "type": "DEBIT", "status": "unreconciled",
           "description": "PMNT.ICDT.DMCT", "title": "Miete"}
EUR_OUT = {"id": 9003, "bank_account_id": 12, "currency_id": 2, "value_date": "2025-12-31", "book_date": "2025-12-31",
           "amount": 99.99, "type": "DEBIT", "status": "auto_reconciled",
           "description": "PMNT.ICDT.AUTT", "title": "Software"}


def record(base, **changes):
    """A copy of a base record with some fields changed."""
    doc = copy.deepcopy(base)
    doc.update(changes)
    return doc


class FakeErp:
    """Answers the one read the lookups make: the Bank Accounts that have a bexio_id."""

    def list(self, doctype, filters=None, fields=("name",)):
        assert doctype == "Bank Account", doctype
        return [{"name": "Hauptkonto - Testbank AG", "bexio_id": "11"}]


class MappingTest(unittest.TestCase):
    def test_money_in_is_a_deposit(self):
        doc = ib.bank_transaction(CHF_IN, LOOKUPS)
        self.assertEqual(doc["doctype"], "Bank Transaction")
        self.assertEqual(doc["company"], im.COMPANY)
        self.assertEqual((doc["bexio_id"], doc["date"]), ("9001", "2026-03-31"))
        self.assertEqual(doc["bank_account"], "Hauptkonto - Testbank AG")
        self.assertEqual((doc["currency"], doc["deposit"], doc["withdrawal"]), ("CHF", 1500.0, 0.0))
        self.assertEqual((doc["description"], doc["reference_number"]), ("Zahlung Rechnung 1001", ""))

    def test_money_out_is_a_withdrawal(self):
        doc = ib.bank_transaction(CHF_OUT, LOOKUPS)
        self.assertEqual((doc["deposit"], doc["withdrawal"]), (0.0, 250.5))

    def test_foreign_currency_account_keeps_its_currency(self):
        doc = ib.bank_transaction(EUR_OUT, LOOKUPS)
        self.assertEqual(doc["bank_account"], "Fremdwaehrung - Zweitbank AG")
        self.assertEqual((doc["currency"], doc["deposit"], doc["withdrawal"]), ("EUR", 0.0, 99.99))

    def test_transaction_in_another_currency_than_its_account_is_unmapped(self):
        with self.assertRaisesRegex(ib.Unmapped, "differs from its bank account's CHF"):
            ib.bank_transaction(record(CHF_IN, currency_id=2), LOOKUPS)

    def test_bank_account_without_a_bexio_match_is_unmapped_by_id(self):
        with self.assertRaisesRegex(ib.Unmapped, "bank account 99 has no Bank Account"):
            ib.bank_transaction(record(CHF_IN, bank_account_id=99), LOOKUPS)

    def test_zero_amount_missing_date_and_unknown_type_are_unmapped(self):
        with self.assertRaisesRegex(ib.Unmapped, "zero amount"):
            ib.bank_transaction(record(CHF_IN, amount=0), LOOKUPS)
        with self.assertRaisesRegex(ib.Unmapped, "no value date"):
            ib.bank_transaction(record(CHF_IN, value_date=None), LOOKUPS)
        with self.assertRaisesRegex(ib.Unmapped, "neither CREDIT nor DEBIT"):
            ib.bank_transaction(record(CHF_IN, type="TRANSFER"), LOOKUPS)

    def test_the_booking_link_is_not_mapped(self):
        # the export has no field that names the booking; the posting plan's rule (D7) decides where it goes
        doc = ib.bank_transaction(record(CHF_IN, booked_with="kb_invoice:1001"), LOOKUPS)
        self.assertNotIn("bexio_booked_with", doc)
        self.assertNotIn("booked_with", doc)


class PlanTest(unittest.TestCase):
    def test_unmapped_records_are_reported_not_raised(self):
        results = ib.plan([CHF_IN, record(CHF_IN, id=9009, bank_account_id=99)], LOOKUPS)
        self.assertEqual([r["error"] is None for r in results], [True, False])
        self.assertEqual(results[1]["bexio_id"], "9009")

    def test_booked_follows_bexio_status(self):
        results = ib.plan([CHF_IN, CHF_OUT, EUR_OUT, record(CHF_IN, id=9010, status="ignored")], LOOKUPS)
        self.assertEqual([r["booked"] for r in results], [True, False, True, False])

    def test_fields_the_doctype_lacks_are_named(self):
        meta = {"doctype", "company", "bexio_id", "date", "bank_account", "currency", "deposit", "withdrawal",
                "description", "reference_number"}
        results = ib.plan([CHF_IN], LOOKUPS, meta)
        self.assertEqual(results[0]["unknown"], [])
        results = ib.plan([CHF_IN], LOOKUPS, meta - {"reference_number"})
        self.assertEqual(results[0]["unknown"], ["Bank Transaction.reference_number"])


class SummaryTest(unittest.TestCase):
    def test_totals_per_account_year_and_currency_are_not_converted(self):
        text = ib.summary(ib.plan([CHF_IN, CHF_OUT, EUR_OUT], LOOKUPS))
        rows = {tuple(line.split()[:3]): line.split()[3:] for line in text.splitlines()
                if line.split()[:1] in (["11"], ["12"])}
        self.assertEqual(rows[("11", "2026", "CHF")], ["2", "1,500.00", "250.50"])
        self.assertEqual(rows[("12", "2025", "EUR")], ["1", "0.00", "99.99"])

    def test_summary_names_no_bank_account(self):
        text = ib.summary(ib.plan([CHF_IN, EUR_OUT], LOOKUPS))
        self.assertNotIn("Hauptkonto", text)
        self.assertNotIn("Fremdwaehrung", text)

    def test_unmapped_and_booked_counts_are_in_the_summary(self):
        text = ib.summary(ib.plan([CHF_IN, record(CHF_IN, id=9009, bank_account_id=99), CHF_OUT], LOOKUPS))
        self.assertIn("records: 3, mapped: 2, unmapped: 1", text)
        self.assertIn("booked in bexio (reconciled or auto_reconciled): 1, not booked: 1", text)


class LookupTest(unittest.TestCase):
    def test_bank_account_gets_the_currency_of_the_export_and_its_erp_name(self):
        data = {"currencies": [{"id": 1, "name": "CHF"}, {"id": 2, "name": "EUR"}],
                "bank_accounts": [{"id": 11, "currency_id": 1}, {"id": 12, "currency_id": 2}]}
        lookups = ib.lookups_from_erp(FakeErp(), data)
        self.assertEqual(lookups["bank_account"], {"11": {"name": "Hauptkonto - Testbank AG", "currency": "CHF"}})
        self.assertEqual(lookups["currency"], {"1": "CHF", "2": "EUR"})


class MainTest(unittest.TestCase):
    def test_without_the_export_file_it_says_not_exported_yet(self):
        with tempfile.TemporaryDirectory() as export, \
                mock.patch.object(im.Erp, "from_file", side_effect=AssertionError("ERPNext must not be read")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ib.main(["--dry-run", "--export", export]), 0)
        self.assertIn("not exported yet", out.getvalue())

    def _export(self, export, rows):
        files = {"bank_transactions": rows, "bank_accounts": [{"id": 11, "currency_id": 1}],
                 "currencies": [{"id": 1, "name": "CHF"}]}
        for name, body in files.items():
            with open(os.path.join(export, name + ".json"), "w") as f:
                json.dump(body, f)

    def _erp_fields(self):
        class Erp(FakeErp):
            def meta(self, doctype):
                return {"fields": [{"fieldname": f} for f in ("bexio_id", "date", "bank_account", "currency", "deposit",
                                                             "withdrawal", "description", "reference_number", "company")]}
        return Erp()

    def test_dry_run_with_the_export_prints_totals_and_writes_only_the_private_report(self):
        with tempfile.TemporaryDirectory() as export:
            self._export(export, [CHF_IN, record(CHF_IN, id=9009, bank_account_id=99)])
            report = os.path.join(export, "report.txt")
            with mock.patch.object(im.Erp, "from_file", return_value=self._erp_fields()), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                code = ib.main(["--dry-run", "--export", export, "--report", report])
            with open(report) as f:
                self.assertEqual(f.read(), "Bank Transaction 9009: unmapped: bank account 99 has no Bank Account with that bexio_id\n")
            self.assertFalse(os.path.exists(os.path.join(export, "bank-docs.json")))
        self.assertEqual(code, 1)
        self.assertIn("records: 2, mapped: 1, unmapped: 1", out.getvalue())
        self.assertIn("dry run: nothing was written to ERPNext", out.getvalue())

    def test_write_keeps_the_documents_in_a_private_file_for_the_loader(self):
        with tempfile.TemporaryDirectory() as export:
            self._export(export, [CHF_IN, CHF_OUT])
            docs = os.path.join(export, "bank-docs.json")
            report = os.path.join(export, "report.txt")
            with mock.patch.object(im.Erp, "from_file", return_value=self._erp_fields()), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                code = ib.main(["--write", docs, "--export", export, "--report", report])
            with open(docs) as f:
                written = json.load(f)["documents"]
        self.assertEqual(code, 0)
        self.assertEqual([d["bexio_id"] for d in written], ["9001", "9002"])
        self.assertEqual([d["booked"] for d in written], [True, False])
        self.assertEqual(written[0]["values"]["deposit"], 1500.0)
        self.assertNotIn("doctype", written[0]["values"])
        self.assertIn("wrote 2 documents", out.getvalue())

    def test_a_run_without_dry_run_or_write_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ib.main([])


if __name__ == "__main__":
    unittest.main()
