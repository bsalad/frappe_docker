"""Offline tests for export.py: files, manifest, permissions, gaps, documents, downloads. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import base64
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

    def get(path, params=None, raw=False, accept=None):
        paths.append(path)
        return real(path, params, raw, accept)

    client.get = get
    return paths


def answering(client, answers):
    """Wrap the client's get so that the paths in `answers` answer first (an exception instance is raised instead)."""
    real = client.get

    def get(path, params=None, raw=False, accept=None):
        if path not in answers:
            return real(path, params, raw, accept)
        if isinstance(answers[path], BexioError):
            raise answers[path]
        return answers[path]

    client.get = get
    return client


def fake_client(missing=()):
    """A Client whose GETs answer from the fixtures: [] for unknown lists, 404 for unknown files, 403 for `missing`."""
    c = Client(token="test-token-not-real")

    def fake_get(path, params=None, raw=False, accept=None):
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
            if name not in ("files", "documents"):
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.out, name)).st_mode), 0o600, name)
        for folder in ("files", "documents"):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.out, folder)).st_mode), 0o700, folder)
            for name in os.listdir(os.path.join(self.out, folder)):
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.out, folder, name)).st_mode), 0o600, name)

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
        c.get = lambda path, params=None, raw=False, accept=None: (seen.append((path, dict(params or {}))), real(path, params, raw, accept))[1]
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

    def test_the_export_scope_set_is_the_entities_with_file_contents_or_files(self):
        self.assertEqual(export.EXPORT_SCOPE_ENTITIES,
                         {"manual_entries", "journal", "bank_transactions", "files", "file_links",
                          "bill_attachments", "expense_attachments"})

    def test_credit_vouchers_are_read_by_the_ids_in_the_payment_rows(self):
        c = answering(fake_client(), {
            "/2.0/kb_invoice/5/payment": [{"kb_credit_voucher_id": 7, "value": "-3"}, {"value": "10"}],
            "/2.0/kb_credit_voucher/7": {"title": "Invented credit"},
        })
        manifest = export.export(c, self.out, only=["credit_vouchers"])
        self.assertEqual(self.load("credit_vouchers.json"), [{"id": 7, "title": "Invented credit"}])
        self.assertEqual(manifest["entities"]["credit_vouchers"]["count"], 1)
        self.assertNotIn("failed", manifest["entities"]["credit_vouchers"])

    def test_a_refused_credit_voucher_is_named_in_the_manifest(self):
        c = answering(fake_client(), {
            "/2.0/kb_invoice/5/payment": [{"kb_credit_voucher_id": 8, "value": "-3"}],
            "/2.0/kb_credit_voucher/8": BexioError(404, "/2.0/kb_credit_voucher/8"),
        })
        manifest = export.export(c, self.out, only=["credit_vouchers"])
        self.assertEqual(self.load("credit_vouchers.json"), [{"id": 8, "error": "HTTP 404 on /2.0/kb_credit_voucher/8"}])
        self.assertEqual(manifest["entities"]["credit_vouchers"]["failed"], [8])
        self.assertEqual(export.failed_required(manifest), [])

    def test_document_pdfs_are_written_per_kind_and_id(self):
        c = answering(fake_client(), {"/2.0/kb_invoice/5/pdf": b"%PDF-invoice"})
        manifest = export.export(c, self.out, only=["invoices", "document_pdfs"])
        with open(os.path.join(self.out, "documents", "invoice-5.pdf"), "rb") as f:
            self.assertEqual(f.read(), b"%PDF-invoice")
        entry = manifest["entities"]["document_pdfs"]
        self.assertEqual((entry["count"], entry["downloaded"], entry["bytes"]), (1, 1, len(b"%PDF-invoice")))
        self.assertEqual(entry["failed"], [])
        self.assertEqual(entry["skipped"], ["offers", "orders", "deliveries", "credit_vouchers"])
        self.assertEqual(self.load("document_pdfs.json"), [{"kind": "invoice", "id": 5, "file": "invoice-5.pdf",
                                                            "bytes": len(b"%PDF-invoice")}])

    def test_a_refused_document_pdf_is_listed_as_kind_and_id(self):
        c = answering(fake_client(), {"/2.0/kb_invoice/5/pdf": BexioError(404, "/2.0/kb_invoice/5/pdf")})
        manifest = export.export(c, self.out, only=["invoices", "document_pdfs"])
        self.assertEqual(manifest["entities"]["document_pdfs"]["failed"], ["invoice:5"])
        self.assertFalse(os.path.exists(os.path.join(self.out, "documents", "invoice-5.pdf")))

    def test_refused_document_pdfs_count_the_status_that_refused_them(self):
        c = answering(fake_client(), {
            "/2.0/kb_invoice/5/pdf": BexioError(404, "/2.0/kb_invoice/5/pdf"),
            "/2.0/kb_invoice/6/pdf": BexioError(403, "/2.0/kb_invoice/6/pdf"),
            "/2.0/kb_invoice/7/pdf": BexioError(404, "/2.0/kb_invoice/7/pdf"),
        })
        os.makedirs(self.out)
        with open(os.path.join(self.out, "invoices.json"), "w", encoding="utf-8") as f:
            json.dump([{"id": 5}, {"id": 6}, {"id": 7}], f)
        _, summary = export.download_document_pdfs(c, self.out)
        self.assertEqual(summary["failed"], ["invoice:5", "invoice:6", "invoice:7"])
        self.assertEqual(summary["reasons"], {"HTTP 403": 1, "HTTP 404": 2})

    def test_a_document_pdf_refused_raw_is_read_in_the_json_form_as_base64(self):
        # invented content: the PDF header and a few bytes, base64 as bexio's JSON form is read to hold it
        raw_refused = BexioError(415, "/2.0/kb_invoice/5/pdf")
        json_form = {"name": "invoice-5.pdf", "content": base64.b64encode(b"%PDF-json-form").decode("ascii")}

        def get(path, params=None, raw=False, accept=None):
            if path != "/2.0/kb_invoice/5/pdf":
                raise AssertionError(path)
            if raw and accept is None:
                raise raw_refused
            return json.dumps(json_form).encode("utf-8")

        c = fake_client()
        c.get = get
        os.makedirs(self.out)
        with open(os.path.join(self.out, "invoices.json"), "w", encoding="utf-8") as f:
            json.dump([{"id": 5}], f)
        _, summary = export.download_document_pdfs(c, self.out)
        self.assertEqual(summary["downloaded"], 1)
        self.assertEqual(summary["failed"], [])
        with open(os.path.join(self.out, "documents", "invoice-5.pdf"), "rb") as f:
            self.assertEqual(f.read(), b"%PDF-json-form")

    def test_a_document_pdf_refused_in_both_forms_counts_415_and_is_not_written(self):
        # the JSON form holds no PDF (the content is not base64 or not a PDF): refused as 415, nothing written
        def get(path, params=None, raw=False, accept=None):
            if raw and accept is None:
                raise BexioError(415, path)
            return json.dumps({"content": base64.b64encode(b"not a pdf").decode("ascii")}).encode("utf-8")

        c = fake_client()
        c.get = get
        os.makedirs(self.out)
        with open(os.path.join(self.out, "invoices.json"), "w", encoding="utf-8") as f:
            json.dump([{"id": 5}], f)
        _, summary = export.download_document_pdfs(c, self.out)
        self.assertEqual(summary["failed"], ["invoice:5"])
        self.assertEqual(summary["reasons"], {"HTTP 415": 1})
        self.assertFalse(os.path.exists(os.path.join(self.out, "documents", "invoice-5.pdf")))

    def test_a_json_answer_without_a_pdf_says_its_shape_not_its_values(self):
        # invented: a name that must not show in the detail, and content that is not a PDF
        def get(path, params=None, raw=False, accept=None):
            if raw and accept is None:
                raise BexioError(415, path)
            return json.dumps({"name": "Invented Person",
                               "content": base64.b64encode(b"not a pdf").decode("ascii")}).encode("utf-8")

        c = fake_client()
        c.get = get
        with self.assertRaises(BexioError) as ctx:
            export.document_pdf(c, "/p")
        self.assertEqual(ctx.exception.status, 415)
        self.assertIn("'name': 'str'", ctx.exception.detail)
        self.assertNotIn("Invented Person", ctx.exception.detail)

    def test_a_credit_voucher_pdf_is_asked_for_even_when_its_detail_is_refused(self):
        c = answering(fake_client(), {
            "/2.0/kb_invoice/5/payment": [{"kb_credit_voucher_id": 8, "value": "-3"}],
            "/2.0/kb_credit_voucher/8": BexioError(404, "/2.0/kb_credit_voucher/8"),
            "/2.0/kb_credit_voucher/8/pdf": b"%PDF-voucher",
        })
        manifest = export.export(c, self.out, only=["credit_vouchers", "document_pdfs"])
        self.assertEqual(manifest["entities"]["document_pdfs"]["downloaded"], 1)
        self.assertTrue(os.path.exists(os.path.join(self.out, "documents", "credit_voucher-8.pdf")))

    def test_notes_deliveries_and_taxes_are_exported(self):
        c = answering(fake_client(), {
            "/2.0/note": [{"id": 1, "text": "Invented note"}],
            "/2.0/kb_delivery": [{"id": 2}],
            "/2.0/kb_delivery/2": {"id": 2, "positions": [{"id": 20, "type": "KbPositionCustom", "text": "Invented"}]},
            "/3.0/taxes": [{"id": 3, "display_name": "Invented VAT"}],
        })
        manifest = export.export(c, self.out, only=["notes", "deliveries", "taxes"])
        self.assertEqual(self.load("notes.json"), [{"id": 1, "text": "Invented note"}])
        self.assertEqual(manifest["entities"]["notes"]["count"], 1)
        self.assertEqual(manifest["entities"]["deliveries"]["count"], 1)
        self.assertEqual(manifest["entities"]["taxes"]["count"], 1)

    def test_a_delivery_is_read_with_its_positions(self):
        c = answering(fake_client(), {
            "/2.0/kb_delivery": [{"id": 2, "document_nr": "LI-2"}],
            "/2.0/kb_delivery/2": {"id": 2, "document_nr": "LI-2", "positions": [{"id": 20, "type": "KbPositionCustom"}]},
        })
        export.export(c, self.out, only=["deliveries"])
        self.assertEqual(self.load("deliveries.json"),
                         [{"id": 2, "document_nr": "LI-2", "positions": [{"id": 20, "type": "KbPositionCustom"}]}])

    def test_expenses_carry_their_details_and_their_attachments(self):
        c = answering(fake_client(), {
            "/4.0/expenses": {"data": [{"id": "e1", "attachment_ids": ["u-e1"]}]},
            "/4.0/expenses/e1": {"data": {"id": "e1", "attachment_ids": ["u-e1"], "title": "Invented expense"}},
            "/3.0/files/u-e1/download": b"%PDF-expense",
        })
        manifest = export.export(c, self.out, only=["expenses", "expense_attachments"])
        self.assertEqual(self.load("expenses.json"), [{"id": "e1", "attachment_ids": ["u-e1"], "title": "Invented expense"}])
        self.assertEqual(manifest["entities"]["expense_attachments"]["downloaded"], 1)
        with open(os.path.join(self.out, "files", "u-e1.pdf"), "rb") as f:
            self.assertEqual(f.read(), b"%PDF-expense")

    def test_file_links_are_read_per_file_and_a_refusal_is_listed(self):
        c = answering(fake_client(), {
            "/3.0/files/9/usage": [{"entity": "kb_invoice"}],
            "/3.0/files/10/usage": BexioError(404, "/3.0/files/10/usage"),
            "/3.0/files/11/usage": BexioError(404, "/3.0/files/11/usage"),
        })
        manifest = export.export(c, self.out, only=["file_links"])
        rows = self.load("file_links.json")
        self.assertEqual(rows[0], {"file_id": 9, "usage": [{"entity": "kb_invoice"}]})
        self.assertEqual(rows[1], {"file_id": 10, "error": "HTTP 404 on /3.0/files/10/usage"})
        self.assertEqual(manifest["entities"]["file_links"]["failed"], [10, 11])

    def test_complete_is_dated_under_the_export_folder(self):
        self.assertEqual(export.default_complete_out(datetime.date(2026, 10, 10)),
                         "/Users/bsaladin/ws_yardr_finance/private/bexio-export/2026-10-10-complete")

    def test_complete_writes_the_entities_and_the_payroll_into_one_folder(self):
        # Both logins answer from the fixtures (the payroll reads come back empty), so only the layout is checked.
        with mock.patch.object(export, "Client", side_effect=[fake_client(), fake_client()]):
            self.assertEqual(export.run_complete(self.out, [2026]), 0)
        self.assertTrue(os.path.exists(os.path.join(self.out, "contacts.json")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "manifest.json")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "documents")))
        self.assertTrue(os.path.exists(os.path.join(self.out, "payroll", "manifest.json")))

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
# What bexio answers a month without a payslip: a 400 whose body names no_paystubs (invented ids).
NO_PAYSLIP_BODY = '{"error": {"message": "no_paystubs", "id": 0}}'
# What bexio answers a month of a year before the payroll company existed: a 400 naming company_not_found (invented).
COMPANY_NOT_FOUND_BODY = '{"error": {"message": "company_not_found", "id": 0}}'


