"""Offline tests for import_files.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import json
import os
import tempfile
import unittest

import import_files as imf
import import_master as im
from test_import_master import FakeErp


def write_export(root, bills=(), expenses=(), files=None, bill_attachments=None, contents=None):
    """An export directory with invented records; files is the files.json list, contents {file id: (name, bytes)}."""
    with open(os.path.join(root, "bills.json"), "w", encoding="utf-8") as f:
        json.dump(list(bills), f)
    with open(os.path.join(root, "expenses.json"), "w", encoding="utf-8") as f:
        json.dump(list(expenses), f)
    if files is not None:
        with open(os.path.join(root, imf.FILES_FILE), "w", encoding="utf-8") as f:
            json.dump(files, f)
    if bill_attachments is not None:
        with open(os.path.join(root, imf.BILL_ATTACHMENTS_FILE), "w", encoding="utf-8") as f:
            json.dump(bill_attachments, f)
    os.makedirs(os.path.join(root, imf.CONTENT_DIR), exist_ok=True)
    for file_id, (name, data) in (contents or {}).items():
        ext = os.path.splitext(name)[1]
        with open(os.path.join(root, imf.CONTENT_DIR, file_id + ext), "wb") as f:
            f.write(data)


def meta(file_id, name, data):
    """A files.json row for the file with this uuid (the test id doubles as the uuid); the id is the content's name."""
    return {"id": file_id, "uuid": file_id, "name": name,
            "extension": os.path.splitext(name)[1].lstrip("."), "size_in_bytes": len(data)}


def erp_with(purchase_invoices=(), files=()):
    """FakeErp with Purchase Invoices keyed by bexio_id and Files on them."""
    return FakeErp({"Purchase Invoice": list(purchase_invoices), "File": list(files)})


def invoice(name, bexio_id):
    return {"name": name, "bexio_id": bexio_id}


class LookupsTest(unittest.TestCase):
    def test_files_on_a_purchase_invoice_are_read_with_their_description(self):
        # the list query refuses description, so the marker must come from a per-File read
        erp = erp_with([invoice("PINV-1", "10"), invoice("PINV-2", "11")], [
            {"name": "FILE-1", "attached_to_doctype": "Purchase Invoice", "attached_to_name": "PINV-1",
             "file_name": "a.pdf", "file_size": 3, "description": "bexio file f-1"},
        ])
        lookups = imf.Lookups.from_erp(erp)
        self.assertEqual(lookups.documents, {"10": "PINV-1", "11": "PINV-2"})
        self.assertEqual(lookups.attached, {"PINV-1": [
            {"name": "FILE-1", "attached_to_name": "PINV-1", "file_name": "a.pdf", "file_size": 3, "description": "bexio file f-1"}]})


class LoadFilesTest(unittest.TestCase):
    def test_files_and_bill_attachments_are_keyed_by_uuid(self):
        with tempfile.TemporaryDirectory() as root:
            write_export(root, files=[dict(meta("f-1", "a.pdf", b"x"), id=9, uuid="u-1")],
                         bill_attachments=[dict(meta("u-2", "u-2.pdf", b"yy"), bill_id=10)])
            files = imf.load_files(root)
        self.assertEqual(sorted(files), ["u-1", "u-2"])
        self.assertEqual(files["u-2"]["bill_id"], 10)

    def test_no_export_file_means_no_metadata_at_all(self):
        with tempfile.TemporaryDirectory() as root:
            write_export(root)
            self.assertIsNone(imf.load_files(root))

    def test_content_is_named_by_the_integer_id_of_files_json(self):
        # export.py names a file's content <id>.<extension>, so the import must look under the integer id
        with tempfile.TemporaryDirectory() as root:
            data = b"%PDF"
            write_export(root, contents={"9": ("Beleg.pdf", data)})
            row = {"id": 9, "uuid": "u-9", "name": "Beleg.pdf", "extension": "pdf", "size_in_bytes": len(data)}
            self.assertEqual(imf.classify("u-9", row, ["10"], imf.Lookups({"10": "PINV-1"}, {}), root),
                             (imf.ATTACH, "PINV-1"))


