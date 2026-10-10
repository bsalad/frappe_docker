"""Offline tests for the Bank Statement Upload record and the Comment that camt_import leaves on a stamp.

Invented data only (an invented account name, file path and counts), no database, no network. The module
imports frappe, so run it in the image (finance/docs/erpnext-setup.md):

    docker run --rm -v "$PWD/finance/apps/bi_finance:/home/frappe/bi_finance_src:ro" \
        frappe-finance-custom:v16.50.0-swiss-bi6 \
        sh -c 'cd /home/frappe/bi_finance_src && ../frappe-bench/env/bin/python -m unittest -v bi_finance.test_bank_statement_upload'
"""

import datetime
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import frappe

from bi_finance import camt_import
from bi_finance.bi_finance.doctype.bank_statement_upload import bank_statement_upload as record

RESULT = {
    "bank_account": "UBS Kontokorrent Test", "iban": "CH5604835012345678009", "version": "camt.053.001.08",
    "currency": "CHF", "imported": 0,
    "counts": {
        "new": 2, "id": 1, "bexio": 1, "skipped": {"not booked": 1, "covered": 0},
        "by_month": {"2026-10": {"new": 2, "id": 1, "bexio": 1}},
    },
    "balances": {
        "OPBD": {"date": "2026-10-01", "file": 1000.0, "ledger": 1000.0, "diff": 0.0},
        "CLBD": {"date": "2026-10-06", "file": 1030.0, "ledger": 1027.5, "diff": 2.5},
    },
}


def upload(status="Draft", statement_file="/private/files/invented-camt.xml", bank_account="UBS Kontokorrent Test", name="BSU-0001"):
    """A record without a database: the fields set directly, as payment_run's tests do."""
    doc = record.BankStatementUpload.__new__(record.BankStatementUpload)
    doc.__dict__.update(
        doctype="Bank Statement Upload", name=name, status=status, statement_file=statement_file,
        bank_account=bank_account, summary=None, detected_version=None, imported_count=None,
        imported_on=None, imported_by=None,
    )
    doc.save = mock.Mock()
    doc.db_set = mock.Mock()
    return doc


class SummaryText(unittest.TestCase):
    def test_dry_run_summary_lines(self):
        self.assertEqual(record.summary_text(RESULT), "\n".join([
            "Bank Account UBS Kontokorrent Test (IBAN CH5604835012345678009, CHF)",
            "File camt.053.001.08",
            "Transactions: 2 new, 1 already on the account, 1 matched to bexio lines",
            "Skipped: 1 not booked, 0 covered by a details entry",
            "By booking month:",
            "  2026-10: 2 new, 1 already on the account, 1 matched to bexio lines",
            "Balance check (reported, never blocks):",
            "  OPBD 2026-10-01: file 1000.00, ledger 1000.00, difference 0.00",
            "  CLBD 2026-10-06: file 1030.00, ledger 1027.50, difference 2.50",
        ]))

    def test_camt054_has_no_balance_lines(self):
        result = dict(RESULT, version="camt.054.001.04", balances={})
        self.assertNotIn("Balance check", record.summary_text(result))

    def test_no_month_without_transactions(self):
        result = dict(RESULT, counts=dict(RESULT["counts"], by_month={}))
        self.assertNotIn("By booking month", record.summary_text(result))


class DryRun(unittest.TestCase):
    def setUp(self):
        self.doc = upload()
        self.addCleanup(mock.patch.stopall)

    def test_records_the_summary_and_the_version(self):
        with mock.patch.object(camt_import, "dry_run", return_value=RESULT) as dry:
            out = self.doc.run_dry_run()
        dry.assert_called_once_with("/private/files/invented-camt.xml", "UBS Kontokorrent Test")
        self.assertEqual((self.doc.status, self.doc.detected_version), ("Dry run", "camt.053.001.08"))
        self.assertTrue(self.doc.summary.startswith("Bank Account UBS Kontokorrent Test"))
        self.assertEqual(out["status"], "Dry run")
        self.doc.save.assert_called_once()

    def test_a_failed_dry_run_is_recorded_as_failed(self):
        db = mock.MagicMock()  # frappe.db is bound to a site: there is none here
        with mock.patch.object(camt_import, "dry_run", side_effect=RuntimeError("not XML")), \
                mock.patch.object(frappe, "db", new=db), \
                mock.patch.object(frappe, "throw", side_effect=RuntimeError("stopped")):
            with self.assertRaises(RuntimeError):
                self.doc.run_dry_run()
        db.rollback.assert_called_once()
        self.doc.db_set.assert_called_once_with({"status": "Failed", "summary": "not XML"}, update_modified=True)
        db.commit.assert_called_once()

    def test_an_imported_upload_is_not_dry_run_again(self):
        self.doc.status = "Imported"
        with mock.patch.object(camt_import, "dry_run") as dry, \
                mock.patch.object(frappe, "throw", side_effect=RuntimeError("stopped")):
            with self.assertRaises(RuntimeError):
                self.doc.run_dry_run()
        dry.assert_not_called()


