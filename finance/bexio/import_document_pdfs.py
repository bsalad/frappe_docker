"""Attach the bexio document PDFs (invoices, credit vouchers, orders, offers) to the ERPNext documents they belong to.

Step ten of the bexio pipeline (the README's import chain), after import_sales.py has the documents in ERPNext. export.py writes each
PDF to documents/<kind>-<id>.pdf and lists it in document_pdfs.json (kind, id, file, bytes). Each document
gets one private ERPNext File, attached to it and keyed by the document's bexio_id: the key import_sales.py
gives the document ("credit-<id>" for a credit voucher, the plain id otherwise). A second run finds the File
and skips it, so the pass can be rerun.

--dry-run (the default) reads ERPNext and writes nothing. --apply uploads each PDF whose document has no
File yet:

    python3 finance/bexio/import_document_pdfs.py [--dry-run] [--export DIR]
    python3 finance/bexio/import_document_pdfs.py --apply [--export DIR]

--export defaults to the newest directory under <private>/bexio-export/. The dry run prints totals per kind
only. The ids it cannot place go to <private>/bexio-document-pdfs-dry-run.txt, never to the screen.

Rows: attach (uploaded by --apply), attached (a File with the bexio_id, or with the file's name and no
bexio_id, is on the document already), no document (the document is not in ERPNext yet, or the kind has no
ERPNext document: a kind not listed in KINDS), no content and size differs (problems: the PDF on disk is not the export's).
A File uploaded without its bexio_id (a run that stopped between the upload and the key) is recognised by
its name, so it is not uploaded twice.

Standard library only, apart from import_files.py and import_master.py.
"""

import argparse
import json
import os
import sys

import export as ex
import import_files as imf
import import_master as im
import import_purchase as ip

PDFS_FILE = "document_pdfs.json"
PROBLEMS_FILE = "bexio-document-pdfs-dry-run.txt"

ATTACH, ATTACHED, NO_DOCUMENT = "attach", "attached", "no document"
PROBLEMS = ("no content", "size differs")

# the ERPNext doctype of each kind of document, and the prefix import_sales.py puts in front of its bexio_id.
KINDS = {
    "invoice": ("Sales Invoice", ""),
    "credit_voucher": ("Sales Invoice", "credit-"),
    "order": ("Sales Order", ""),
    "offer": ("Quotation", ""),
    "delivery": ("Delivery Note", ""),
}
DOCTYPES = sorted({doctype for doctype, _ in KINDS.values()})


class Lookups:
    """What the plan reads from ERPNext: the documents by (doctype, bexio_id), and the Files on each document.

    The Files on a document are (bexio_id, file name) pairs, the bexio_id empty for a File that has none.
    """

    def __init__(self, documents, attached):
        self.documents = documents  # (doctype, bexio id) -> ERPNext name
        self.attached = attached    # (doctype, ERPNext name) -> [(bexio id, file name)]

    @classmethod
    def from_erp(cls, erp):
        documents = {}
        for doctype in DOCTYPES:
            for r in erp.list(doctype, [["bexio_id", "is", "set"]], ["name", "bexio_id"]):
                documents[(doctype, r["bexio_id"])] = r["name"]
        attached = {}
        for f in erp.list("File", [["attached_to_doctype", "in", DOCTYPES]],
                          ["attached_to_doctype", "attached_to_name", "file_name", "bexio_id"]):
            attached.setdefault((f["attached_to_doctype"], f["attached_to_name"]), []).append(
                (f["bexio_id"] or "", f["file_name"]))
        return cls(documents, attached)


def document_pdf_path(export_dir, row):
    return os.path.join(export_dir, ex.DOCUMENTS, row["file"])


