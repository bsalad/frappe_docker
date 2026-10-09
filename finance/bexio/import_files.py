"""Attach the bexio files (the PDFs and receipts) to the ERPNext documents they belong to.

Step four of the bexio pipeline, after import_purchase.py has the bills and expenses as
Purchase Invoices. A bill or an expense lists its files by bexio id (attachment_ids). Each
file becomes a private ERPNext File on the Purchase Invoice with the same bexio_id, so the
books keep their vouchers (GeBüV, see finance/docs/archiving.md). The export holds the
metadata in files.json and the content in files/<id><ext>.

The live run belongs to erp-a2ma. The command line here only runs the dry run, which reads
ERPNext and writes nothing:

    python3 finance/bexio/import_files.py --dry-run [--export DIR]

--export defaults to the newest directory under <private>/bexio-export/. The dry run prints
totals per doctype only. The bexio ids of the files it cannot place go to
<private>/bexio-files-dry-run.txt, never to the screen or the repository.

Idempotent: a file is already attached when its Purchase Invoice has a File with the marker
"bexio file <id>" as its description, or with the same name and size. The live run skips
those. A file that no document lists is listed, not attached; so is a file that two documents
list, since a File has one document.

Standard library only, apart from import_master.py and import_purchase.py.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

import import_master as im
import import_purchase as ip

DOCTYPE = "Purchase Invoice"  # bills and expenses both become Purchase Invoices
FILES_FILE = "files.json"
CONTENT_DIR = "files"
PROBLEMS_FILE = "bexio-files-dry-run.txt"
MARKER = "bexio file {}"

ATTACH, ATTACHED = "attach", "attached"
PROBLEMS = ("unlinked", "shared", "no metadata", "no document", "no content", "size differs")


class Lookups:
    """What the plan reads from ERPNext: the Purchase Invoices by bexio_id, and the Files already on them."""

    def __init__(self, documents, attached):
        self.documents = documents  # bexio id -> Purchase Invoice name
        self.attached = attached    # Purchase Invoice name -> [File rows]

    @classmethod
    def from_erp(cls, erp):
        documents = {r["bexio_id"]: r["name"] for r in erp.list(DOCTYPE, [["bexio_id", "is", "set"]], ["name", "bexio_id"])}
        attached = {}
        for f in erp.list("File", [["attached_to_doctype", "=", DOCTYPE]],
                          ["name", "attached_to_name", "file_name", "file_size"]):
            # description is a long text field, which a list query refuses; it is read per File
            f["description"] = erp.get("File", f["name"]).get("description")
            attached.setdefault(f["attached_to_name"], []).append(f)
        return cls(documents, attached)


def attachment_owners(bills, expenses):
    """bexio file id -> the bexio ids of the records that list it."""
    owners = {}
    for record in list(bills) + list(expenses):
        for file_id in record.get("attachment_ids") or []:
            owners.setdefault(str(file_id), []).append(str(record["id"]))
    return owners


def load_files(export_dir):
    """The file metadata by bexio id, or None when the export has no files.json (its login gets a 403 so far)."""
    path = os.path.join(export_dir, FILES_FILE)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return {str(x["id"]): x for x in json.load(f)}


def content_path(export_dir, file_id, meta):
    return os.path.join(export_dir, CONTENT_DIR, file_id + os.path.splitext(meta.get("name") or "")[1])


def already_attached(file_id, meta, files):
    # the marker is compared whole: "bexio file 1" must not match "bexio file 12"
    return any(
        f.get("description") == MARKER.format(file_id)
        or (f.get("file_name") == meta["name"] and f.get("file_size") == int(meta["size"]))
        for f in files
    )


def classify(file_id, meta, holders, lookups, export_dir):
    """What the live run does with one file: its outcome, and the Purchase Invoice it goes to."""
    if not holders:
        return "unlinked", None
    if len(holders) > 1:
        return "shared", None
    # the document is checked before the metadata, so the dry run counts the documents even without files.json
    document = lookups.documents.get(holders[0])
    if document is None:
        return "no document", None
    if meta is None:
        return "no metadata", document
    if already_attached(file_id, meta, lookups.attached.get(document, [])):
        return ATTACHED, document
    path = content_path(export_dir, file_id, meta)
    if not os.path.isfile(path):
        return "no content", document
    if os.path.getsize(path) != int(meta["size"]):
        return "size differs", document
    return ATTACH, document


def plan(files, owners, lookups, export_dir):
    """One row per file that is referenced or has metadata: its outcome, the document and the size in bytes."""
    rows = []
    for file_id in sorted(set(owners) | set(files)):
        holders = owners.get(file_id, [])
        meta = files.get(file_id)
        outcome, document = classify(file_id, meta, holders, lookups, export_dir)
        rows.append({
            "file": file_id, "outcome": outcome, "doctype": DOCTYPE if holders else "-",
            "document": document, "size": int(meta["size"]) if meta else None,
        })
    return rows


def run(export_dir, erp):
    """Plan every file of the export against ERPNext; returns the rows and whether the export has files.json."""
    bills, expenses = ip.load_records(export_dir)
    files = load_files(export_dir)
    lookups = Lookups.from_erp(erp)
    rows = plan(files or {}, attachment_owners(bills, expenses), lookups, export_dir)
    return rows, files is not None


def report(rows, export_dir, has_files):
    """Totals only: no file ids, no names."""
    lines = ["file dry run from {}".format(export_dir),
             "files.json: {}".format("in the export" if has_files else "not in the export, so no file has its bytes or content checked yet"),
             "{:<18}{:>7}{:>14}{:>9}{:>14}{:>10}{:>10}".format("doctype", "files", "bytes", "attach", "bytes", "attached", "problems")]
    for doctype in sorted({r["doctype"] for r in rows}):
        group = [r for r in rows if r["doctype"] == doctype]
        todo = [r for r in group if r["outcome"] == ATTACH]
        done = [r for r in group if r["outcome"] == ATTACHED]
        problems = [r for r in group if r["outcome"] in PROBLEMS]
        lines.append("{:<18}{:>7}{:>14}{:>9}{:>14}{:>10}{:>10}".format(
            doctype, len(group), _bytes(group, has_files), len(todo), _bytes(todo, has_files), len(done), len(problems)))
    counts = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    for outcome in (ATTACH, ATTACHED) + PROBLEMS:
        if counts.get(outcome):
            lines.append("  {:<14}{:>6}".format(outcome, counts[outcome]))
    lines.append("dry run: nothing was written")
    return "\n".join(lines)


def _bytes(rows, has_files):
    return sum(r["size"] or 0 for r in rows) if has_files else "-"


def _header_safe(name):
    # a quote, CR or LF in a name would end the filename parameter or the header line
    return "".join("_" if c in '"\r\n' else c for c in name)


def upload_request(erp, document, file_name, content):
    """The multipart request for Frappe's upload_file: one private File on the Purchase Invoice. Built here, sent by upload() in the live run only."""
    boundary = uuid.uuid4().hex
    sep = "--{}\r\n".format(boundary)
    body = b""
    for key, value in (("doctype", DOCTYPE), ("docname", document), ("is_private", "1")):
        body += (sep + 'Content-Disposition: form-data; name="{}"\r\n\r\n{}\r\n'.format(key, value)).encode("utf-8")
    body += (sep + 'Content-Disposition: form-data; name="file"; filename="{}"\r\n'
             'Content-Type: application/octet-stream\r\n\r\n'.format(_header_safe(file_name))).encode("utf-8")
    body += content + ("\r\n--{}--\r\n".format(boundary)).encode("ascii")
    req = urllib.request.Request(erp._url + "/api/method/upload_file", data=body, method="POST")
    req.add_header("Authorization", erp._auth)  # the Erp object keeps its secret; it is only read here
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    return req


