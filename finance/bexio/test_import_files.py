"""Offline tests for import_files.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

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


def raw(file_id, name, data):
    """A files.json record with the keys bexio writes: the bexio id (the content's name), the name without extension, the extension, size_in_bytes."""
    stem, ext = os.path.splitext(name)
    return {"id": int(file_id.split("-")[1]), "uuid": file_id, "name": stem, "extension": ext[1:],
            "size_in_bytes": len(data), "is_referenced": True, "is_archived": False}


def erp_with(purchase_invoices=(), files=()):
    """FakeErp with Purchase Invoices keyed by bexio_id and Files on them."""
    return FakeErp({"Purchase Invoice": list(purchase_invoices), "File": list(files)})


def invoice(name, bexio_id):
    return {"name": name, "bexio_id": bexio_id}


class LookupsTest(unittest.TestCase):
    def test_files_on_a_purchase_invoice_are_read_with_their_bexio_id(self):
        erp = erp_with([invoice("PINV-1", "10"), invoice("PINV-2", "11")], [
            {"name": "FILE-1", "attached_to_doctype": "Purchase Invoice", "attached_to_name": "PINV-1",
             "file_name": "a.pdf", "file_size": 3, "bexio_id": "f-1"},
        ])
        lookups = imf.Lookups.from_erp(erp)
        self.assertEqual(lookups.documents, {"10": "PINV-1", "11": "PINV-2"})
        self.assertEqual(lookups.attached, {"PINV-1": [
            {"name": "FILE-1", "attached_to_name": "PINV-1", "file_name": "a.pdf", "file_size": 3, "bexio_id": "f-1"}]})

    def test_files_in_the_archive_folder_are_read_by_their_bexio_id(self):
        erp = erp_with([], [
            {"name": "FILE-2", "folder": imf.ARCHIVE_FOLDER, "bexio_id": "f-9"},
            {"name": "FILE-3", "folder": imf.ARCHIVE_FOLDER, "bexio_id": None},
            {"name": "FILE-4", "folder": "Home", "bexio_id": "f-8"},
        ])
        self.assertEqual(imf.Lookups.from_erp(erp).archived, {"f-9"})


class LoadFilesTest(unittest.TestCase):
    def test_files_and_bill_attachments_are_keyed_by_uuid(self):
        with tempfile.TemporaryDirectory() as root:
            write_export(root, files=[dict(meta("f-1", "a.pdf", b"x"), id=9, uuid="u-1")],
                         bill_attachments=[dict(meta("u-2", "u-2.pdf", b"yy"), bill_id=10)])
            files = imf.load_files(root)
        self.assertEqual(sorted(files), ["u-1", "u-2"])
        self.assertEqual(files["u-2"]["bill_id"], 10)

    def test_a_file_in_both_lists_keeps_its_files_json_row(self):
        # the same uuid is in files.json (bexio's name) and bill_attachments.json (named by the uuid): the Files were uploaded under the first
        with tempfile.TemporaryDirectory() as root:
            write_export(root, files=[{"id": 9, "uuid": "u-1", "name": "Beleg", "extension": "pdf", "size_in_bytes": 3}],
                         bill_attachments=[{"id": "u-1", "uuid": "u-1", "name": "u-1.pdf", "extension": "pdf", "size_in_bytes": 3}])
            files = imf.load_files(root)
        self.assertEqual(files["u-1"]["name"], "Beleg.pdf")
        self.assertEqual(files["u-1"]["id"], 9)

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

    def test_files_json_name_gets_its_extension_and_the_content_is_the_bexio_id(self):
        # invented record with bexio's keys: the name carries no extension, the content file is named by the bexio id
        with tempfile.TemporaryDirectory() as root:
            write_export(root, files=[{"id": 1009, "uuid": "u-1", "name": "Beleg", "extension": "pdf",
                                       "size_in_bytes": 3, "is_referenced": True}])
            files = imf.load_files(root)
        self.assertEqual(files["u-1"]["name"], "Beleg.pdf")
        self.assertEqual(files["u-1"]["size_in_bytes"], 3)
        self.assertEqual(os.path.basename(imf.content_path(root, files["u-1"])), "1009.pdf")

    def test_a_name_that_already_has_the_extension_is_not_extended_again(self):
        with tempfile.TemporaryDirectory() as root:
            write_export(root, files=[{"id": 7, "uuid": "u-7", "name": "Beleg.PDF", "extension": "pdf", "size_in_bytes": 1}])
            self.assertEqual(imf.load_files(root)["u-7"]["name"], "Beleg.PDF")


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

    def test_bexio_id_on_the_file_means_attached_whatever_its_name(self):
        data = b"x"
        write_export(self.root, contents={"f-1": ("a.pdf", data)})
        lookups = imf.Lookups({"10": "PINV-1"}, {"PINV-1": [{"file_name": "renamed.pdf", "file_size": 99, "bexio_id": "f-1"}]})
        self.assertEqual(self.outcome("f-1", ["10"], meta("f-1", "a.pdf", data), lookups), imf.ATTACHED)

    def test_same_name_and_size_means_attached_for_a_file_without_bexio_id(self):
        data = b"xyz"
        write_export(self.root, contents={"f-1": ("a.pdf", data)})
        lookups = imf.Lookups({"10": "PINV-1"}, {"PINV-1": [{"file_name": "a.pdf", "file_size": 3, "bexio_id": None}]})
        self.assertEqual(self.outcome("f-1", ["10"], meta("f-1", "a.pdf", data), lookups), imf.ATTACHED)

    def test_bexio_id_matches_whole_so_file_1_does_not_match_file_12(self):
        data = b"x"
        write_export(self.root, contents={"f-1": ("a.pdf", data)})
        lookups = imf.Lookups({"10": "PINV-1"}, {"PINV-1": [{"file_name": "b.pdf", "file_size": 7, "bexio_id": "f-12"}]})
        self.assertEqual(self.outcome("f-1", ["10"], meta("f-1", "a.pdf", data), lookups), imf.ATTACH)

    def test_a_file_keyed_by_another_bexio_id_does_not_match_by_name_and_size(self):
        # two bexio files of one name and size: the File keyed for f-12 is not f-1, so f-1 is still to attach
        data = b"x"
        write_export(self.root, contents={"f-1": ("a.pdf", data)})
        lookups = imf.Lookups({"10": "PINV-1"}, {"PINV-1": [{"file_name": "a.pdf", "file_size": 1, "bexio_id": "f-12"}]})
        self.assertEqual(self.outcome("f-1", ["10"], meta("f-1", "a.pdf", data), lookups), imf.ATTACH)

    def test_no_owner_and_no_metadata_is_unlinked(self):
        self.assertEqual(self.outcome("f-9", [], None, imf.Lookups({}, {})), "unlinked")

    def test_no_owner_with_content_goes_to_the_archive(self):
        # no record lists it: its own File in the archive folder, not a document's attachment
        data = b"%PDF-invented"
        write_export(self.root, contents={"f-9": ("a.pdf", data)})
        self.assertEqual(imf.classify("f-9", meta("f-9", "a.pdf", data), [], imf.Lookups({}, {}), self.root),
                         (imf.ARCHIVE, None))

    def test_no_owner_already_in_the_archive_folder_is_archived(self):
        data = b"x"
        write_export(self.root, contents={"f-9": ("a.pdf", data)})
        lookups = imf.Lookups({}, {}, archived={"f-9"})
        self.assertEqual(imf.classify("f-9", meta("f-9", "a.pdf", data), [], lookups, self.root), (imf.ARCHIVED, None))

    def test_no_owner_without_content_or_of_another_size_is_a_problem_not_archived(self):
        lookups = imf.Lookups({}, {})
        self.assertEqual(self.outcome("f-9", [], meta("f-9", "a.pdf", b"x"), lookups), "no content")
        write_export(self.root, contents={"f-8": ("b.pdf", b"four")})
        self.assertEqual(self.outcome("f-8", [], meta("f-8", "b.pdf", b"x"), lookups), "size differs")

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
        lookups = imf.Lookups({"10": "PINV-1"}, {"PINV-1": [{"file_name": None, "file_size": None, "bexio_id": None}]})
        row = {"id": "u-1", "uuid": "u-1", "bill_id": "10"}
        self.assertEqual(self.outcome("u-1", ["10"], row, lookups), "no content")

    def test_content_missing_on_disk(self):
        lookups = imf.Lookups({"10": "PINV-1"}, {})
        self.assertEqual(self.outcome("f-1", ["10"], meta("f-1", "a.pdf", b"x"), lookups), "no content")

    def test_content_of_another_size_than_the_metadata(self):
        write_export(self.root, contents={"f-1": ("a.pdf", b"four")})
        lookups = imf.Lookups({"10": "PINV-1"}, {})
        self.assertEqual(self.outcome("f-1", ["10"], meta("f-1", "a.pdf", b"x"), lookups), "size differs")

    def test_image_re_encoded_by_frappe_is_attached_not_attached_again(self):
        # Frappe strips the EXIF of an uploaded image: the File keeps the name, not the size
        write_export(self.root, contents={"f-1": ("a.jpg", b"exif-invented-bytes")})
        lookups = imf.Lookups({"10": "PINV-1"}, {"PINV-1": [{"file_name": "a.jpg", "file_size": 10, "bexio_id": None}]})
        self.assertEqual(imf.classify("f-1", meta("f-1", "a.jpg", b"exif-invented-bytes"), ["10"], lookups, self.root),
                         (imf.REENCODED, "PINV-1"))

    def test_a_pdf_of_another_size_under_the_same_name_is_still_a_difference(self):
        # the content on disk is two bytes, the metadata says four: a PDF is not re-encoded, so this is a difference
        write_export(self.root, contents={"f-1": ("a.pdf", b"xx")})
        lookups = imf.Lookups({"10": "PINV-1"}, {"PINV-1": [{"file_name": "a.pdf", "file_size": 10, "bexio_id": None}]})
        self.assertEqual(self.outcome("f-1", ["10"], meta("f-1", "a.pdf", b"four"), lookups), "size differs")


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
        self.assertEqual(rows["f-2"]["outcome"], "archive")
        self.assertEqual(rows["f-2"]["doctype"], "-")
        self.assertEqual(rows["f-3"]["outcome"], "no content")
        self.assertEqual(rows["f-4"]["outcome"], "no metadata")


class ReportTest(unittest.TestCase):
    def rows(self):
        return [
            {"file": "f-1", "outcome": "attach", "doctype": "Purchase Invoice", "document": "PINV-1", "size": 100},
            {"file": "f-2", "outcome": "attached", "doctype": "Purchase Invoice", "document": "PINV-2", "size": 50},
            {"file": "f-3", "outcome": "unlinked", "doctype": "-", "document": None, "size": 7},
        ]

    def test_a_re_encoded_image_counts_as_attached_not_as_a_problem(self):
        rows = self.rows() + [{"file": "f-4", "outcome": imf.REENCODED, "doctype": "Purchase Invoice", "document": "PINV-3", "size": 30}]
        text = imf.report(rows, "/private/export", True)
        pi = next(line for line in text.splitlines() if line.startswith("Purchase Invoice"))
        # files, bytes, to attach, bytes, attached (the re-encoded one too), problems
        self.assertEqual(pi[len("Purchase Invoice"):].split(), ["3", "180", "1", "100", "2", "0"])
        self.assertNotIn(imf.REENCODED, imf.PROBLEMS)

    def test_the_problems_file_says_a_re_encoded_file_is_attached(self):
        self.assertEqual(imf.problem_line({"file": "f-4", "outcome": imf.REENCODED}),
                         "file f-4: attached, re-encoded by Frappe (size differs)")
        self.assertEqual(imf.problem_line({"file": "f-5", "outcome": "unlinked"}), "file f-5: unlinked")

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
            rows, has_files, backfills = imf.run(root, erp)
        self.assertFalse(has_files)
        self.assertEqual([(r["file"], r["outcome"]) for r in rows], [("f-1", "no metadata")])
        self.assertEqual(backfills, ([], []))
        self.assertEqual(erp.writes, 0)

    def test_run_attaches_nothing_and_reads_only(self):
        with tempfile.TemporaryDirectory() as root:
            write_export(root, bills=[{"id": 10, "attachment_ids": ["f-1"], "bill_date": "2026-01-01",
                                       "currency_code": "CHF", "gross": 1, "net": 1, "positions": []}],
                         files=[meta("f-1", "a.pdf", b"xx")], contents={"f-1": ("a.pdf", b"xx")})
            erp = erp_with([invoice("PINV-1", "10")])
            rows, has_files, backfills = imf.run(root, erp)
        self.assertTrue(has_files)
        self.assertEqual([(r["file"], r["outcome"], r["size"]) for r in rows], [("f-1", "attach", 2)])
        self.assertEqual(erp.writes, 0)


class BackfillPlanTest(unittest.TestCase):
    """Invented data: Purchase Invoice PINV-1 (bexio id 10) with planned files f-1 and f-2."""

    def plan_for(self, files, attached):
        rows = [{"file": "f-1", "document": "PINV-1", "outcome": "attached"},
                {"file": "f-2", "document": "PINV-1", "outcome": "attached"}]
        lookups = imf.Lookups({"10": "PINV-1"}, {"PINV-1": attached})
        return imf.backfill_plan(rows, files, lookups)

    def files(self):
        return {"f-1": meta("f-1", "a.pdf", b"xx"), "f-2": meta("f-2", "b.pdf", b"yyy")}

    def test_a_file_without_bexio_id_matching_one_planned_file_is_written(self):
        writes, listed = self.plan_for(self.files(), [{"name": "FILE-1", "file_name": "a.pdf", "file_size": 2, "bexio_id": None}])
        self.assertEqual((writes, listed), ([("FILE-1", "f-1")], []))

    def test_a_file_with_bexio_id_is_left_alone(self):
        writes, listed = self.plan_for(self.files(), [{"name": "FILE-1", "file_name": "a.pdf", "file_size": 2, "bexio_id": "f-1"}])
        self.assertEqual((writes, listed), ([], []))

    def test_a_file_matching_no_planned_file_is_listed_by_name(self):
        writes, listed = self.plan_for(self.files(), [{"name": "FILE-1", "file_name": "other.pdf", "file_size": 2, "bexio_id": None}])
        self.assertEqual((writes, listed), ([], ["other.pdf"]))

    def test_a_file_matching_two_planned_files_is_listed(self):
        files = {"f-1": meta("f-1", "a.pdf", b"xx"), "f-2": meta("f-2", "a.pdf", b"yy")}
        writes, listed = self.plan_for(files, [{"name": "FILE-1", "file_name": "a.pdf", "file_size": 2, "bexio_id": None}])
        self.assertEqual((writes, listed), ([], ["a.pdf"]))

    def test_two_files_claiming_one_planned_file_are_both_listed(self):
        attached = [{"name": "FILE-1", "file_name": "a.pdf", "file_size": 2, "bexio_id": None},
                    {"name": "FILE-2", "file_name": "a.pdf", "file_size": 2, "bexio_id": None}]
        writes, listed = self.plan_for({"f-1": meta("f-1", "a.pdf", b"xx")}, attached)
        self.assertEqual((writes, listed), ([], ["a.pdf", "a.pdf"]))

    def test_a_planned_file_already_keyed_on_another_file_is_not_claimed_again(self):
        attached = [{"name": "FILE-1", "file_name": "a.pdf", "file_size": 2, "bexio_id": "f-1"},
                    {"name": "FILE-2", "file_name": "a.pdf", "file_size": 2, "bexio_id": None}]
        writes, listed = self.plan_for({"f-1": meta("f-1", "a.pdf", b"xx")}, attached)
        self.assertEqual((writes, listed), ([], ["a.pdf"]))

    def test_a_re_encoded_image_matches_by_name(self):
        # Frappe strips the EXIF of an image: the size differs, the name stays
        files = {"f-1": meta("f-1", "a.jpg", b"exif-invented-bytes")}
        writes, listed = self.plan_for(files, [{"name": "FILE-1", "file_name": "a.jpg", "file_size": 10, "bexio_id": None}])
        self.assertEqual((writes, listed), ([("FILE-1", "f-1")], []))

    def test_a_pdf_of_another_size_under_the_same_name_is_listed(self):
        writes, listed = self.plan_for(self.files(), [{"name": "FILE-1", "file_name": "a.pdf", "file_size": 99, "bexio_id": None}])
        self.assertEqual((writes, listed), ([], ["a.pdf"]))

    def test_no_export_metadata_means_nothing_matches(self):
        writes, listed = self.plan_for({}, [{"name": "FILE-1", "file_name": "a.pdf", "file_size": 2, "bexio_id": None}])
        self.assertEqual((writes, listed), ([], ["a.pdf"]))


class BackfillTest(unittest.TestCase):
    def test_backfill_writes_the_bexio_id_on_each_file_it_is_given(self):
        erp = erp_with([], [{"name": "FILE-1", "file_name": "a.pdf", "file_size": 2, "bexio_id": None}])
        self.assertEqual(imf.backfill([("FILE-1", "f-1")], erp), 1)
        self.assertEqual(erp.get("File", "FILE-1")["bexio_id"], "f-1")
        self.assertEqual(erp.writes, 1)


class UploadMarkTest(unittest.TestCase):
    def test_an_uploaded_file_carries_its_bexio_id(self):
        erp = im.Erp("https://erp.test/api", "key", "secret")
        response = io.BytesIO(json.dumps({"message": {"name": "FILE-1"}}).encode("utf-8"))
        with mock.patch("urllib.request.urlopen", return_value=response), mock.patch.object(erp, "update") as update:
            name = imf.upload(erp, "PINV-1", "f-1", "a.pdf", b"xx")
        self.assertEqual(name, "FILE-1")
        update.assert_called_once_with("File", "FILE-1", {"bexio_id": "f-1"})


class ApplyTest(unittest.TestCase):
    def test_apply_uploads_each_file_marked_attach_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as root:
            write_export(root, files=[raw("f-1", "a.pdf", b"xx"), raw("f-2", "b.pdf", b"yyy")],
                         contents={"1": ("a.pdf", b"xx"), "2": ("b.pdf", b"yyy")})
            rows = [{"file": "f-1", "outcome": "attach", "document": "PINV-1", "doctype": "Purchase Invoice", "size": 2},
                    {"file": "f-2", "outcome": "unlinked", "document": None, "doctype": "-", "size": 3}]
            calls = []
            with mock.patch.object(imf, "upload", lambda erp, doc, file_id, name, content: calls.append((doc, file_id, name, content))):
                uploaded = imf.apply(rows, root, None)
        self.assertEqual(uploaded, 1)
        self.assertEqual(calls, [("PINV-1", "f-1", "a.pdf", b"xx")])

    def test_the_report_counts_the_files_to_backfill_and_the_unmatched_ones(self):
        text = imf.report([], "/private/export", True, backfills=([("FILE-1", "f-1")], ["a.pdf"]))
        self.assertIn("to backfill", text)
        self.assertIn("unmatched", text)
        self.assertNotIn("f-1", text)
        self.assertNotIn("a.pdf", text)

    def test_applied_report_says_files_were_uploaded(self):
        text = imf.report([], "/private/export", True, applied=True)
        self.assertIn("uploaded", text)
        self.assertNotIn("nothing was written", text)


class UploadRequestTest(unittest.TestCase):
    def test_multipart_request_carries_the_document_privacy_and_content(self):
        erp = im.Erp("https://erp.test/api", "key", "secret")
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
        erp = im.Erp("https://erp.test/api", "key", "secret")
        req = imf.upload_request(erp, "PINV-1", "a\r\nX-Evil: 1.pdf", b"x")
        self.assertIn(b'filename="a__X-Evil: 1.pdf"', req.data)


class ArchiveTest(unittest.TestCase):
    def test_archive_request_names_the_folder_and_no_document(self):
        erp = im.Erp("https://erp.test/api", "key", "secret")
        req = imf.archive_request(erp, "Scan.pdf", b"%PDF-invented")
        self.assertIn(b'name="folder"\r\n\r\nHome/bexio Archive\r\n', req.data)
        self.assertIn(b'name="is_private"\r\n\r\n1\r\n', req.data)
        self.assertNotIn(b'name="doctype"', req.data)
        self.assertNotIn(b'name="docname"', req.data)
        self.assertIn(b"%PDF-invented", req.data)

    def test_an_archived_upload_carries_its_bexio_id(self):
        erp = im.Erp("https://erp.test/api", "key", "secret")
        response = io.BytesIO(json.dumps({"message": {"name": "FILE-2"}}).encode("utf-8"))
        with mock.patch("urllib.request.urlopen", return_value=response), mock.patch.object(erp, "update") as update:
            name = imf.upload_archive(erp, "f-9", "a.pdf", b"xx")
        self.assertEqual(name, "FILE-2")
        update.assert_called_once_with("File", "FILE-2", {"bexio_id": "f-9"})

    def test_the_archive_folder_is_created_once_under_home(self):
        erp = mock.Mock()
        erp.list.return_value = []
        imf.ensure_archive_folder(erp)
        erp.insert.assert_called_once_with("File", {"file_name": "bexio Archive", "is_folder": 1, "folder": "Home"})
        erp.list.return_value = [{"name": imf.ARCHIVE_FOLDER}]
        imf.ensure_archive_folder(erp)
        erp.insert.assert_called_once()

    def test_apply_archives_each_unlinked_file_once_and_creates_the_folder_first(self):
        with tempfile.TemporaryDirectory() as root:
            write_export(root, files=[raw("f-2", "b.pdf", b"yyy")], contents={"2": ("b.pdf", b"yyy")})
            rows = [{"file": "f-2", "outcome": "archive", "document": None, "doctype": "-", "size": 3}]
            calls, folders = [], []
            with mock.patch.object(imf, "ensure_archive_folder", lambda erp: folders.append(erp)), \
                    mock.patch.object(imf, "upload_archive", lambda erp, file_id, name, content: calls.append((file_id, name, content))):
                uploaded = imf.apply(rows, root, "erp")
        self.assertEqual(uploaded, 1)
        self.assertEqual(folders, ["erp"])
        self.assertEqual(calls, [("f-2", "b.pdf", b"yyy")])


class UploadErrorTest(unittest.TestCase):
    def test_a_failed_upload_names_the_status_and_ERPNext_s_reason(self):
        erp = im.Erp("https://erp.test/api", "key", "secret")
        body = json.dumps({"exc_type": "PermissionError", "exception": "PermissionError: no access\nsecond line"}).encode("utf-8")
        failure = urllib.error.HTTPError("https://erp.test/api/method/upload_file", 403, "Forbidden", {}, io.BytesIO(body))
        with mock.patch("urllib.request.urlopen", side_effect=failure):
            with self.assertRaises(im.ErpError) as ctx:
                imf.upload(erp, "PINV-1", 7, "a.pdf", b"x")
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertIn("PermissionError: no access", str(ctx.exception))
        self.assertNotIn("second line", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
