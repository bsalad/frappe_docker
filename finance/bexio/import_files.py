"""Attach the bexio files (the PDFs and receipts) to the ERPNext documents they belong to.

Step four of the bexio pipeline, after import_purchase.py has the bills and expenses as
Purchase Invoices. A bill or an expense lists its files by bexio id (attachment_ids). Each
file becomes a private ERPNext File on the Purchase Invoice with the same bexio_id, so the
books keep their vouchers (GeBüV, see finance/docs/archiving.md). The export holds the
metadata in files.json and the content in files/<id><ext>.

--dry-run (the default) reads ERPNext and writes nothing. --apply uploads each file that
would be attached, through the API user (upload_file, then the bexio id as its description):

    python3 finance/bexio/import_files.py [--dry-run] [--export DIR]
    python3 finance/bexio/import_files.py --apply [--export DIR]

--export defaults to the newest directory under <private>/bexio-export/. A file is found by its uuid:
files.json (the /3.0/files list) and bill_attachments.json (the attachments of the bills, export.py)
both hold the uuid that a bill lists in attachment_ids. The dry run prints
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

import export as ex
import import_master as im
import import_purchase as ip

DOCTYPE = "Purchase Invoice"  # bills and expenses both become Purchase Invoices
FILES_FILE = "files.json"
BILL_ATTACHMENTS_FILE = "bill_attachments.json"
CONTENT_DIR = "files"
PROBLEMS_FILE = "bexio-files-dry-run.txt"
MARKER = "bexio file {}"

ATTACH, ATTACHED, REENCODED = "attach", "attached", "re-encoded"
PROBLEMS = ("unlinked", "shared", "no metadata", "no document", "no content", "size differs")
IMAGES = (".jpg", ".jpeg", ".png", ".gif", ".webp")


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
    """The file metadata by file uuid, from files.json and bill_attachments.json; None when the export has neither.

    A bill lists its files by uuid (attachment_ids), so both are keyed by uuid, not by the integer id
    of /3.0/files. A file's content is named by its id (export.file_name), so content_path reads the id.
    A uuid in both lists keeps its files.json row: that row has the bexio name the File was uploaded under,
    while the bill's row is named by the uuid.
    """
    names = [n for n in (FILES_FILE, BILL_ATTACHMENTS_FILE) if os.path.exists(os.path.join(export_dir, n))]
    if not names:
        return None
    files = {}
    for name in names:
        with open(os.path.join(export_dir, name), encoding="utf-8") as f:
            for x in json.load(f):
                files.setdefault(str(x["uuid"]), with_extension(x))
    return files


def with_extension(row):
    """The row with its name carrying the extension: files.json has the name without it, and the File keeps it."""
    name, extension = row.get("name"), row.get("extension") or ""
    if name and extension and not name.lower().endswith("." + extension.lower()):
        return dict(row, name="{}.{}".format(name, extension))
    return row


def content_path(export_dir, meta):
    return os.path.join(export_dir, CONTENT_DIR, ex.file_name(meta))


def already_attached(file_id, meta, files):
    # the marker is compared whole: "bexio file 1" must not match "bexio file 12"
    return any(
        f.get("description") == MARKER.format(file_id)
        # a bill attachment whose download failed has no name or size: it matches nothing by them
        or (meta.get("name") and f.get("file_name") == meta["name"] and f.get("file_size") == meta.get("size_in_bytes"))
        for f in files
    )


def reencoded(meta, files):
    # Frappe strips the EXIF data of an uploaded image, so the File's bytes and size differ from the export's
    # while its name stays: the same picture, not a missing one. Only images, so a PDF of another size still differs.
    name = meta.get("name")
    return bool(name) and name.lower().endswith(IMAGES) and any(f.get("file_name") == name for f in files)


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
    files = lookups.attached.get(document, [])
    if already_attached(file_id, meta, files):
        return ATTACHED, document
    if reencoded(meta, files):
        return REENCODED, document
    path = content_path(export_dir, meta)
    if not os.path.isfile(path):
        return "no content", document
    if os.path.getsize(path) != meta.get("size_in_bytes"):
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
            "document": document, "size": meta.get("size_in_bytes") if meta else None,
        })
    return rows


def run(export_dir, erp):
    """Plan every file of the export against ERPNext; returns the rows and whether the export has files.json."""
    bills, expenses = ip.load_records(export_dir)
    files = load_files(export_dir)
    lookups = Lookups.from_erp(erp)
    rows = plan(files or {}, attachment_owners(bills, expenses), lookups, export_dir)
    return rows, os.path.exists(os.path.join(export_dir, FILES_FILE))


def report(rows, export_dir, has_files, applied=False):
    """Totals only: no file ids, no names."""
    lines = ["file dry run from {}".format(export_dir),
             "files.json: {}".format("in the export" if has_files else "not in the export, so no file has its bytes or content checked yet"),
             "{:<18}{:>7}{:>14}{:>9}{:>14}{:>10}{:>10}".format("doctype", "files", "bytes", "attach", "bytes", "attached", "problems")]
    for doctype in sorted({r["doctype"] for r in rows}):
        group = [r for r in rows if r["doctype"] == doctype]
        todo = [r for r in group if r["outcome"] == ATTACH]
        done = [r for r in group if r["outcome"] in (ATTACHED, REENCODED)]
        problems = [r for r in group if r["outcome"] in PROBLEMS]
        lines.append("{:<18}{:>7}{:>14}{:>9}{:>14}{:>10}{:>10}".format(
            doctype, len(group), _bytes(group, has_files), len(todo), _bytes(todo, has_files), len(done), len(problems)))
    counts = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    for outcome in (ATTACH, ATTACHED, REENCODED) + PROBLEMS:
        if counts.get(outcome):
            lines.append("  {:<14}{:>6}".format(outcome, counts[outcome]))
    lines.append("applied: the files marked attach are uploaded" if applied else "dry run: nothing was written")
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
    # Erp's base URL already ends in /api (its own paths are /resource/... and /method/...)
    req = urllib.request.Request(erp._url + "/method/upload_file", data=body, method="POST")
    req.add_header("Authorization", erp._auth)  # the Erp object keeps its secret; it is only read here
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    return req


def upload(erp, document, file_id, file_name, content):
    """Upload one file and mark it with its bexio id, so a second run skips it. The live run calls this; the dry run never does."""
    try:
        with urllib.request.urlopen(upload_request(erp, document, file_name, content), timeout=300) as resp:
            name = json.loads(resp.read().decode("utf-8"))["message"]["name"]
    except urllib.error.HTTPError as err:
        raise im.ErpError(err.code, "upload_file for {}".format(document) + im._reason(err)) from None
    erp.update("File", name, {"description": MARKER.format(file_id)})
    return name


def apply(rows, export_dir, erp):
    """Upload every file the plan marks to attach. Returns how many were uploaded."""
    files = load_files(export_dir)
    uploaded = 0
    for row in rows:
        if row["outcome"] != ATTACH:
            continue
        meta = files[row["file"]]
        with open(content_path(export_dir, meta), "rb") as f:
            upload(erp, row["document"], row["file"], meta["name"], f.read())
        uploaded += 1
    return uploaded


def main(argv):
    parser = argparse.ArgumentParser(description="Attach the bexio files to the Purchase Invoices (a dry run by default).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals (the default)")
    parser.add_argument("--apply", action="store_true", help="upload the files that would be attached, as private Files")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are one or the other")
    export_dir = args.export or im.newest_export()
    try:
        erp = im.Erp.from_file(args.token_file)
        rows, has_files = run(export_dir, erp)
        if args.apply:
            print("uploaded {} file(s)".format(apply(rows, export_dir, erp)))
            rows, has_files = run(export_dir, erp)
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    print(report(rows, export_dir, has_files, applied=args.apply))
    problems = [r for r in rows if r["outcome"] in PROBLEMS]
    listed = [r for r in rows if r["outcome"] in PROBLEMS or r["outcome"] == REENCODED]
    if listed:
        ip.write_private(os.path.join(im.PRIVATE, PROBLEMS_FILE), [problem_line(r) for r in listed])
        print("{} file(s) listed by bexio id in {}".format(len(listed), os.path.join(im.PRIVATE, PROBLEMS_FILE)), file=sys.stderr)
    return 1 if problems else 0


def problem_line(row):
    if row["outcome"] == REENCODED:
        return "file {}: attached, re-encoded by Frappe (size differs)".format(row["file"])
    return "file {}: {}".format(row["file"], row["outcome"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