class AttachmentOwnersTest(unittest.TestCase):
    def test_bills_and_expenses_list_their_files_by_bexio_id(self):
        owners = imf.attachment_owners(
            [{"id": 10, "attachment_ids": ["f-1", "f-2"]}, {"id": 11, "attachment_ids": []}, {"id": 12}],
            [{"id": 20, "attachment_ids": ["f-3"]}],
        )
        self.assertEqual(owners, {"f-1": ["10"], "f-2": ["10"], "f-3": ["20"]})

    def test_a_file_listed_twice_has_two_owners(self):
        owners = imf.attachment_owners([{"id": 10, "attachment_ids": ["f-1"]}, {"id": 11, "attachment_ids": ["f-1"]}], [])
        self.assertEqual(owners, {"f-1": ["10", "11"]})


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def outcome(self, file_id, holders, files_meta, lookups):
        return imf.classify(file_id, files_meta, holders, lookups, self.root)[0]

    def test_file_with_document_and_matching_content_is_attached(self):
        data = b"%PDF-invented"
        write_export(self.root, contents={"f-1": ("Beleg.pdf", data)})
        lookups = imf.Lookups({"10": "PINV-1"}, {})
        self.assertEqual(imf.classify("f-1", meta("f-1", "Beleg.pdf", data), ["10"], lookups, self.root),
                         (imf.ATTACH, "PINV-1"))

    def test_marker_in_description_means_attached(self):
        data = b"x"
        write_export(self.root, contents={"f-1": ("a.pdf", data)})
        lookups = imf.Lookups({"10": "PINV-1"}, {"PINV-1": [{"file_name": "renamed.pdf", "file_size": 99, "description": "bexio file f-1"}]})
        self.assertEqual(self.outcome("f-1", ["10"], meta("f-1", "a.pdf", data), lookups), imf.ATTACHED)

    def test_same_name_and_size_means_attached(self):
        data = b"xyz"
        write_export(self.root, contents={"f-1": ("a.pdf", data)})
        lookups = imf.Lookups({"10": "PINV-1"}, {"PINV-1": [{"file_name": "a.pdf", "file_size": 3, "description": ""}]})
        self.assertEqual(self.outcome("f-1", ["10"], meta("f-1", "a.pdf", data), lookups), imf.ATTACHED)

    def test_marker_matches_whole_so_file_1_does_not_match_file_12(self):
        data = b"x"
        write_export(self.root, contents={"f-1": ("a.pdf", data)})
        lookups = imf.Lookups({"10": "PINV-1"}, {"PINV-1": [{"file_name": "b.pdf", "file_size": 7, "description": "bexio file f-12"}]})
        self.assertEqual(self.outcome("f-1", ["10"], meta("f-1", "a.pdf", data), lookups), imf.ATTACH)

    def test_no_owner_is_unlinked(self):
        self.assertEqual(self.outcome("f-9", [], meta("f-9", "a.pdf", b"x"), imf.Lookups({}, {})), "unlinked")

    def test_two_owners_is_shared(self):
        self.assertEqual(self.outcome("f-1", ["10", "11"], meta("f-1", "a.pdf", b"x"), imf.Lookups({}, {})), "shared")

    def test_reference_without_metadata(self):
        self.assertEqual(self.outcome("f-1", ["10"], None, imf.Lookups({"10": "PINV-1"}, {})), "no metadata")

    def test_document_is_counted_even_without_metadata(self):
        # the dry run must say how many documents are in ERPNext, with or without files.json
        self.assertEqual(imf.classify("f-1", None, ["10"], imf.Lookups({"10": "PINV-1"}, {}), self.root),
                         ("no metadata", "PINV-1"))
        self.assertEqual(self.outcome("f-1", ["10"], None, imf.Lookups({}, {})), "no document")

    def test_document_not_in_erpnext(self):
        self.assertEqual(self.outcome("f-1", ["10"], meta("f-1", "a.pdf", b"x"), imf.Lookups({}, {})), "no document")

    def test_a_row_without_name_or_size_is_no_content_even_next_to_other_files(self):
        # a bill attachment whose download failed (export.py) has neither; the other files of the document must not matter
        lookups = imf.Lookups({"10": "PINV-1"}, {"PINV-1": [{"file_name": None, "file_size": None, "description": ""}]})
        row = {"id": "u-1", "uuid": "u-1", "bill_id": "10"}
        self.assertEqual(self.outcome("u-1", ["10"], row, lookups), "no content")

    def test_content_missing_on_disk(self):
        lookups = imf.Lookups({"10": "PINV-1"}, {})
        self.assertEqual(self.outcome("f-1", ["10"], meta("f-1", "a.pdf", b"x"), lookups), "no content")

    def test_content_of_another_size_than_the_metadata(self):
        write_export(self.root, contents={"f-1": ("a.pdf", b"four")})
        lookups = imf.Lookups({"10": "PINV-1"}, {})
        self.assertEqual(self.outcome("f-1", ["10"], meta("f-1", "a.pdf", b"x"), lookups), "size differs")