def upload(erp, document, file_id, file_name, content):
    """Upload one file and mark it with its bexio id, so a second run skips it. The live run calls this; the dry run never does."""
    try:
        with urllib.request.urlopen(upload_request(erp, document, file_name, content), timeout=300) as resp:
            name = json.loads(resp.read().decode("utf-8"))["message"]["name"]
    except urllib.error.HTTPError as err:
        raise im.ErpError(err.code, "upload_file for {}".format(document)) from None
    erp.update("File", name, {"description": MARKER.format(file_id)})
    return name


def main(argv):
    parser = argparse.ArgumentParser(description="Plan the attachment of the bexio files to the Purchase Invoices (dry run).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if not args.dry_run:
        print("the live run is erp-a2ma's; this command takes --dry-run only", file=sys.stderr)
        return 2
    export_dir = args.export or im.newest_export()
    try:
        rows, has_files = run(export_dir, im.Erp.from_file(args.token_file))
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    print(report(rows, export_dir, has_files))
    problems = [r for r in rows if r["outcome"] in PROBLEMS]
    if problems:
        ip.write_private(os.path.join(im.PRIVATE, PROBLEMS_FILE),
                         ["file {}: {}".format(r["file"], r["outcome"]) for r in problems])
        print("{} file(s) listed by bexio id in {}".format(len(problems), os.path.join(im.PRIVATE, PROBLEMS_FILE)), file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
