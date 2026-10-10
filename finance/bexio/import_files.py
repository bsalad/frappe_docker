"""Attach the bexio files (the PDFs and receipts) to the ERPNext documents they belong to.

Step four of the bexio pipeline, after import_purchase.py has the bills and expenses as
Purchase Invoices. A bill or an expense lists its files by bexio id (attachment_ids). Each
file becomes a private ERPNext File on the Purchase Invoice with the same bexio_id, so the
books keep their vouchers (GeBüV, see finance/docs/archiving.md). The export holds the
metadata in files.json and the content in files/<id><ext>.

--dry-run (the default) reads ERPNext and writes nothing. --apply uploads each file that
would be attached, through the API user (upload_file, then the bexio id as its File's bexio_id),
and writes the bexio_id on each attached File that lacks one (the backfill):

    python3 finance/bexio/import_files.py [--dry-run] [--export DIR]
    python3 finance/bexio/import_files.py --apply [--export DIR]

--export defaults to the newest directory under <private>/bexio-export/. A file is found by its uuid:
files.json (the /3.0/files list) and bill_attachments.json (the attachments of the bills, export.py)
both hold the uuid that a bill lists in attachment_ids. The dry run prints
totals per doctype only. The bexio ids of the files it cannot place, and the names of the Files
the backfill cannot match, go to <private>/bexio-files-dry-run.txt, never to the screen or the repository.

Idempotent: a file is already attached when its Purchase Invoice has a File with its bexio_id, or,
for a File without one, with the same name and size. The live run skips those. A file that no
record lists (outcome archive) is uploaded into the File folder "bexio Archive" under Home, on no
document, keyed by its bexio id; a second run finds it there and skips it. A file that two documents
list is listed, not attached, since a File has one document.

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
# a file no record lists (file_links has no document for it) goes into this File folder, not onto a document
ARCHIVE_FOLDER_NAME = "bexio Archive"
ARCHIVE_FOLDER = "Home/" + ARCHIVE_FOLDER_NAME

ATTACH, ATTACHED, REENCODED = "attach", "attached", "re-encoded"
ARCHIVE, ARCHIVED = "archive", "archived"
PROBLEMS = ("unlinked", "shared", "no metadata", "no document", "no content", "size differs")
IMAGES = (".jpg", ".jpeg", ".png", ".gif", ".webp")


class Lookups:
    """What the plan reads from ERPNext: the Purchase Invoices by bexio_id, the Files already on them, and the
    bexio ids of the Files already in the archive folder."""

    def __init__(self, documents, attached, archived=frozenset()):
        self.documents = documents  # bexio id -> Purchase Invoice name
        self.attached = attached    # Purchase Invoice name -> [File rows]
        self.archived = archived    # bexio ids of the Files in ARCHIVE_FOLDER

    @classmethod
    def from_erp(cls, erp):
        documents = {r["bexio_id"]: r["name"] for r in erp.list(DOCTYPE, [["bexio_id", "is", "set"]], ["name", "bexio_id"])}
        attached = {}
        for f in erp.list("File", [["attached_to_doctype", "=", DOCTYPE]],
                          ["name", "attached_to_name", "file_name", "file_size", "bexio_id"]):
            attached.setdefault(f["attached_to_name"], []).append(f)
        archived = {f["bexio_id"] for f in erp.list("File", [["folder", "=", ARCHIVE_FOLDER], ["bexio_id", "is", "set"]], ["bexio_id"])}
        return cls(documents, attached, archived)


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
    # a File keyed by a bexio id is that file and no other, even when another file has its name and size;
    # the name and size are compared only for a File with no bexio_id (attached before the key was written)
    return any(
        f.get("bexio_id") == file_id
        # a bill attachment whose download failed has no name or size: it matches nothing by them
        or (not f.get("bexio_id") and meta.get("name") and f.get("file_name") == meta["name"]
            and f.get("file_size") == meta.get("size_in_bytes"))
        for f in files
    )


def same_file(meta, f):
    # the planned file a File without a bexio_id is: the same name, and the same size or an image Frappe re-encoded
    if not meta or not meta.get("name") or f.get("file_name") != meta["name"]:
        return False
    return f.get("file_size") == meta.get("size_in_bytes") or meta["name"].lower().endswith(IMAGES)


def backfill_plan(rows, files, lookups):
    """The attached Files that lack a bexio_id, and the planned file each one is.

    Returns (writes, listed): writes are (File name, bexio id) pairs, one per File that matches exactly one planned
    file of its Purchase Invoice; listed are the file names of the Files that match none or several, left alone.
    A planned file is claimed by one File only, since a bexio id is unique: two Files that both match it are listed.
    """
    keyed = {f["bexio_id"] for group in lookups.attached.values() for f in group if f.get("bexio_id")}
    matches = []  # (File row, the bexio ids of the planned files it matches)
    for document, group in lookups.attached.items():
        planned = [r["file"] for r in rows if r["document"] == document and r["file"] not in keyed]
        for f in group:
            if not f.get("bexio_id"):
                matches.append((f, [file_id for file_id in planned if same_file(files.get(file_id), f)]))
    claims = {}
    for _, ids in matches:
        for file_id in ids:
            claims[file_id] = claims.get(file_id, 0) + 1
    writes, listed = [], []
    for f, ids in matches:
        if len(ids) == 1 and claims[ids[0]] == 1:
            writes.append((f["name"], ids[0]))
        else:
            listed.append(f["file_name"])
    return writes, listed


def reencoded(meta, files):
    # Frappe strips the EXIF data of an uploaded image, so the File's bytes and size differ from the export's
    # while its name stays: the same picture, not a missing one. Only images, so a PDF of another size still differs.
    name = meta.get("name")
    return bool(name) and name.lower().endswith(IMAGES) and any(f.get("file_name") == name for f in files)


def classify(file_id, meta, holders, lookups, export_dir):
    """What the live run does with one file: its outcome, and the Purchase Invoice it goes to."""
    if not holders:
        # no record lists it: it goes to the archive folder, with the same content checks as an attachment
        if meta is None:
            return "unlinked", None
        if file_id in lookups.archived:
            return ARCHIVED, None
        path = content_path(export_dir, meta)
        if not os.path.isfile(path):
            return "no content", None
        if os.path.getsize(path) != meta.get("size_in_bytes"):
            return "size differs", None
        return ARCHIVE, None
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
    """Plan every file of the export against ERPNext; returns the rows, whether the export has files.json, and the backfill plan."""
    bills, expenses = ip.load_records(export_dir)
    files = load_files(export_dir) or {}
    lookups = Lookups.from_erp(erp)
    rows = plan(files, attachment_owners(bills, expenses), lookups, export_dir)
    return rows, os.path.exists(os.path.join(export_dir, FILES_FILE)), backfill_plan(rows, files, lookups)


def report(rows, export_dir, has_files, applied=False, backfills=None):
    """Totals only: no file ids, no names. backfills is the (writes, listed) pair of backfill_plan."""
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
    for outcome in (ATTACH, ATTACHED, REENCODED, ARCHIVE, ARCHIVED) + PROBLEMS:
        if counts.get(outcome):
            lines.append("  {:<14}{:>6}".format(outcome, counts[outcome]))
    if backfills is not None:
        writes, listed = backfills
        lines.append("  {:<14}{:>6}".format("to backfill", len(writes)))
        if listed:
            lines.append("  {:<14}{:>6}".format("unmatched", len(listed)))
    lines.append("applied: the files marked attach or archive are uploaded and the Files to backfill get their bexio_id" if applied
                 else "dry run: nothing was written")
    return "\n".join(lines)


def _bytes(rows, has_files):
    return sum(r["size"] or 0 for r in rows) if has_files else "-"


def _header_safe(name):
    # a quote, CR or LF in a name would end the filename parameter or the header line
    return "".join("_" if c in '"\r\n' else c for c in name)


def _multipart(erp, fields, file_name, content):
    """The multipart request for Frappe's upload_file with these form fields and the file. Built here, sent by upload() in the live run only."""
    boundary = uuid.uuid4().hex
    sep = "--{}\r\n".format(boundary)
    body = b""
    for key, value in fields:
        body += (sep + 'Content-Disposition: form-data; name="{}"\r\n\r\n{}\r\n'.format(key, value)).encode("utf-8")
    body += (sep + 'Content-Disposition: form-data; name="file"; filename="{}"\r\n'
             'Content-Type: application/octet-stream\r\n\r\n'.format(_header_safe(file_name))).encode("utf-8")
    body += content + ("\r\n--{}--\r\n".format(boundary)).encode("ascii")
    # Erp's base URL already ends in /api (its own paths are /resource/... and /method/...)
    req = urllib.request.Request(erp._url + "/method/upload_file", data=body, method="POST")
    req.add_header("Authorization", erp._auth)  # the Erp object keeps its secret; it is only read here
    req.add_header("Content-Type", "multipart/form-data; boundary=" + boundary)
    return req


