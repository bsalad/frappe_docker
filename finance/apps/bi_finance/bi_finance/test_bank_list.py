"""Offline checks of the Booking Date and the Desk list of Bank Transaction: the fixtures, the feeds that fill the date,
and the Treasury shortcuts to the Desk pages. Invented data only (made-up accounts, references and amounts), no
database, no network. The module imports frappe, so run it in the image (finance/docs/erpnext-setup.md):

    docker run --rm -v "$PWD/finance/apps/bi_finance:/home/frappe/bi_finance_src:ro" \
        frappe-finance-custom:v16.50.0-swiss-bi9 \
        sh -c 'cd /home/frappe/bi_finance_src && ../frappe-bench/env/bin/python -m unittest -v bi_finance.test_bank_list'
"""

import json
import os
import unittest
from decimal import Decimal
from unittest import mock

import frappe

from bi_finance import bank_feed, bank_list, camt_import, hooks

PACKAGE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(PACKAGE, "fixtures")
# The module folder: the Treasury files Frappe syncs (see test_layout.py).
HERE = os.path.join(PACKAGE, "bi_finance")
APPS = os.path.dirname(os.path.dirname(os.path.dirname(frappe.__file__)))
BANK_TRANSACTION = os.path.join(APPS, "erpnext", "erpnext", "accounts", "doctype", "bank_transaction", "bank_transaction.json")
BANK_RECONCILIATION_TOOL = os.path.join(APPS, "erpnext", "erpnext", "accounts", "doctype", "bank_reconciliation_tool", "bank_reconciliation_tool.json")

# the list's columns, in the order Benchi reads them
COLUMNS = ["booking_date", "value_date", "bank_account", "description", "deposit", "withdrawal", "status", "unallocated_amount"]


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fixture(name):
    return load(os.path.join(FIXTURES, name))


def bank_transaction_fields():
    # ERPNext's fields, plus Booking Date and Value Date, which the custom fields add
    return {f["fieldname"]: f for f in load(BANK_TRANSACTION)["fields"]}


