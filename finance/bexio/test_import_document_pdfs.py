"""Offline tests for import_document_pdfs.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import io
import json
import os
import tempfile
import unittest
import urllib.request
from unittest import mock

import import_document_pdfs as idp
import import_files as imf
import import_master as im
from test_import_master import FakeErp


def write_export(root, pdfs=None, contents=None):
    """An export directory: document_pdfs.json (None for no file) and the PDFs under documents/, {file: bytes}."""
    if pdfs is not None:
        with open(os.path.join(root, idp.PDFS_FILE), "w", encoding="utf-8") as f:
            json.dump(pdfs, f)
    os.makedirs(os.path.join(root, idp.ex.DOCUMENTS), exist_ok=True)
    for name, data in (contents or {}).items():
        with open(os.path.join(root, idp.ex.DOCUMENTS, name), "wb") as f:
            f.write(data)


def pdf(kind, doc_id, data=b"%PDF-invented"):
    """A document_pdfs.json row for a PDF of this kind and id, and the bytes it names."""
    return {"kind": kind, "id": doc_id, "file": "{}-{}.pdf".format(kind, doc_id), "bytes": len(data)}


def erp_with(sales_invoices=(), sales_orders=(), quotations=(), files=()):
    return FakeErp({"Sales Invoice": list(sales_invoices), "Sales Order": list(sales_orders),
                    "Quotation": list(quotations), "File": list(files)})


class LookupsTest(unittest.TestCase):
    def test_documents_are_keyed_by_doctype_and_bexio_id(self):
        erp = erp_with(sales_invoices=[{"name": "SINV-1", "bexio_id": "1"}, {"name": "SINV-9", "bexio_id": None}],
                       quotations=[{"name": "QTN-1", "bexio_id": "3"}])
        lookups = idp.Lookups.from_erp(erp)
        self.assertEqual(lookups.documents, {("Sales Invoice", "1"): "SINV-1", ("Quotation", "3"): "QTN-1"})

    def test_files_on_a_document_are_read_with_their_bexio_id_and_name(self):
        erp = erp_with(files=[
            {"name": "FILE-1", "attached_to_doctype": "Sales Invoice", "attached_to_name": "SINV-1",
             "file_name": "invoice-1.pdf", "bexio_id": "1"},
            {"name": "FILE-2", "attached_to_doctype": "Sales Invoice", "attached_to_name": "SINV-1",
             "file_name": "scan.pdf", "bexio_id": None},
            {"name": "FILE-3", "attached_to_doctype": "Purchase Invoice", "attached_to_name": "PINV-1",
             "file_name": "bill.pdf", "bexio_id": "f-1"},
        ])
        self.assertEqual(idp.Lookups.from_erp(erp).attached, {
            ("Sales Invoice", "SINV-1"): [("1", "invoice-1.pdf"), ("", "scan.pdf")]})


class PlanTest(unittest.TestCase):
    def test_each_kind_goes_to_its_doctype_with_the_key_import_sales_gives_it(self):
        pdfs = [pdf("invoice", 1), pdf("credit_voucher", 4), pdf("order", 2), pdf("offer", 3)]
        erp = erp_with(sales_invoices=[{"name": "SINV-1", "bexio_id": "1"}, {"name": "SINV-4", "bexio_id": "credit-4"}],
                       sales_orders=[{"name": "SO-2", "bexio_id": "2"}], quotations=[{"name": "QTN-3", "bexio_id": "3"}])
        with tempfile.TemporaryDirectory() as root:
            rows = idp.plan(pdfs, idp.Lookups.from_erp(erp), root)
        self.assertEqual([(r["doctype"], r["document"], r["bexio_id"], r["outcome"]) for r in rows], [
            ("Sales Invoice", "SINV-1", "1", "no content"),
            ("Sales Invoice", "SINV-4", "credit-4", "no content"),
            ("Sales Order", "SO-2", "2", "no content"),
            ("Quotation", "QTN-3", "3", "no content"),
        ])

    def test_a_pdf_whose_document_is_not_in_erpnext_yet_is_no_document(self):
        with tempfile.TemporaryDirectory() as root:
            rows = idp.plan([pdf("invoice", 1)], idp.Lookups.from_erp(erp_with()), root)
        self.assertEqual(rows[0]["outcome"], idp.NO_DOCUMENT)
        self.assertIsNone(rows[0]["document"])

    def test_a_delivery_has_no_erpnext_document(self):
        # invented delivery: the kind has no doctype, so it is listed and never attached
        with tempfile.TemporaryDirectory() as root:
            rows = idp.plan([pdf("delivery", 7)], idp.Lookups.from_erp(erp_with(sales_invoices=[{"name": "SINV-7", "bexio_id": "7"}])), root)
        self.assertEqual((rows[0]["outcome"], rows[0]["doctype"], rows[0]["document"]), (idp.NO_DOCUMENT, None, None))

    def test_a_pdf_on_the_document_already_is_attached_by_its_bexio_id(self):
        erp = erp_with(sales_invoices=[{"name": "SINV-1", "bexio_id": "1"}], files=[
            {"name": "FILE-1", "attached_to_doctype": "Sales Invoice", "attached_to_name": "SINV-1",
             "file_name": "renamed.pdf", "bexio_id": "1"}])
        data = b"%PDF-invented"
        with tempfile.TemporaryDirectory() as root:
            write_export(root, pdfs=[pdf("invoice", 1, data)], contents={"invoice-1.pdf": data})
            rows = idp.run(root, erp)
        self.assertEqual(rows[0]["outcome"], idp.ATTACHED)

    def test_a_file_without_a_bexio_id_is_attached_when_its_name_is_on_the_document(self):
        # a run that stopped between the upload and the key wrote the File without its bexio_id: not uploaded again
        erp = erp_with(sales_invoices=[{"name": "SINV-1", "bexio_id": "1"}], files=[
            {"name": "FILE-1", "attached_to_doctype": "Sales Invoice", "attached_to_name": "SINV-1",
             "file_name": "invoice-1.pdf", "bexio_id": None}])
        with tempfile.TemporaryDirectory() as root:
            rows = idp.plan([pdf("invoice", 1)], idp.Lookups.from_erp(erp), root)
        self.assertEqual(rows[0]["outcome"], idp.ATTACHED)

    def test_a_file_of_another_document_does_not_count(self):
        erp = erp_with(sales_invoices=[{"name": "SINV-1", "bexio_id": "1"}, {"name": "SINV-2", "bexio_id": "2"}], files=[
            {"name": "FILE-1", "attached_to_doctype": "Sales Invoice", "attached_to_name": "SINV-2",
             "file_name": "invoice-1.pdf", "bexio_id": "1"}])
        data = b"%PDF-invented"
        with tempfile.TemporaryDirectory() as root:
            write_export(root, contents={"invoice-1.pdf": data})
            rows = idp.plan([pdf("invoice", 1, data)], idp.Lookups.from_erp(erp), root)
        self.assertEqual(rows[0]["outcome"], idp.ATTACH)

    def test_a_pdf_missing_on_disk_is_a_problem(self):
        erp = erp_with(sales_invoices=[{"name": "SINV-1", "bexio_id": "1"}])
        with tempfile.TemporaryDirectory() as root:
            rows = idp.plan([pdf("invoice", 1)], idp.Lookups.from_erp(erp), root)
        self.assertEqual(rows[0]["outcome"], "no content")

    def test_a_pdf_of_another_size_than_the_export_is_a_problem(self):
        erp = erp_with(sales_invoices=[{"name": "SINV-1", "bexio_id": "1"}])
        with tempfile.TemporaryDirectory() as root:
            write_export(root, contents={"invoice-1.pdf": b"%PDF-short"})
            row = dict(pdf("invoice", 1), bytes=99)
            rows = idp.plan([row], idp.Lookups.from_erp(erp), root)
        self.assertEqual(rows[0]["outcome"], "size differs")

    def test_a_pdf_with_its_document_and_content_is_to_attach(self):
        data = b"%PDF-invented"
        erp = erp_with(sales_invoices=[{"name": "SINV-1", "bexio_id": "1"}])
        with tempfile.TemporaryDirectory() as root:
            write_export(root, contents={"invoice-1.pdf": data})
            rows = idp.plan([pdf("invoice", 1, data)], idp.Lookups.from_erp(erp), root)
        self.assertEqual((rows[0]["outcome"], rows[0]["document"], rows[0]["doctype"]), (idp.ATTACH, "SINV-1", "Sales Invoice"))


class RunTest(unittest.TestCase):
    def test_an_export_without_document_pdfs_has_no_plan(self):
        with tempfile.TemporaryDirectory() as root:
            write_export(root)
            self.assertIsNone(idp.run(root, erp_with()))

    def test_an_export_with_no_rows_has_an_empty_plan(self):
        with tempfile.TemporaryDirectory() as root:
            write_export(root, pdfs=[])
            self.assertEqual(idp.run(root, erp_with()), [])


class ApplyTest(unittest.TestCase):
    def test_apply_uploads_each_pdf_to_attach_on_its_own_doctype(self):
        data = b"%PDF-invented"
        erp = erp_with(sales_invoices=[{"name": "SINV-4", "bexio_id": "credit-4"}], quotations=[{"name": "QTN-3", "bexio_id": "3"}])
        with tempfile.TemporaryDirectory() as root:
            write_export(root, pdfs=[pdf("credit_voucher", 4, data), pdf("offer", 3, data), pdf("delivery", 5, data)],
                         contents={"credit_voucher-4.pdf": data, "offer-3.pdf": data, "delivery-5.pdf": data})
            rows = idp.run(root, erp)
            calls = []
            with mock.patch.object(imf, "upload", lambda erp, doc, file_id, name, content, doctype: calls.append((doctype, doc, file_id, name, content))):
                uploaded = idp.apply(rows, root, erp)
        self.assertEqual(uploaded, 2)
        self.assertEqual(calls, [("Sales Invoice", "SINV-4", "credit-4", "credit_voucher-4.pdf", data),
                                 ("Quotation", "QTN-3", "3", "offer-3.pdf", data)])

    def test_a_second_run_finds_the_files_the_first_one_attached(self):
        data = b"%PDF-invented"
        erp = erp_with(sales_invoices=[{"name": "SINV-1", "bexio_id": "1"}])

        def fake_upload(erp, document, file_id, file_name, content, doctype=imf.DOCTYPE):
            # what upload() does: the File on the document, keyed by the bexio id
            erp.insert("File", {"attached_to_doctype": doctype, "attached_to_name": document,
                                "file_name": file_name, "bexio_id": file_id})

        with tempfile.TemporaryDirectory() as root:
            write_export(root, pdfs=[pdf("invoice", 1, data)], contents={"invoice-1.pdf": data})
            with mock.patch.object(imf, "upload", fake_upload):
                self.assertEqual(idp.apply(idp.run(root, erp), root, erp), 1)
            rows = idp.run(root, erp)
            self.assertEqual(idp.apply(rows, root, erp), 0)
        self.assertEqual([r["outcome"] for r in rows], [idp.ATTACHED])
        self.assertEqual(sum(1 for f in erp.docs["File"].values() if f.get("bexio_id") == "1"), 1)

    def test_an_upload_request_names_the_sales_document_not_a_purchase_invoice(self):
        erp = im.Erp("https://erp.test/api", "key", "secret")
        req = imf.upload_request(erp, "SINV-1", "invoice-1.pdf", b"%PDF-invented", doctype="Sales Invoice")
        self.assertEqual(req.full_url, "https://erp.test/api/method/upload_file")
        body = req.data
        self.assertIn(b'name="doctype"\r\n\r\nSales Invoice', body)
        self.assertIn(b'name="docname"\r\n\r\nSINV-1', body)
        self.assertIn(b'name="is_private"\r\n\r\n1', body)

    def test_an_upload_without_a_doctype_is_still_a_purchase_invoice(self):
        erp = im.Erp("https://erp.test/api", "key", "secret")
        req = imf.upload_request(erp, "PINV-1", "a.pdf", b"x")
        self.assertIn(b'name="doctype"\r\n\r\nPurchase Invoice', req.data)


class ReportTest(unittest.TestCase):
    def test_the_report_has_totals_per_kind_and_no_ids_or_names(self):
        rows = [
            {"kind": "invoice", "id": 1, "bexio_id": "1", "file": "invoice-1.pdf", "doctype": "Sales Invoice",
             "document": "SINV-1", "outcome": idp.ATTACH, "bytes": 10},
            {"kind": "delivery", "id": 7, "bexio_id": None, "file": "delivery-7.pdf", "doctype": None,
             "document": None, "outcome": idp.NO_DOCUMENT, "bytes": 20},
        ]
        text = idp.report(rows, "/invented/export")
        self.assertIn("invoice", text)
        self.assertIn("delivery", text)
        self.assertIn("dry run: nothing was written", text)
        self.assertNotIn("SINV-1", text)
        self.assertNotIn("invoice-1.pdf", text)

    def test_the_applied_report_says_files_were_uploaded(self):
        self.assertIn("uploaded", idp.report([], "/invented/export", applied=True))


class MainTest(unittest.TestCase):
    def test_an_export_without_document_pdfs_aborts_with_status_2(self):
        with tempfile.TemporaryDirectory() as root:
            write_export(root)
            with mock.patch.object(im.Erp, "from_file", return_value=erp_with()):
                self.assertEqual(idp.main(["--export", root]), 2)

    def test_a_dry_run_writes_nothing_to_erpnext(self):
        data = b"%PDF-invented"
        erp = erp_with(sales_invoices=[{"name": "SINV-1", "bexio_id": "1"}])
        with tempfile.TemporaryDirectory() as root:
            write_export(root, pdfs=[pdf("invoice", 1, data)], contents={"invoice-1.pdf": data})
            with mock.patch.object(im.Erp, "from_file", return_value=erp), \
                    mock.patch.object(imf, "upload") as upload, mock.patch.object(idp.ip, "write_private") as write:
                self.assertEqual(idp.main(["--export", root]), 0)
        upload.assert_not_called()
        write.assert_not_called()

    def test_an_apply_run_uploads_the_pdfs_it_plans(self):
        data = b"%PDF-invented"
        erp = erp_with(sales_invoices=[{"name": "SINV-1", "bexio_id": "1"}])
        with tempfile.TemporaryDirectory() as root:
            write_export(root, pdfs=[pdf("invoice", 1, data)], contents={"invoice-1.pdf": data})
            with mock.patch.object(im.Erp, "from_file", return_value=erp), mock.patch.object(imf, "upload") as upload:
                self.assertEqual(idp.main(["--export", root, "--apply"]), 0)
        upload.assert_called_once()

    def test_a_problem_makes_the_run_fail_and_lists_its_ids_in_the_private_file(self):
        erp = erp_with(sales_invoices=[{"name": "SINV-1", "bexio_id": "1"}])
        with tempfile.TemporaryDirectory() as root:
            write_export(root, pdfs=[pdf("invoice", 1)])
            with mock.patch.object(im.Erp, "from_file", return_value=erp), \
                    mock.patch.object(idp.ip, "write_private") as write:
                self.assertEqual(idp.main(["--export", root]), 1)
        lines = write.call_args[0][1]
        self.assertEqual(lines, ["document invoice:1: no content"])


if __name__ == "__main__":
    unittest.main()
