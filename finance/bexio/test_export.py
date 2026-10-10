"""Offline tests for export.py: files, manifest, permissions, gaps, documents, downloads. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import datetime
import json
import os
import stat
import tempfile
import unittest
from unittest import mock

import export
from client import BexioError, Client

FAKE = {
    "/2.0/contact": [{"id": 1, "name_1": "Muster AG"}, {"id": 2, "name_1": "Test GmbH"}],
    "/2.0/accounts": [{"id": 77, "account_no": "1020", "name": "Testbank"}],
    "/2.0/kb_invoice": [{"id": 5, "document_nr": "RE-1"}],
    "/4.0/purchase/bills": [{"id": "b1", "gross": "10"}],
    "/3.0/files": [
        {"id": 9, "extension": "pdf"},
        {"id": 10, "extension": "../x"},
        {"id": 11, "extension": "pdf"},
    ],
}

# Single-record answers: a record's detail (positions, lines) and its sub-resources.
DETAIL = {
    "/2.0/kb_invoice/5": {"id": 5, "positions": [{"amount": "1"}]},
    "/2.0/kb_invoice/5/payment": [{"value": "10"}],
    "/4.0/purchase/bills/b1": {"data": {"id": "b1", "lines": [{"text": "Papier"}], "document_no": "B-1",
                                        "bill_date": "2026-01-02", "attachment_ids": ["u-1", "u-2"]}},
}

# Raw file contents. File 10 answers only on its second candidate path; file 11 on none.
# The bill attachment u-1 is on the uuid's download path; u-2 on none.
RAW = {
    "/3.0/files/9/download": b"%PDF-invented",
    "/3.0/files/10/content": b"\x00\x01",
    "/3.0/files/u-1/download": b"%PDF-attachment",
}


def recording(client):
    """Wrap the client's get so that the paths it is asked for are listed."""
    paths = []
    real = client.get

    def get(path, params=None, raw=False):
        paths.append(path)
        return real(path, params, raw)

    client.get = get
    return paths


def fake_client(missing=()):
    """A Client whose GETs answer from the fixtures: [] for unknown lists, 404 for unknown files, 403 for `missing`."""
    c = Client(token="test-token-not-real")

    def fake_get(path, params=None, raw=False):
        if path in missing:
            raise BexioError(403, path)
        if raw:
            if path not in RAW:
                raise BexioError(404, path)
            return RAW[path]
        if path in DETAIL:
            return DETAIL[path]
        if params and params.get("offset"):
            return []
        body = FAKE.get(path, [])
        if path.startswith("/4.0/banking/"):
            return {"results": body}
        return {"data": body} if path.startswith("/4.0/") else body

    c.get = fake_get
    return c


class ExportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = os.path.join(self._tmp.name, "export")

    def load(self, name):
        with open(os.path.join(self.out, name), encoding="utf-8") as f:
            return json.load(f)

    def test_one_file_per_entity_with_the_records_unmasked(self):
        export.export(fake_client(), self.out)
        self.assertEqual(self.load("contacts.json"), FAKE["/2.0/contact"])
        self.assertEqual(self.load("accounts.json"), FAKE["/2.0/accounts"])
        for name in export.ENTITIES:
            self.assertTrue(os.path.exists(os.path.join(self.out, name + ".json")), name)

    def test_manifest_has_counts_and_status(self):
        manifest = export.export(fake_client(), self.out)
        self.assertEqual(manifest["entities"]["contacts"]["count"], 2)
        self.assertEqual(manifest["entities"]["contacts"]["status"], "ok")
        self.assertEqual(self.load("manifest.json"), manifest)

    def test_directory_is_700_and_files_are_600(self):
        os.umask(0o022)
        export.export(fake_client(), self.out)
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o700)
        for name in os.listdir(self.out):
            if name != "files":
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.out, name)).st_mode), 0o600, name)
        for name in os.listdir(os.path.join(self.out, "files")):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.out, "files", name)).st_mode), 0o600, name)

    def test_a_forbidden_entity_is_recorded_and_the_rest_still_written(self):
        manifest = export.export(fake_client(missing={"/2.0/accounts"}), self.out)
        entry = manifest["entities"]["accounts"]
        self.assertEqual(entry["status"], "error")
        self.assertIn("403", entry["error"])
        self.assertFalse(os.path.exists(os.path.join(self.out, "accounts.json")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "contacts.json")))
        self.assertEqual(export.failed_required(manifest), ["accounts"])

    def test_a_missing_lookup_is_not_a_failure(self):
        manifest = export.export(fake_client(missing={"/2.0/country"}), self.out)
        self.assertEqual(export.failed_required(manifest), [])

    def test_a_stale_file_does_not_survive_a_failed_entity(self):
        export.export(fake_client(), self.out)
        export.export(fake_client(missing={"/2.0/accounts"}), self.out)
        self.assertFalse(os.path.exists(os.path.join(self.out, "accounts.json")))

    def test_bills_are_read_page_numbered_and_carry_their_lines(self):
        c = fake_client()
        seen = []
        real = c.get
        c.get = lambda path, params=None, raw=False: (seen.append((path, dict(params or {}))), real(path, params, raw))[1]
        export.export(c, self.out, only=["bills"])
        bills = [p for path, p in seen if path == "/4.0/purchase/bills"]
        self.assertIn("page", bills[0])
        self.assertEqual(self.load("bills.json"), [{"id": "b1", "gross": "10", "lines": [{"text": "Papier"}],
                                                    "document_no": "B-1", "bill_date": "2026-01-02",
                                                    "attachment_ids": ["u-1", "u-2"]}])

    def test_bill_attachments_are_one_row_per_uuid_with_the_bill_and_its_content(self):
        manifest = export.export(fake_client(), self.out, only=["bill_attachments"])
        rows = self.load("bill_attachments.json")
        self.assertEqual([r["id"] for r in rows], ["u-1", "u-2"])
        self.assertEqual(rows[0], {"id": "u-1", "uuid": "u-1", "bill_id": "b1", "document_no": "B-1",
                                   "bill_date": "2026-01-02", "extension": "pdf", "name": "u-1.pdf",
                                   "size_in_bytes": len(b"%PDF-attachment")})
        with open(os.path.join(self.out, "files", "u-1.pdf"), "rb") as f:
            self.assertEqual(f.read(), b"%PDF-attachment")
        self.assertEqual(manifest["entities"]["bill_attachments"]["downloaded"], 1)
        self.assertEqual(manifest["entities"]["bill_attachments"]["failed"], ["u-2"])

    def test_a_row_without_an_extension_takes_it_from_the_content(self):
        c = Client(token="test-token-not-real")
        c.get = lambda path, params=None, raw=False: {"/3.0/files/u-3/download": b"\x89PNG\r\n\x1a\nxx"}[path]
        rows = [{"id": "u-3", "uuid": "u-3"}]
        export.download_files(c, rows, self.out, export.ATTACHMENT_CONTENT_PATHS)
        self.assertEqual((rows[0]["extension"], rows[0]["name"], rows[0]["size_in_bytes"]), ("png", "u-3.png", 10))
        self.assertTrue(os.path.exists(os.path.join(self.out, "files", "u-3.png")))

    def test_the_content_types_of_bexio_attachments_are_sniffed_by_their_first_bytes(self):
        self.assertEqual(export.sniff_extension(b"%PDF-1.7"), "pdf")
        self.assertEqual(export.sniff_extension(b"\xff\xd8\xff\xe1\x00"), "jpg")
        self.assertEqual(export.sniff_extension(b"\x89PNG\r\n\x1a\n"), "png")
        self.assertEqual(export.sniff_extension(b"<html>"), "bin")

    def test_a_failed_bill_attachment_keeps_its_row_without_a_size(self):
        export.export(fake_client(), self.out, only=["bill_attachments"])
        failed = [r for r in self.load("bill_attachments.json") if r["id"] == "u-2"]
        self.assertEqual(len(failed), 1)
        self.assertNotIn("size_in_bytes", failed[0])

    def test_invoices_carry_their_positions(self):
        export.export(fake_client(), self.out, only=["invoices"])
        self.assertEqual(self.load("invoices.json"), [{"id": 5, "document_nr": "RE-1", "positions": [{"amount": "1"}]}])

    def test_invoice_payments_are_read_per_invoice(self):
        export.export(fake_client(), self.out, only=["invoice_payments"])
        self.assertEqual(self.load("invoice_payments.json"), [{"parent_id": 5, "rows": [{"value": "10"}]}])

    def test_files_are_downloaded_by_id_and_checked_in_the_manifest(self):
        manifest = export.export(fake_client(), self.out, only=["files"])
        folder = os.path.join(self.out, "files")
        with open(os.path.join(folder, "9.pdf"), "rb") as f:
            self.assertEqual(f.read(), b"%PDF-invented")
        with open(os.path.join(folder, "10.x"), "rb") as f:  # the extension is cut to a plain word
            self.assertEqual(f.read(), b"\x00\x01")
        self.assertEqual(manifest["entities"]["files"]["count"], 3)
        self.assertEqual(manifest["entities"]["files"]["downloaded"], 2)
        self.assertEqual(manifest["entities"]["files"]["bytes"], len(b"%PDF-invented") + 2)
        self.assertEqual(manifest["entities"]["files"]["failed"], [11])

    def test_only_reruns_the_named_entities_and_keeps_the_rest_of_the_manifest(self):
        export.export(fake_client(), self.out)
        manifest = export.export(fake_client(missing={"/2.0/contact"}), self.out, only=["contacts"])
        self.assertEqual(manifest["entities"]["contacts"]["status"], "error")
        self.assertEqual(manifest["entities"]["accounts"]["status"], "ok")
        self.assertTrue(os.path.exists(os.path.join(self.out, "accounts.json")))
        self.assertEqual(self.load("manifest.json")["entities"]["accounts"]["count"], 1)

    def test_default_out_is_dated_under_private(self):
        import datetime

        self.assertEqual(
            export.default_out(datetime.date(2026, 10, 9)),
            "/Users/bsaladin/ws_yardr_finance/private/bexio-export/2026-10-09",
        )

    def test_export_issues_only_get_requests(self):
        c = Client(token="test-token-not-real")

        def answer(req, timeout=None):
            resp = mock.MagicMock()
            url = req.full_url
            if "/3.0/files/" in url:
                body = b"\x00"
            elif "/4.0/banking/" in url:
                body = b'{"results": []}'
            elif "/4.0/" in url:
                body = b'{"data": []}'
            else:
                body = b"[]"
            resp.__enter__.return_value.read.return_value = body
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=answer) as opened:
            export.export(c, self.out)
        methods = {call.args[0].get_method() for call in opened.call_args_list}
        self.assertEqual(methods, {"GET"})

    def test_export_scope_entities_go_through_the_export_client_only(self):
        read, write = fake_client(), fake_client()
        asked_read, asked_write = recording(read), recording(write)
        export.export(read, self.out, export_client=write)
        export_paths = ["/3.0/accounting/journal", "/3.0/accounting/manual_entries", "/3.0/banking/transactions", "/3.0/files"]
        for path in export_paths:
            self.assertIn(path, asked_write)
            self.assertNotIn(path, asked_read)
        self.assertIn("/3.0/files/9/download", asked_write)  # file contents need the file scope too
        self.assertNotIn("/3.0/files/9/download", asked_read)
        self.assertIn("/3.0/files/u-1/download", asked_write)  # and so do the bill attachments
        self.assertNotIn("/3.0/files/u-1/download", asked_read)
        self.assertIn("/2.0/contact", asked_read)
        self.assertNotIn("/2.0/contact", asked_write)

    def test_the_export_scope_set_is_the_five_entities_with_file_contents(self):
        self.assertEqual(export.EXPORT_SCOPE_ENTITIES,
                         {"manual_entries", "journal", "bank_transactions", "files", "bill_attachments"})

    def test_raw_get_returns_the_bytes_and_plain_get_decodes_json(self):
        c = Client(token="test-token-not-real")
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"\x89PNG"
        with mock.patch("urllib.request.urlopen", return_value=resp):
            self.assertEqual(c.get("/3.0/files/1/download", raw=True), b"\x89PNG")
        resp.__enter__.return_value.read.return_value = b'{"id": 1}'
        with mock.patch("urllib.request.urlopen", return_value=resp):
            self.assertEqual(c.get("/2.0/contact/1"), {"id": 1})