def payroll_client(missing=()):
    """A Client whose payroll GETs answer from the invented fixtures; 404 for a payslip not in PAYROLL_PAYSLIPS.

    A payslip is answered as bexio answers the download: 415 to the raw request (Accept */*), the PDF itself
    to Accept: application/json.
    """
    c = Client(token="test-token-not-real")

    def fake_get(path, params=None, raw=False, accept=None):
        if path in missing:
            raise BexioError(403, path)
        if path == export.PAYROLL_EMPLOYEES:
            return {"data": PAYROLL_EMPLOYEES}
        if path in export.PAYROLL_COMPANY:
            return {"status": "invented"}
        if path.endswith("/absences"):
            if "businessYear" not in (params or {}):
                raise BexioError(400, path, '{"title": "No business year provided"}')
            return {"data": [{"reason": "Vacation", "days": 5, "year": params["businessYear"]}]}
        _, _, _, _, employee, _, year, month = path.split("/")
        key = (int(employee), int(year), int(month))
        if key not in PAYROLL_PAYSLIPS:
            raise BexioError(400, path, NO_PAYSLIP_BODY)
        if raw and accept is None:
            raise BexioError(415, path)
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

    def test_absences_are_asked_with_the_business_year_parameter(self):
        seen = []
        c = payroll_client()
        real = c.get

        def get(path, params=None, raw=False, accept=None):
            if path.endswith("/absences"):
                seen.append(params)
            return real(path, params, raw, accept)

        c.get = get
        manifest = export.export_payroll(c, self.out, [2026], today=PAYROLL_TODAY)
        self.assertEqual(seen, [{"businessYear": 2026}, {"businessYear": 2026}])
        self.assertEqual(manifest["entities"]["absences"]["status"], "ok")

    def test_an_employee_without_absences_is_an_empty_list_not_an_error(self):
        c = payroll_client()
        real = c.get

        def get(path, params=None, raw=False, accept=None):
            if path.endswith("/absences") and path.split("/")[4] == "102":
                return {"data": []}
            return real(path, params, raw, accept)

        c.get = get
        manifest = export.export_payroll(c, self.out, [2026], today=PAYROLL_TODAY)
        self.assertEqual(manifest["entities"]["absences"]["status"], "ok")
        self.assertEqual(manifest["entities"]["absences"]["count"], 1)
        self.assertEqual(export.failed_required(manifest), [])

    def test_there_is_no_overview_entity_and_no_overview_path(self):
        # bexio's 4.0 has no paystub overview (the path answered 401): the months are asked one by one
        manifest = export.export_payroll(payroll_client(), self.out, [2026], today=PAYROLL_TODAY)
        self.assertNotIn("paystubs_overview", manifest["entities"])
        self.assertFalse(hasattr(export, "PAYROLL_PAYSTUBS_OVERVIEW"))
        self.assertFalse(os.path.exists(os.path.join(self.out, "paystubs_overview.json")))

    def test_a_payslip_served_raw_is_written_as_it_comes(self):
        c = payroll_client()
        real = c.get

        def get(path, params=None, raw=False, accept=None):
            if raw and path.endswith("/2026/1"):
                return PAYROLL_PAYSLIPS[(101, 2026, 1)]
            return real(path, params, raw, accept)

        c.get = get
        export.export_payroll(c, self.out, [2026], today=PAYROLL_TODAY)
        with open(os.path.join(self.out, "paystubs", "101-2026-01.pdf"), "rb") as f:
            self.assertEqual(f.read(), PAYROLL_PAYSLIPS[(101, 2026, 1)])

    def test_a_payslip_answered_as_json_with_base64_is_written_as_its_pdf(self):
        c = payroll_client()
        real = c.get

        def get(path, params=None, raw=False, accept=None):
            if accept == "application/json" and path.endswith("/2026/1"):
                return json.dumps({"content": base64.b64encode(PAYROLL_PAYSLIPS[(101, 2026, 1)]).decode("ascii")}).encode()
            return real(path, params, raw, accept)

        c.get = get
        export.export_payroll(c, self.out, [2026], today=PAYROLL_TODAY)
        with open(os.path.join(self.out, "paystubs", "101-2026-01.pdf"), "rb") as f:
            self.assertEqual(f.read(), PAYROLL_PAYSLIPS[(101, 2026, 1)])

    def test_company_reads_are_exported_as_their_own_entity(self):
        manifest = export.export_payroll(payroll_client(), self.out, [2026], today=PAYROLL_TODAY)
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

    def test_no_payslip_is_asked_for_after_today(self):
        # Months after February 2026 are not asked for at all: a 404 there would hide a bug.
        seen = []
        c = payroll_client()
        real = c.get

        def get(path, params=None, raw=False, accept=None):
            seen.append(path)
            return real(path, params, raw, accept)

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

    def test_a_bad_request_for_a_payslip_that_is_not_no_paystubs_fails_its_entity(self):
        path = export.PAYROLL_PAYSTUB.format(id=101, year=2026, month=2)
        c = payroll_client()
        real = c.get

        def get(p, params=None, raw=False, accept=None):
            if p == path:
                raise BexioError(400, p, '{"error": {"message": "invented_other"}}')
            return real(p, params, raw, accept)

        c.get = get
        manifest = export.export_payroll(c, self.out, [2026], today=PAYROLL_TODAY)
        self.assertEqual(manifest["entities"]["paystubs"]["status"], "error")
        self.assertIn("400", manifest["entities"]["paystubs"]["error"])

    def test_months_without_a_payslip_are_skipped_not_failed(self):
        manifest = export.export_payroll(payroll_client(), self.out, [2026], today=PAYROLL_TODAY)
        self.assertEqual(manifest["entities"]["paystubs"]["status"], "ok")
        self.assertEqual(manifest["entities"]["paystubs"]["by_month"], {"2026-01": 2, "2026-02": 1})

    def test_a_year_before_payroll_began_is_no_payslips_not_a_failed_entity(self):
        # 2020 is before the payroll company existed: bexio answers every month with company_not_found
        c = payroll_client()
        real = c.get

        def get(path, params=None, raw=False, accept=None):
            if "/paystub-pdf-download/" in path and path.split("/")[6] == "2020":
                raise BexioError(400, path, COMPANY_NOT_FOUND_BODY)
            return real(path, params, raw, accept)

        c.get = get
        manifest = export.export_payroll(c, self.out, [2020, 2026], today=PAYROLL_TODAY)
        self.assertEqual(manifest["entities"]["paystubs"]["status"], "ok")
        self.assertEqual(manifest["entities"]["paystubs"]["count"], 3)
        self.assertEqual(manifest["entities"]["paystubs"]["by_month"], {"2026-01": 2, "2026-02": 1})
        self.assertEqual(export.failed_required(manifest), [])

    def test_company_not_found_skips_only_its_month_so_a_payslip_later_in_that_year_is_kept(self):
        # Payslips begin 2023-11: January to October answer company_not_found, November has a payslip
        c = payroll_client()
        real = c.get
        november = b"%PDF-invented-november"

        def get(path, params=None, raw=False, accept=None):
            if "/paystub-pdf-download/" in path and path.split("/")[6] == "2023":
                if path.split("/")[7] == "11":
                    return november
                raise BexioError(400, path, COMPANY_NOT_FOUND_BODY)
            return real(path, params, raw, accept)

        c.get = get
        manifest = export.export_payroll(c, self.out, [2023], today=PAYROLL_TODAY)
        self.assertEqual(manifest["entities"]["paystubs"]["status"], "ok")
        self.assertEqual(manifest["entities"]["paystubs"]["by_month"], {"2023-11": 2})
        with open(os.path.join(self.out, "paystubs", "101-2023-11.pdf"), "rb") as f:
            self.assertEqual(f.read(), november)

    def test_a_forbidden_payslip_fails_its_entity_and_is_not_taken_for_no_payslip(self):
        path = export.PAYROLL_PAYSTUB.format(id=101, year=2026, month=2)
        manifest = export.export_payroll(payroll_client(missing={path}), self.out, [2026], today=PAYROLL_TODAY)
        self.assertEqual(manifest["entities"]["paystubs"]["status"], "error")
        self.assertIn("403", manifest["entities"]["paystubs"]["error"])
        self.assertNotIn("by_month", manifest["entities"]["paystubs"])
        self.assertEqual(export.failed_required(manifest), [])

    def test_a_failed_entity_removes_its_file_from_an_earlier_run(self):
        export.export_payroll(payroll_client(), self.out, [2026], today=PAYROLL_TODAY)
        export.export_payroll(payroll_client(missing={export.PAYROLL_ABSENCES.format(id=101)}), self.out, [2026],
                              today=PAYROLL_TODAY)
        self.assertFalse(os.path.exists(os.path.join(self.out, "absences.json")))

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