def upload_request(erp, document, file_name, content):
    """The multipart request for one private File on the Purchase Invoice."""
    return _multipart(erp, (("doctype", DOCTYPE), ("docname", document), ("is_private", "1")), file_name, content)


def archive_request(erp, file_name, content):
    """The multipart request for one private File in the archive folder, on no document."""
    return _multipart(erp, (("folder", ARCHIVE_FOLDER), ("is_private", "1")), file_name, content)


def upload(erp, document, file_id, file_name, content):
    """Upload one file and mark it with its bexio id, so a second run skips it. The live run calls this; the dry run never does."""
    try:
        with urllib.request.urlopen(upload_request(erp, document, file_name, content), timeout=300) as resp:
            name = json.loads(resp.read().decode("utf-8"))["message"]["name"]
    except urllib.error.HTTPError as err:
        raise im.ErpError(err.code, "upload_file for {}".format(document) + im._reason(err)) from None
    erp.update("File", name, {"bexio_id": file_id})
    return name


def upload_archive(erp, file_id, file_name, content):
    """Upload one unlinked file into the archive folder, marked with its bexio id."""
    try:
        with urllib.request.urlopen(archive_request(erp, file_name, content), timeout=300) as resp:
            name = json.loads(resp.read().decode("utf-8"))["message"]["name"]
    except urllib.error.HTTPError as err:
        raise im.ErpError(err.code, "upload_file into {}".format(ARCHIVE_FOLDER) + im._reason(err)) from None
    erp.update("File", name, {"bexio_id": file_id})
    return name