# Invented payroll: two employees, absences and payslips of January and February 2026 only.
PAYROLL_EMPLOYEES = [{"id": 101, "first_name": "Invented"}, {"id": 102, "first_name": "Testperson"}]
PAYROLL_PAYSLIPS = {
    (101, 2026, 1): b"%PDF-invented-1",
    (101, 2026, 2): b"%PDF-invented-2",
    (102, 2026, 1): b"%PDF-invented-3",
}
PAYROLL_TODAY = datetime.date(2026, 2, 15)


PAYROLL_OVERVIEW = [{"employee_id": 101, "year": 2026, "month": 1}]


def payroll_client(missing=()):
    """A Client whose payroll GETs answer from the invented fixtures; 404 for a payslip not in PAYROLL_PAYSLIPS."""
    c = Client(token="test-token-not-real")

    def fake_get(path, params=None, raw=False):
        if path in missing:
            raise BexioError(403, path)
        if path == export.PAYROLL_EMPLOYEES:
            return {"data": PAYROLL_EMPLOYEES}
        if path == export.PAYROLL_PAYSTUBS_OVERVIEW:
            return {"data": PAYROLL_OVERVIEW}
        if path in export.PAYROLL_COMPANY:
            return {"status": "invented"}
        if path.endswith("/absences"):
            return {"data": [{"reason": "Vacation", "days": 5, "year": params["year"]}]}
        _, _, _, _, employee, _, year, month = path.split("/")
        key = (int(employee), int(year), int(month))
        if not raw or key not in PAYROLL_PAYSLIPS:
            raise BexioError(404, path)
        return PAYROLL_PAYSLIPS[key]

    c.get = fake_get
    return c


class PayrollExportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = os.path.join(self._tmp.name, "payroll")

    def load(self, name):
        with open(os.path.join(self.out, name), encoding="utf-8") as f:
            return json.load(f)

    def test_employees_absences_and_payslips_are_written(self):
        export.export_payroll(payroll_client(), self.out, [2026], today=PAYROLL_TODAY)
        self.assertEqual(self.load("employees.json"), PAYROLL_EMPLOYEES)
        self.assertEqual(len(self.load("absences.json")), 2)
        self.assertEqual(self.load("absences.json")[0]["year"], 2026)
        with open(os.path.join(self.out, "paystubs", "101-2026-01.pdf"), "rb") as f:
            self.assertEqual(f.read(), PAYROLL_PAYSLIPS[(101, 2026, 1)])
        self.assertEqual([row["file"] for row in self.load("paystubs.json")],
                         ["101-2026-01.pdf", "101-2026-02.pdf", "102-2026-01.pdf"])

    def test_manifest_has_counts_per_entity_and_per_month_of_payslips(self):
        manifest = export.export_payroll(payroll_client(), self.out, [2026], today=PAYROLL_TODAY)
        self.assertEqual(manifest["entities"]["employees"]["count"], 2)
        self.assertEqual(manifest["entities"]["absences"]["count"], 2)
        self.assertEqual(manifest["entities"]["paystubs"]["count"], 3)
        self.assertEqual(manifest["entities"]["paystubs"]["by_month"], {"2026-01": 2, "2026-02": 1})
        self.assertEqual(self.load("manifest.json"), manifest)

    def test_overview_and_company_reads_are_exported_as_their_own_entities(self):
        manifest = export.export_payroll(payroll_client(), self.out, [2026], today=PAYROLL_TODAY)
        self.assertEqual(manifest["entities"]["paystubs_overview"]["count"], 1)
        self.assertEqual(self.load("paystubs_overview.json"), PAYROLL_OVERVIEW)
        self.assertEqual(manifest["entities"]["company"]["count"], len(export.PAYROLL_COMPANY))
        self.assertEqual(self.load("company.json")[0], {"path": export.PAYROLL_COMPANY[0], "body": {"status": "invented"}})
        self.assertFalse(manifest["entities"]["company"]["required"])

    def test_a_refused_company_read_is_recorded_and_the_others_still_export(self):
        path = export.PAYROLL_COMPANY[1]
        manifest = export.export_payroll(payroll_client(missing={path}), self.out, [2026], today=PAYROLL_TODAY)
        rows = self.load("company.json")
        self.assertEqual(rows[1], {"path": path, "error": "HTTP 403"})
        self.assertEqual(rows[0]["body"], {"status": "invented"})
        self.assertEqual(manifest["entities"]["company"]["status"], "ok")
        self.assertEqual(manifest["entities"]["paystubs"]["count"], 3)
        self.assertEqual(export.failed_required(manifest), [])

    def test_a_refused_overview_fails_only_its_entity(self):
        manifest = export.export_payroll(payroll_client(missing={export.PAYROLL_PAYSTUBS_OVERVIEW}), self.out, [2026],
                                         today=PAYROLL_TODAY)
        self.assertEqual(manifest["entities"]["paystubs_overview"]["status"], "error")
        self.assertEqual(manifest["entities"]["paystubs"]["status"], "ok")
        self.assertFalse(os.path.exists(os.path.join(self.out, "paystubs_overview.json")))

    def test_no_payslip_is_asked_for_after_today(self):
        # Months after February 2026 are not asked for at all: a 404 there would hide a bug.
        seen = []
        c = payroll_client()
        real = c.get

        def get(path, params=None, raw=False):
            seen.append(path)
            return real(path, params, raw)

        c.get = get
        export.export_payroll(c, self.out, [2026], today=PAYROLL_TODAY)
        self.assertFalse([p for p in seen if p.endswith(("/2026/3", "/2026/12"))])

    def test_a_forbidden_employee_list_stops_the_run(self):
        manifest = export.export_payroll(payroll_client(missing={export.PAYROLL_EMPLOYEES}), self.out, [2026],
                                         today=PAYROLL_TODAY)
        entry = manifest["entities"]["employees"]
        self.assertEqual(entry["status"], "error")
        self.assertIn("403", entry["error"])
        self.assertEqual(set(manifest["entities"]), {"employees"})
        self.assertEqual(export.failed_required(manifest), ["employees"])

    def test_a_forbidden_payslip_fails_its_entity_and_is_not_taken_for_no_payslip(self):
        path = export.PAYROLL_PAYSTUB.format(id=101, year=2026, month=2)
        manifest = export.export_payroll(payroll_client(missing={path}), self.out, [2026], today=PAYROLL_TODAY)
        self.assertEqual(manifest["entities"]["paystubs"]["status"], "error")
        self.assertIn("403", manifest["entities"]["paystubs"]["error"])
        self.assertNotIn("by_month", manifest["entities"]["paystubs"])
        self.assertEqual(export.failed_required(manifest), [])

    def test_a_failed_entity_removes_its_file_from_an_earlier_run(self):
        export.export_payroll(payroll_client(), self.out, [2026], today=PAYROLL_TODAY)
        export.export_payroll(payroll_client(missing={export.PAYROLL_PAYSTUBS_OVERVIEW}), self.out, [2026],
                              today=PAYROLL_TODAY)
        self.assertFalse(os.path.exists(os.path.join(self.out, "paystubs_overview.json")))

    def test_a_rerun_gives_the_same_manifest_and_files(self):
        first = export.export_payroll(payroll_client(), self.out, [2026], today=PAYROLL_TODAY)
        second = export.export_payroll(payroll_client(), self.out, [2026], today=PAYROLL_TODAY)
        self.assertEqual(first, second)
        self.assertEqual(len(os.listdir(os.path.join(self.out, "paystubs"))), 3)

    def test_payroll_folder_is_700_and_files_are_600(self):
        os.umask(0o022)
        export.export_payroll(payroll_client(), self.out, [2026], today=PAYROLL_TODAY)
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.out, "paystubs")).st_mode), 0o700)
        for name in ("employees.json", "absences.json", "paystubs.json", "manifest.json"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.out, name)).st_mode), 0o600, name)
        for name in os.listdir(os.path.join(self.out, "paystubs")):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.out, "paystubs", name)).st_mode), 0o600, name)


if __name__ == "__main__":
    unittest.main()