class Fixtures(unittest.TestCase):
    def test_every_key_of_a_fixture_is_a_field_of_its_doctype(self):
        # the meta fields every exported document carries, next to the doctype's own
        meta = {"doctype", "name", "docstatus"}
        doctypes = {
            "custom_field.json": os.path.join(os.path.dirname(frappe.__file__), "custom", "doctype", "custom_field", "custom_field.json"),
            "property_setter.json": os.path.join(os.path.dirname(frappe.__file__), "custom", "doctype", "property_setter", "property_setter.json"),
            "list_view_settings.json": os.path.join(os.path.dirname(frappe.__file__), "desk", "doctype", "list_view_settings", "list_view_settings.json"),
        }
        for name, path in doctypes.items():
            fields = {f["fieldname"] for f in load(path)["fields"]} | meta
            for doc in fixture(name):
                self.assertTrue(set(doc) <= fields, (name, set(doc) - fields))

    def test_the_hook_declares_the_three_fixture_files(self):
        self.assertEqual(sorted(os.listdir(FIXTURES)), ["custom_field.json", "list_view_settings.json", "property_setter.json"])
        self.assertEqual(sorted(f["dt"] for f in hooks.fixtures), ["Custom Field", "List View Settings", "Property Setter"])

    def test_the_custom_fields_are_booking_date_and_value_date_on_bank_transaction(self):
        fields = {f["fieldname"]: f for f in fixture("custom_field.json")}
        self.assertEqual(sorted(fields), ["booking_date", "value_date"])
        field = fields["booking_date"]
        self.assertEqual(field["doctype"], "Custom Field")
        self.assertEqual(field["name"], "Bank Transaction-booking_date")
        self.assertEqual(field["dt"], "Bank Transaction")
        self.assertEqual(field["fieldtype"], "Date")
        self.assertEqual(field["insert_after"], "date")
        self.assertEqual(field["module"], "BI Finance")
        # the standard filter Benchi asked for
        self.assertEqual(field["in_standard_filter"], 1)

    def test_the_value_date_is_a_date_column_after_booking_date_in_the_list_and_the_filters(self):
        field = {f["fieldname"]: f for f in fixture("custom_field.json")}["value_date"]
        self.assertEqual(field["doctype"], "Custom Field")
        self.assertEqual(field["name"], "Bank Transaction-value_date")
        self.assertEqual(field["dt"], "Bank Transaction")
        self.assertEqual(field["label"], "Value Date")
        self.assertEqual(field["fieldtype"], "Date")
        self.assertEqual(field["insert_after"], "booking_date")
        self.assertEqual(field["module"], "BI Finance")
        self.assertEqual((field["in_list_view"], field["in_standard_filter"]), (1, 1))

    def test_the_list_sorts_by_booking_date_newest_first(self):
        settings = {p["property"]: p for p in fixture("property_setter.json")}
        self.assertEqual(sorted(settings), ["sort_field", "sort_order"])
        self.assertEqual(settings["sort_field"]["value"], "booking_date")
        self.assertEqual(settings["sort_order"]["value"], "DESC")
        for name, p in settings.items():
            self.assertEqual(p["doctype"], "Property Setter")
            self.assertEqual(p["doc_type"], "Bank Transaction")
            self.assertEqual(p["name"], "Bank Transaction-main-" + name)

    def test_the_list_columns_are_the_seven_in_order_and_each_is_a_field(self):
        [settings] = fixture("list_view_settings.json")
        self.assertEqual(settings["doctype"], "List View Settings")
        self.assertEqual(settings["name"], "Bank Transaction")
        columns = json.loads(settings["fields"])
        self.assertEqual([c["fieldname"] for c in columns], COLUMNS)
        self.assertTrue(all(c["label"] for c in columns))
        fields = set(bank_transaction_fields()) | {"booking_date", "value_date"}
        self.assertTrue(set(COLUMNS) <= fields, set(COLUMNS) - fields)

    def test_the_standard_filters_are_bank_account_status_and_both_dates(self):
        standard = {name for name, f in bank_transaction_fields().items() if f.get("in_standard_filter")}
        self.assertTrue({"bank_account", "status"} <= standard)
        self.assertEqual({f["fieldname"] for f in fixture("custom_field.json") if f["in_standard_filter"]}, {"booking_date", "value_date"})


class Feeds(unittest.TestCase):
    def test_a_feed_row_is_booked_on_its_own_date(self):
        row = {"transaction_id": "wise:7:1", "date": "2026-03-04", "deposit": 40.0, "withdrawal": 0.0,
               "currency": "CHF", "description": "Invoice 77", "reference_number": "77"}
        db = mock.Mock()
        db.get_value.return_value = "Testfirma AG"
        db.exists.return_value = False
        with mock.patch.object(bank_feed.frappe, "db", db), \
                mock.patch.object(bank_feed.frappe, "get_doc", return_value=mock.Mock()) as get_doc:
            self.assertEqual(bank_feed.write("Wise CHF Test", [row]), 1)
        values = get_doc.call_args.args[0]
        self.assertEqual(values["booking_date"], "2026-03-04")
        self.assertEqual(values["value_date"], "2026-03-04")
        self.assertEqual(values["date"], "2026-03-04")

    def test_a_camt_line_is_booked_on_its_booking_date(self):
        tx = {"rule": "new", "reference": "camt-ref-1", "booking_date": "2026-03-05", "value_date": "2026-03-04",
              "deposit": Decimal("0"), "withdrawal": Decimal("12.50"), "reference_number": "", "description": "Test",
              "bank_party_name": "", "bank_party_iban": ""}
        account = {"name": "UBS Test", "company": "Testfirma AG", "currency": "CHF"}
        with mock.patch.object(camt_import.frappe, "get_doc", return_value=mock.Mock()) as get_doc:
            self.assertEqual(camt_import._write(account, [tx]), 1)
        values = get_doc.call_args.args[0]
        self.assertEqual(values["booking_date"], "2026-03-05")
        self.assertEqual(values["date"], "2026-03-05")
        # the statement's ValDt is the value date, its BookgDt the booking date
        self.assertEqual(values["value_date"], "2026-03-04")

    def test_a_camt_line_without_a_value_date_takes_its_booking_date(self):
        tx = {"rule": "new", "reference": "camt-ref-2", "booking_date": "2026-03-05", "value_date": None,
              "deposit": Decimal("0"), "withdrawal": Decimal("3.00"), "reference_number": "", "description": "Test",
              "bank_party_name": "", "bank_party_iban": ""}
        account = {"name": "UBS Test", "company": "Testfirma AG", "currency": "CHF"}
        with mock.patch.object(camt_import.frappe, "get_doc", return_value=mock.Mock()) as get_doc:
            self.assertEqual(camt_import._write(account, [tx]), 1)
        self.assertEqual(get_doc.call_args.args[0]["value_date"], "2026-03-05")