class Import(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.doc = upload(status="Dry run")
        self.now = datetime.datetime(2026, 10, 10, 12, 30)

    def test_import_after_a_dry_run_records_count_and_who(self):
        imported = dict(RESULT, imported=2)
        with mock.patch.object(camt_import, "import_statement", return_value=imported) as write, \
                mock.patch.object(record, "now_datetime", return_value=self.now), \
                mock.patch.object(frappe, "session", SimpleNamespace(user="accounts@example.test")):
            self.doc.run_import()
        write.assert_called_once_with("/private/files/invented-camt.xml", "UBS Kontokorrent Test", upload="BSU-0001")
        self.assertEqual((self.doc.status, self.doc.imported_count), ("Imported", 2))
        self.assertEqual((self.doc.imported_on, self.doc.imported_by), (self.now, "accounts@example.test"))
        self.assertTrue(self.doc.summary.endswith("\nImported: 2"))
        self.doc.save.assert_called_once()

    def test_import_without_a_dry_run_is_refused(self):
        self.doc.status = "Draft"
        with mock.patch.object(camt_import, "import_statement") as write, \
                mock.patch.object(frappe, "throw", side_effect=RuntimeError("stopped")):
            with self.assertRaises(RuntimeError):
                self.doc.run_import()
        write.assert_not_called()

    def test_a_second_import_is_refused(self):
        self.doc.status = "Imported"
        self.doc.imported_on = self.now
        with mock.patch.object(camt_import, "import_statement") as write, \
                mock.patch.object(frappe, "throw", side_effect=RuntimeError("stopped")) as throw:
            with self.assertRaises(RuntimeError):
                self.doc.run_import()
        write.assert_not_called()
        self.assertIn("imports once", throw.call_args[0][0])

    def test_a_failed_import_leaves_failed_and_nothing_of_the_run(self):
        db = mock.MagicMock()
        with mock.patch.object(camt_import, "import_statement", side_effect=RuntimeError("IBAN mismatch")), \
                mock.patch.object(frappe, "db", new=db), \
                mock.patch.object(frappe, "throw", side_effect=RuntimeError("stopped")):
            with self.assertRaises(RuntimeError):
                self.doc.run_import()
        self.assertEqual(db.mock_calls[0], mock.call.rollback())
        self.doc.db_set.assert_called_once_with({"status": "Failed", "summary": "IBAN mismatch"}, update_modified=True)
        self.assertEqual(db.mock_calls[-1], mock.call.commit())
        self.assertIsNone(self.doc.imported_count)


class Validate(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.doc = upload(status="Dry run")
        self.doc.summary = "old summary"
        self.doc.detected_version = "camt.053.001.08"

    def before(self, **fields):
        values = dict(statement_file="/private/files/invented-camt.xml", bank_account="UBS Kontokorrent Test", status="Dry run")
        values.update(fields)
        return SimpleNamespace(**values)

    def test_unchanged_keeps_the_dry_run(self):
        with mock.patch.object(self.doc, "get_doc_before_save", return_value=self.before()):
            self.doc.validate()
        self.assertEqual((self.doc.status, self.doc.summary), ("Dry run", "old summary"))

    def test_a_new_file_needs_its_own_dry_run(self):
        self.doc.statement_file = "/private/files/other.xml"
        with mock.patch.object(self.doc, "get_doc_before_save", return_value=self.before()):
            self.doc.validate()
        self.assertEqual((self.doc.status, self.doc.summary, self.doc.detected_version), ("Draft", None, None))

    def test_a_new_account_needs_its_own_dry_run(self):
        self.doc.bank_account = "UBS Other Test"
        with mock.patch.object(self.doc, "get_doc_before_save", return_value=self.before()):
            self.doc.validate()
        self.assertEqual(self.doc.status, "Draft")

    def test_an_imported_upload_keeps_its_file_and_account(self):
        self.doc.status = "Imported"
        self.doc.statement_file = "/private/files/other.xml"
        with mock.patch.object(self.doc, "get_doc_before_save", return_value=self.before(status="Imported")), \
                mock.patch.object(frappe, "throw", side_effect=RuntimeError("stopped")):
            with self.assertRaises(RuntimeError):
                self.doc.validate()

    def test_a_new_record_has_nothing_to_compare(self):
        with mock.patch.object(self.doc, "get_doc_before_save", return_value=None):
            self.doc.validate()
        self.assertEqual(self.doc.status, "Dry run")


class Doctype(unittest.TestCase):
    """The .json the site loads: the fields, the status choices, the naming and the roles of the brief."""

    def setUp(self):
        with open(record.__file__.replace(".py", ".json")) as handle:
            self.meta = json.load(handle)

    def test_fields_and_status_choices(self):
        fields = {field["fieldname"]: field["fieldtype"] for field in self.meta["fields"]}
        self.assertEqual(fields, {
            "bank_account": "Link", "statement_file": "Attach", "detected_version": "Data", "status": "Select",
            "summary": "Long Text", "imported_count": "Int", "imported_on": "Datetime", "imported_by": "Link",
        })
        status = next(field for field in self.meta["fields"] if field["fieldname"] == "status")
        self.assertEqual(status["options"].split("\n"), ["Draft", "Dry run", "Imported", "Failed"])

    def test_naming_module_and_not_submittable(self):
        self.assertEqual(self.meta["autoname"], "BSU-.####")
        self.assertEqual(self.meta["module"], "BI Finance")
        self.assertFalse(self.meta.get("is_submittable"))

    def test_roles_read_and_write_and_nothing_else(self):
        roles = {perm["role"]: perm for perm in self.meta["permissions"]}
        self.assertEqual(set(roles), {"Accounts Manager", "Accounts User"})
        for perm in roles.values():
            self.assertEqual((perm["read"], perm["write"], perm["create"], perm["delete"]), (1, 1, 1, 0))


class StampComment(unittest.TestCase):
    """camt_import stamps a bexio line with db_set; the Comment on that line names the upload and the date."""

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.decisions = [{"rule": "bexio", "bexio": "BX-1", "reference": "AS-1"}]

    def write(self, upload=None):
        bank_transaction = mock.MagicMock()
        comment = mock.MagicMock()
        with mock.patch.object(frappe, "get_doc", side_effect=[bank_transaction, comment]) as get_doc, \
                mock.patch.object(camt_import, "today", return_value="2026-10-10"):
            camt_import._write({"name": "UBS Kontokorrent Test"}, self.decisions, upload)
        return bank_transaction, get_doc, comment

    def test_the_stamp_leaves_a_comment_naming_the_upload_and_the_date(self):
        bank_transaction, get_doc, comment = self.write(upload="BSU-0001")
        bank_transaction.db_set.assert_called_once_with("transaction_id", "AS-1", update_modified=False)
        get_doc.assert_any_call("Bank Transaction", "BX-1")
        get_doc.assert_called_with({
            "doctype": "Comment", "comment_type": "Info",
            "reference_doctype": "Bank Transaction", "reference_name": "BX-1",
            "content": "transaction_id AS-1 stamped by Bank Statement Upload BSU-0001 on 2026-10-10",
        })
        comment.insert.assert_called_once_with(ignore_permissions=True)

    def test_a_stamp_without_an_upload_says_so(self):
        _, get_doc, _ = self.write()
        self.assertEqual(get_doc.call_args[0][0]["content"], "transaction_id AS-1 stamped by a camt import on 2026-10-10")


if __name__ == "__main__":
    unittest.main()
