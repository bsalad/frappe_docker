"""Offline tests for export.py: files, manifest, permissions, gaps, documents, downloads. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

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
    "/4.0/purchase/bills/b1": {"data": {"id": "b1", "lines": [{"text": "Papier"}]}},
}

# Raw file contents. File 10 answers only on its second candidate path; file 11 on none.
RAW = {
    "/3.0/files/9/download": b"%PDF-invented",
    "/3.0/files/10/content": b"\x00\x01",
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
        self.assertEqual(self.load("bills.json"), [{"id": "b1", "gross": "10", "lines": [{"text": "Papier"}]}])

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
        self.assertIn("/2.0/contact", asked_read)
        self.assertNotIn("/2.0/contact", asked_write)

    def test_the_export_scope_set_is_exactly_the_four_entities(self):
        self.assertEqual(export.EXPORT_SCOPE_ENTITIES, {"manual_entries", "journal", "bank_transactions", "files"})

    def test_raw_get_returns_the_bytes_and_plain_get_decodes_json(self):
        c = Client(token="test-token-not-real")
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"\x89PNG"
        with mock.patch("urllib.request.urlopen", return_value=resp):
            self.assertEqual(c.get("/3.0/files/1/download", raw=True), b"\x89PNG")
        resp.__enter__.return_value.read.return_value = b'{"id": 1}'
        with mock.patch("urllib.request.urlopen", return_value=resp):
            self.assertEqual(c.get("/2.0/contact/1"), {"id": 1})


if __name__ == "__main__":
    unittest.main()