class BankDates(unittest.TestCase):
    """The whitelisted method the backfill calls: fills an empty Booking Date or Value Date, never a set one."""

    def setUp(self):
        self.db = mock.Mock()
        self.patches = [mock.patch.object(bank_list.frappe, "db", self.db), mock.patch.object(bank_list.frappe, "only_for")]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()

    def test_the_booking_date_is_the_default_field(self):
        self.db.get_value.return_value = None
        self.assertEqual(bank_list.set_booking_dates({"BT-1": "2026-03-31"}), 1)
        self.db.set_value.assert_called_once_with("Bank Transaction", "BT-1", "booking_date", mock.ANY, update_modified=False)

    def test_a_value_date_fills_only_the_lines_whose_value_date_is_empty(self):
        self.db.get_value.side_effect = lambda doctype, name, field: "2026-03-30" if name == "BT-1" else None
        self.assertEqual(bank_list.set_booking_dates({"BT-1": "2026-03-30", "BT-2": "2026-04-02"}, field="value_date"), 1)
        self.db.get_value.assert_any_call("Bank Transaction", "BT-1", "value_date")
        self.db.set_value.assert_called_once_with("Bank Transaction", "BT-2", "value_date", mock.ANY, update_modified=False)

    def test_another_field_of_the_line_is_refused(self):
        with mock.patch.object(bank_list.frappe, "throw", side_effect=ValueError("refused")):
            with self.assertRaises(ValueError):
                bank_list.set_booking_dates({"BT-1": "2026-03-30"}, field="docstatus")
        self.db.set_value.assert_not_called()


class Treasury(unittest.TestCase):
    def setUp(self):
        self.ws = load(os.path.join(HERE, "workspace", "treasury", "treasury.json"))
        self.rows = {r["label"]: r for r in self.ws["shortcuts"]}

    def test_the_bank_shortcuts_open_the_desk_pages(self):
        expected = {
            "Bank Transactions": ("DocType", "Bank Transaction"),
            "Unreconciled bank lines": ("URL", "/app/bank-transaction?status=Unreconciled"),
            "Bank Reconciliation Tool": ("DocType", "Bank Reconciliation Tool"),
            "Bank Statement Upload": ("DocType", "Bank Statement Upload"),
        }
        for label, (kind, target) in expected.items():
            row = self.rows[label]
            self.assertEqual(row["type"], kind, label)
            self.assertEqual(row["link_to"] if kind == "DocType" else row["url"], target, label)

    def test_no_shortcut_opens_the_react_banking_page(self):
        for row in self.ws["shortcuts"]:
            self.assertNotIn("banking", (row.get("url", "") + " " + row.get("link_to", "")).lower(), row["label"])

    def test_the_bank_doctypes_exist(self):
        self.assertTrue(os.path.isfile(BANK_RECONCILIATION_TOOL), BANK_RECONCILIATION_TOOL)
        self.assertTrue(os.path.isfile(os.path.join(HERE, "doctype", "bank_statement_upload", "bank_statement_upload.json")))


if __name__ == "__main__":
    unittest.main()