def ensure_archive_folder(erp):
    """Create the archive folder under Home, once: a File folder is named by its path, so the uploads find it by ARCHIVE_FOLDER."""
    if not erp.list("File", [["name", "=", ARCHIVE_FOLDER]], ["name"]):
        erp.insert("File", {"file_name": ARCHIVE_FOLDER_NAME, "is_folder": 1, "folder": "Home"})


def apply(rows, export_dir, erp):
    """Upload every file the plan marks to attach, and every unlinked one to the archive folder. Returns how many were uploaded."""
    files = load_files(export_dir)
    uploaded = 0
    if any(row["outcome"] == ARCHIVE for row in rows):
        ensure_archive_folder(erp)
    for row in rows:
        if row["outcome"] == ATTACH:
            meta = files[row["file"]]
            with open(content_path(export_dir, meta), "rb") as f:
                upload(erp, row["document"], row["file"], meta["name"], f.read())
        elif row["outcome"] == ARCHIVE:
            meta = files[row["file"]]
            with open(content_path(export_dir, meta), "rb") as f:
                upload_archive(erp, row["file"], meta["name"], f.read())
        else:
            continue
        uploaded += 1
    return uploaded


def backfill(writes, erp):
    """Write the bexio_id on each attached File that backfill_plan matched. Returns how many were written."""
    for name, file_id in writes:
        erp.update("File", name, {"bexio_id": file_id})
    return len(writes)


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
        rows, has_files, backfills = run(export_dir, erp)
        if args.apply:
            print("uploaded {} file(s)".format(apply(rows, export_dir, erp)))
            print("backfilled {} File(s)".format(backfill(backfills[0], erp)))
            rows, has_files, backfills = run(export_dir, erp)
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    print(report(rows, export_dir, has_files, applied=args.apply, backfills=backfills))
    problems = [r for r in rows if r["outcome"] in PROBLEMS]
    listed = [r for r in rows if r["outcome"] in PROBLEMS or r["outcome"] == REENCODED]
    unmatched = backfills[1]
    if listed or unmatched:
        lines = [problem_line(r) for r in listed] + ["File {}: no single planned file to backfill from".format(name) for name in unmatched]
        ip.write_private(os.path.join(im.PRIVATE, PROBLEMS_FILE), lines)
        print("{} line(s) listed in {}".format(len(lines), os.path.join(im.PRIVATE, PROBLEMS_FILE)), file=sys.stderr)
    return 1 if problems or unmatched else 0


def problem_line(row):
    if row["outcome"] == REENCODED:
        return "file {}: attached, re-encoded by Frappe (size differs)".format(row["file"])
    return "file {}: {}".format(row["file"], row["outcome"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