def load_pdfs(export_dir):
    """The rows of document_pdfs.json, or None when the export has none."""
    path = os.path.join(export_dir, PDFS_FILE)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def plan(pdfs, lookups, export_dir):
    """One row per PDF of the export: its outcome, the ERPNext doctype and document it goes to, and its bexio id."""
    rows = []
    for row in pdfs:
        kind = row["kind"]
        bexio_id = None
        doctype, document, outcome = None, None, None
        if kind in KINDS:
            doctype, prefix = KINDS[kind]
            bexio_id = prefix + str(row["id"])
            document = lookups.documents.get((doctype, bexio_id))
        if document is None:
            outcome = NO_DOCUMENT
        elif any(key == bexio_id or (not key and name == row["file"])
                 for key, name in lookups.attached.get((doctype, document), [])):
            outcome = ATTACHED
        else:
            path = document_pdf_path(export_dir, row)
            if not os.path.isfile(path):
                outcome = "no content"
            elif os.path.getsize(path) != row["bytes"]:
                outcome = "size differs"
            else:
                outcome = ATTACH
        rows.append({"kind": kind, "id": row["id"], "bexio_id": bexio_id, "file": row["file"],
                     "doctype": doctype, "document": document, "outcome": outcome, "bytes": row["bytes"]})
    return rows


def run(export_dir, erp):
    """Plan every PDF of the export against ERPNext; returns the rows, or None when the export has no document_pdfs.json."""
    pdfs = load_pdfs(export_dir)
    if pdfs is None:
        return None
    return plan(pdfs, Lookups.from_erp(erp), export_dir)


def apply(rows, export_dir, erp):
    """Upload each PDF the plan marks to attach, as a private File on its document. Returns how many were uploaded."""
    uploaded = 0
    for row in rows:
        if row["outcome"] != ATTACH:
            continue
        with open(document_pdf_path(export_dir, row), "rb") as f:
            imf.upload(erp, row["document"], row["bexio_id"], row["file"], f.read(), doctype=row["doctype"])
        uploaded += 1
    return uploaded


def report(rows, export_dir, applied=False):
    """Totals only: no ids, no names. One line per kind, then one per outcome that occurs."""
    lines = ["document PDFs from {}".format(export_dir),
             "{:<16}{:>7}{:>14}{:>9}{:>10}{:>10}".format("kind", "pdfs", "bytes", "attach", "attached", "problems")]
    for kind in sorted({r["kind"] for r in rows}):
        group = [r for r in rows if r["kind"] == kind]
        lines.append("{:<16}{:>7}{:>14}{:>9}{:>10}{:>10}".format(
            kind, len(group), sum(r["bytes"] for r in group),
            sum(r["outcome"] == ATTACH for r in group), sum(r["outcome"] == ATTACHED for r in group),
            sum(r["outcome"] in PROBLEMS for r in group)))
    counts = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    for outcome in (ATTACH, ATTACHED, NO_DOCUMENT) + PROBLEMS:
        if counts.get(outcome):
            lines.append("  {:<14}{:>6}".format(outcome, counts[outcome]))
    lines.append("applied: the PDFs marked attach are uploaded as private Files" if applied
                 else "dry run: nothing was written")
    return "\n".join(lines)


def problem_line(row):
    return "document {}:{}: {}".format(row["kind"], row["id"], row["outcome"])


def main(argv):
    parser = argparse.ArgumentParser(description="Attach the bexio document PDFs to their ERPNext documents (a dry run by default).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals (the default)")
    parser.add_argument("--apply", action="store_true", help="upload the PDFs that would be attached, as private Files")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are one or the other")
    export_dir = args.export or im.newest_export()
    try:
        erp = im.Erp.from_file(args.token_file)
        rows = run(export_dir, erp)
        if rows is None:
            print("aborted: no {} in {}; run export.py --complete first".format(PDFS_FILE, export_dir), file=sys.stderr)
            return 2
        if args.apply:
            print("uploaded {} document PDF(s)".format(apply(rows, export_dir, erp)))
            rows = run(export_dir, erp)
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    print(report(rows, export_dir, applied=args.apply))
    listed = [r for r in rows if r["outcome"] != ATTACH and r["outcome"] != ATTACHED]
    if listed:
        ip.write_private(os.path.join(im.PRIVATE, PROBLEMS_FILE), [problem_line(r) for r in listed])
        print("{} line(s) listed in {}".format(len(listed), os.path.join(im.PRIVATE, PROBLEMS_FILE)), file=sys.stderr)
    return 1 if any(r["outcome"] in PROBLEMS for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