class PlanTest(unittest.TestCase):
    def test_one_row_per_referenced_or_described_file(self):
        with tempfile.TemporaryDirectory() as root:
            write_export(root, contents={"f-1": ("a.pdf", b"aa"), "f-2": ("b.pdf", b"bbb")})
            files = {"f-1": meta("f-1", "a.pdf", b"aa"), "f-2": meta("f-2", "b.pdf", b"bbb"), "f-3": meta("f-3", "c.pdf", b"c")}
            owners = {"f-1": ["10"], "f-4": ["11"]}
            lookups = imf.Lookups({"10": "PINV-1", "11": "PINV-2"}, {})
            rows = {r["file"]: r for r in imf.plan(files, owners, lookups, root)}
        self.assertEqual(sorted(rows), ["f-1", "f-2", "f-3", "f-4"])
        self.assertEqual((rows["f-1"]["outcome"], rows["f-1"]["doctype"], rows["f-1"]["size"]), ("attach", "Purchase Invoice", 2))
        self.assertEqual(rows["f-2"]["outcome"], "unlinked")
        self.assertEqual(rows["f-2"]["doctype"], "-")
        self.assertEqual(rows["f-3"]["outcome"], "unlinked")
        self.assertEqual(rows["f-4"]["outcome"], "no metadata")


class ReportTest(unittest.TestCase):
    def rows(self):
        return [
            {"file": "f-1", "outcome": "attach", "doctype": "Purchase Invoice", "document": "PINV-1", "size": 100},
            {"file": "f-2", "outcome": "attached", "doctype": "Purchase Invoice", "document": "PINV-2", "size": 50},
            {"file": "f-3", "outcome": "unlinked", "doctype": "-", "document": None, "size": 7},
        ]

    def test_totals_per_doctype_with_bytes(self):
        text = imf.report(self.rows(), "/private/export", True)
        self.assertIn("Purchase Invoice", text)
        self.assertIn("100", text)  # bytes to attach
        self.assertIn("unlinked", text)
        self.assertIn("nothing was written", text)

    def test_without_files_json_bytes_are_not_shown(self):
        rows = [dict(r, size=None) for r in self.rows()]
        text = imf.report(rows, "/private/export", False)
        self.assertIn("not in the export", text)
        self.assertNotIn("100", text)

    def test_no_file_ids_or_names_in_the_report(self):
        text = imf.report(self.rows(), "/private/export", True)
        for file_id in ("f-1", "f-2", "f-3", "PINV-1"):
            self.assertNotIn(file_id, text)


class RunTest(unittest.TestCase):
    def test_run_without_files_json_lists_every_reference_as_no_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            write_export(root, bills=[{"id": 10, "attachment_ids": ["f-1"], "bill_date": "2026-01-01",
                                       "currency_code": "CHF", "gross": 1, "net": 1, "positions": []}])
            erp = erp_with([invoice("PINV-1", "10")])
            rows, has_files = imf.run(root, erp)
        self.assertFalse(has_files)
        self.assertEqual([(r["file"], r["outcome"]) for r in rows], [("f-1", "no metadata")])
        self.assertEqual(erp.writes, 0)

    def test_run_attaches_nothing_and_reads_only(self):
        with tempfile.TemporaryDirectory() as root:
            write_export(root, bills=[{"id": 10, "attachment_ids": ["f-1"], "bill_date": "2026-01-01",
                                       "currency_code": "CHF", "gross": 1, "net": 1, "positions": []}],
                         files=[meta("f-1", "a.pdf", b"xx")], contents={"f-1": ("a.pdf", b"xx")})
            erp = erp_with([invoice("PINV-1", "10")])
            rows, has_files = imf.run(root, erp)
        self.assertTrue(has_files)
        self.assertEqual([(r["file"], r["outcome"], r["size"]) for r in rows], [("f-1", "attach", 2)])
        self.assertEqual(erp.writes, 0)


class UploadRequestTest(unittest.TestCase):
    def test_multipart_request_carries_the_document_privacy_and_content(self):
        erp = im.Erp("https://erp.test/", "key", "secret")
        req = imf.upload_request(erp, "PINV-1", 'Beleg "1".pdf', b"%PDF-invented")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, "https://erp.test/api/method/upload_file")
        self.assertEqual(req.get_header("Authorization"), "token key:secret")
        content_type = req.get_header("Content-type")
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        boundary = content_type.split("boundary=", 1)[1].encode("ascii")
        body = req.data
        self.assertTrue(body.endswith(b"--" + boundary + b"--\r\n"))
        self.assertIn(b'name="doctype"\r\n\r\nPurchase Invoice\r\n', body)
        self.assertIn(b'name="docname"\r\n\r\nPINV-1\r\n', body)
        self.assertIn(b'name="is_private"\r\n\r\n1\r\n', body)
        self.assertIn(b'filename="Beleg _1_.pdf"', body)
        self.assertIn(b"%PDF-invented", body)

    def test_line_breaks_in_the_file_name_cannot_end_the_header(self):
        erp = im.Erp("https://erp.test/", "key", "secret")
        req = imf.upload_request(erp, "PINV-1", "a\r\nX-Evil: 1.pdf", b"x")
        self.assertIn(b'filename="a__X-Evil: 1.pdf"', req.data)


if __name__ == "__main__":
    unittest.main()
