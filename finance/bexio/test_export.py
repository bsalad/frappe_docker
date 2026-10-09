"""Offline tests for export.py: files, manifest, permissions, gaps. Invented data only, no network.

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
}


def fake_client(missing=()):
    """A Client whose GETs answer from FAKE: [] for unknown paths, 403 for `missing`."""
    c = Client(token="test-token-not-real")

    def fake_get(path, params=None):
        if path in missing:
            raise BexioError(403, path)
        if params and params.get("offset"):
            return []
        body = FAKE.get(path, [])
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
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.out, name)).st_mode), 0o600, name)

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

    def test_bills_are_read_page_numbered(self):
        c = fake_client()
        seen = []
        real = c.get
        c.get = lambda path, params=None: (seen.append((path, dict(params or {}))), real(path, params))[1]
        FAKE["/4.0/purchase/bills"] = [{"id": "b1"}]
        self.addCleanup(FAKE.pop, "/4.0/purchase/bills")
        export.export(c, self.out)
        bills = [p for path, p in seen if path == "/4.0/purchase/bills"]
        self.assertIn("page", bills[0])
        self.assertEqual(self.load("bills.json"), [{"id": "b1"}])

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
            resp.__enter__.return_value.read.return_value = b'{"data": []}' if "/4.0/" in req.full_url else b"[]"
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=answer) as opened:
            export.export(c, self.out)
        methods = {call.args[0].get_method() for call in opened.call_args_list}
        self.assertEqual(methods, {"GET"})


if __name__ == "__main__":
    unittest.main()
