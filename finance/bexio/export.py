"""Export the bexio data to private JSON files: master data, accounting history, documents.

Step one of the pipelines: import_master.py reads the master-data files and puts
them into ERPNext; the posting plan reads the journal and the documents. GET only
(client.py has no way to write). Nothing is masked: the files are the private copy
of the account's data, so they go to /Users/bsaladin/ws_yardr_finance/private/
(directory mode 700, files 600) and never into the repository.

Run it as:
    varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/export.py [--out DIR] [--only ENTITY ...]

--out defaults to <private>/bexio-export/<YYYY-MM-DD>/. Besides the entity
files the directory gets manifest.json: per entity the file, the record count
and the status. An entity the token has no scope for (403) or the API does not
know (404) is recorded in the manifest and skipped, so one gap does not stop
the run; the exit status is 2 when a required entity is missing.

--only reruns the named entities (repeatable) and keeps the manifest entries of
the others; without it every entity is exported.

Four entities need the accounting and file scopes, which bexio grants only with
write access. With BEXIO_BROKER=1 they are read through the Varlock broker, which
holds the token and allows GET only; no varlock run and no login is needed then.
Without the broker they are read with the export login (oauth.py login --export-scope,
its own keychain item), and everything else with the read-only login. Once the
run is done, `oauth.py logout --export-scope` removes the export login again.

Documents come with their positions: invoices, orders, offers and purchase bills
are read as a list, then one call per record adds the positions to it. The
payments of each invoice go to invoice_payments.json. The content of each file
goes to files/<id>.<extension>; files.json holds the metadata of all of them.

bill_attachments.json holds the attachments of the bills, one row per file uuid (the
bills list the uuids in attachment_ids; their content is on /3.0/files/<uuid>/download).
Their content goes to files/<uuid>.pdf, the same folder as files.json's.

--payroll exports the payroll module instead (employees, absences per year, payslip PDFs
per month, the paystub overview and the company reads) to <private>/bexio-payroll/, with its
own manifest. It is not part of the default run, and it reads with the read-only login, which
needs the payroll scopes (oauth.SCOPE) consented once.
"""

import argparse
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client import BexioError, Client  # noqa: E402

PRIVATE = "/Users/bsaladin/ws_yardr_finance/private"

FILES = "files"
BILL_ATTACHMENTS = "bill_attachments"

# A file's content, tried in order: the first path that answers wins. Not yet
# checked against the live API; a 404 on both is recorded per file in the manifest.
FILE_CONTENT_PATHS = ("/3.0/files/{id}/download", "/3.0/files/{id}/content")

# A bill attachment is named by its file uuid, and its content is on the same
# download path (probed through the broker: 200 for the uuid, 415 for /3.0/files/{uuid}).
ATTACHMENT_CONTENT_PATHS = ("/3.0/files/{id}/download",)

# The entities whose content is downloaded into files/, with the paths to try.
CONTENT_PATHS = {FILES: FILE_CONTENT_PATHS, BILL_ATTACHMENTS: ATTACHMENT_CONTENT_PATHS}


def offset(path):
    return ("offset", path, None)


def pages(path, size_param, rows_key):
    return ("pages", path, (size_param, rows_key))


def detailed(listing, template):
    """The records of `listing`, each merged with the answer of GET `template` (positions)."""
    return ("detailed", listing, template)


def attached(listing, template):
    """One item per record of `listing`: {"parent_id", "rows"} with the answer of GET `template`."""
    return ("attached", listing, template)


def attachments(listing, template):
    """One row per attachment of the records of `listing`, from the answer of GET `template` (attachment_ids)."""
    return ("attachments", listing, template)


# file name -> (how to read it, required). Required entities are what the
# import cannot do without; the lookups only translate ids into names.
ENTITIES = {
    "contacts": (offset("/2.0/contact"), True),
    "contact_groups": (offset("/2.0/contact_group"), True),
    "contact_relations": (offset("/2.0/contact_relation"), True),
    "articles": (offset("/2.0/article"), True),
    "accounts": (offset("/2.0/accounts"), True),
    "account_groups": (offset("/2.0/account_groups"), False),
    "currencies": (offset("/3.0/currencies"), True),
    "bank_accounts": (offset("/3.0/banking/accounts"), True),
    "business_years": (offset("/3.0/accounting/business_years"), True),
    "manual_entries": (offset("/3.0/accounting/manual_entries"), True),
    "journal": (offset("/3.0/accounting/journal"), True),
    "invoices": (detailed(offset("/2.0/kb_invoice"), "/2.0/kb_invoice/{id}"), True),
    "invoice_payments": (attached(offset("/2.0/kb_invoice"), "/2.0/kb_invoice/{id}/payment"), True),
    "credit_vouchers": (offset("/2.0/kb_credit_voucher"), False),
    "orders": (detailed(offset("/2.0/kb_order"), "/2.0/kb_order/{id}"), False),
    "offers": (detailed(offset("/2.0/kb_offer"), "/2.0/kb_offer/{id}"), False),
    "bills": (detailed(pages("/4.0/purchase/bills", "page_size", "data"), "/4.0/purchase/bills/{id}"), True),
    "expenses": (pages("/4.0/expenses", "page_size", "data"), False),
    "payments": (pages("/4.0/banking/payments", "per-page", "results"), False),
    "bank_transactions": (offset("/3.0/banking/transactions"), True),
    FILES: (offset("/3.0/files"), False),
    BILL_ATTACHMENTS: (attachments(pages("/4.0/purchase/bills", "page_size", "data"), "/4.0/purchase/bills/{id}"), False),
    "countries": (offset("/2.0/country"), False),
    "units": (offset("/2.0/unit"), False),
    "salutations": (offset("/2.0/salutation"), False),
    "languages": (offset("/2.0/language"), False),
}


# The entities read with the export login; every other one uses the read-only login.
# bill_attachments needs the file scope for its content, as files does.
EXPORT_SCOPE_ENTITIES = frozenset({"manual_entries", "journal", "bank_transactions", FILES, BILL_ATTACHMENTS})


def default_out(today=None):
    today = today or datetime.date.today()
    return os.path.join(PRIVATE, "bexio-export", today.isoformat())


def get_record(client, template, record_id):
    """GET one record by its id. Some answers come wrapped in {"data": {...}}; unwrapped here."""
    body = client.get(template.format(id=record_id))
    if isinstance(body, dict) and set(body) == {"data"} and isinstance(body["data"], dict):
        return body["data"]
    return body


def read_entity(client, spec):
    kind = spec[0]
    if kind == "offset":
        return list(client.paginate(spec[1]))
    if kind == "pages":
        size_param, rows_key = spec[2]
        return list(client.paginate_pages(spec[1], size_param, rows_key))
    if kind == "detailed":
        rows = []
        for row in read_entity(client, spec[1]):
            merged = dict(row)
            merged.update(get_record(client, spec[2], row["id"]))
            rows.append(merged)
        return rows
    if kind == "attached":
        return [{"parent_id": row["id"], "rows": get_record(client, spec[2], row["id"])}
                for row in read_entity(client, spec[1])]
    if kind == "attachments":
        rows = []
        for bill in read_entity(client, spec[1]):
            rows.extend(attachment_rows(get_record(client, spec[2], bill["id"])))
        return rows
    raise ValueError("unknown entity kind {!r}".format(kind))


def attachment_rows(bill):
    """One row per attachment of a bill: the file's uuid as id, with the bill's document_no and bill_date.

    The bill carries no file name or extension: download_files takes both from the content.
    """
    return [{"id": uuid, "uuid": uuid, "bill_id": bill["id"], "document_no": bill.get("document_no"),
             "bill_date": bill.get("bill_date")}
            for uuid in bill.get("attachment_ids") or []]


# The first bytes of the content types bexio attachments come in; anything else is "bin".
SNIFFED = ((b"%PDF-", "pdf"), (b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"))


def sniff_extension(content):
    for magic, extension in SNIFFED:
        if content.startswith(magic):
            return extension
    return "bin"


def _open_private(path):
    """Open for writing with mode 600, whatever the umask."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


def write_private(path, data):
    with _open_private(path) as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")


def write_private_bytes(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def file_name(row):
    """<id>.<extension> for a file's metadata row; the extension only when it is a plain word."""
    extension = re.sub(r"[^A-Za-z0-9]", "", str(row.get("extension") or ""))[:10]
    return "{}.{}".format(row["id"], extension) if extension else str(row["id"])


def download_files(client, rows, out, templates=FILE_CONTENT_PATHS):
    """Write each file's content to out/files/; returns the manifest summary of the downloads.

    A row without a size_in_bytes, extension or name (a bill attachment) gets them from its content, so
    the JSON written after the download carries them.
    """
    folder = os.path.join(out, "files")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    summary = {"downloaded": 0, "bytes": 0, "failed": []}
    for row in rows:
        content = None
        for template in templates:
            try:
                content = client.get(template.format(id=row["id"]), raw=True)
                break
            except BexioError:
                continue
        if content is None:
            summary["failed"].append(row["id"])
            continue
        row.setdefault("size_in_bytes", len(content))
        row.setdefault("extension", sniff_extension(content))
        row.setdefault("name", "{}.{}".format(row["id"], row["extension"]))
        write_private_bytes(os.path.join(folder, file_name(row)), content)
        summary["downloaded"] += 1
        summary["bytes"] += len(content)
    return summary


def read_manifest(out):
    path = os.path.join(out, "manifest.json")
    if not os.path.exists(path):
        return {"entities": {}}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def export(client, out, entities=None, only=None, export_client=None):
    """Write the entities (all, or just `only`) to `out` and return the manifest (also written there).

    The entities of EXPORT_SCOPE_ENTITIES go through export_client, the others through client.
    """
    entities = entities or ENTITIES
    os.makedirs(out, mode=0o700, exist_ok=True)
    os.chmod(out, 0o700)
    manifest = read_manifest(out) if only else {"entities": {}}
    manifest["exported_on"] = datetime.date.today().isoformat()
    for name in (only or entities):
        spec, required = entities[name]
        source = export_client if name in EXPORT_SCOPE_ENTITIES and export_client is not None else client
        entry = {"file": name + ".json", "required": required}
        try:
            rows = read_entity(source, spec)
        except BexioError as err:
            # A stale file from an earlier run must not pass for this run's data.
            stale = os.path.join(out, entry["file"])
            if os.path.exists(stale):
                os.remove(stale)
            entry.update(status="error", error="HTTP {} on {}".format(err.status, err.path), count=0)
        else:
            # the content first: a download adds the size of each row, which the JSON must carry
            if name in CONTENT_PATHS:
                entry.update(download_files(source, rows, out, CONTENT_PATHS[name]))
            write_private(os.path.join(out, entry["file"]), rows)
            entry.update(status="ok", count=len(rows))
        manifest["entities"][name] = entry
    write_private(os.path.join(out, "manifest.json"), manifest)
    return manifest


# The payroll export (--payroll), for the move of payroll into ERPNext HRMS. Names, AHV numbers,
# salaries, absences and payslips: it goes to its own folder, apart from the export above, and is
# read with the read-only login, so its scopes must be in that login's consent (oauth.SCOPE).
# The paths are from docs.bexio.com (read 2026-10-10), base 4.0. Not yet checked against the live API:
# the year parameter of the absences and the shape of the paystub overview. A 403 means a scope is
# missing, a 404 on a payslip means none for that month.
PAYROLL = "/Users/bsaladin/ws_yardr_finance/private/bexio-payroll"
PAYSTUBS = "paystubs"
PAYROLL_EMPLOYEES = "/4.0/payroll/employees"
PAYROLL_ABSENCES = "/4.0/payroll/employees/{id}/absences"
# The download replaces the deprecated /paystubs/{year}/{month}/pdf (deprecated 2026-05-26).
PAYROLL_PAYSTUB = "/4.0/payroll/employees/{id}/paystub-pdf-download/{year}/{month}"
PAYROLL_PAYSTUBS_OVERVIEW = "/4.0/payroll/paystubs/overview"
# Company-level reads for the HRMS study: each is kept as it comes, under its path.
PAYROLL_COMPANY = (
    "/4.0/payroll/companies/elm-status",
    "/4.0/payroll/companies/qst-status",
    "/4.0/payroll/companies/statistic-enabled",
    "/4.0/payroll/accounting/last-transfer",
    "/4.0/payroll/payments/last-transfer",
)


def payroll_rows(body, path):
    """The records of a payroll answer: a bare list, or the list under "data"."""
    rows = body.get("data") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        raise BexioError("unexpected body", path)
    return rows


def payroll_absences(client, employees, years):
    """The absences of each employee for each year, one row each with the employee and year added."""
    rows = []
    for employee in employees:
        path = PAYROLL_ABSENCES.format(id=employee["id"])
        for year in sorted(years):
            for absence in payroll_rows(client.get(path, {"year": year}), path):
                rows.append(dict(absence, employee_id=employee["id"], year=year))
    return rows


def payroll_paystubs(client, out, employees, years, today):
    """One PDF per employee and month up to today, in paystubs/; returns the index of them (no names).

    A 404 is a month without a payslip. Any other refusal raises, so the entity is recorded as failed.
    """
    index = []
    for employee in employees:
        for year in sorted(years):
            for month in range(1, 13):
                if (year, month) > (today.year, today.month):
                    continue
                path = PAYROLL_PAYSTUB.format(id=employee["id"], year=year, month=month)
                try:
                    content = client.get(path, raw=True)
                except BexioError as err:
                    if err.status == 404:
                        continue
                    raise
                name = "{}-{}-{:02d}.pdf".format(employee["id"], year, month)
                write_private_bytes(os.path.join(out, PAYSTUBS, name), content)
                index.append({"employee_id": employee["id"], "year": year, "month": month,
                              "file": name, "bytes": len(content)})
    return index


def payroll_company(client):
    """The company-level reads, one row each: {"path", "body"}, or {"path", "error"} for a read that was refused."""
    rows = []
    for path in PAYROLL_COMPANY:
        try:
            rows.append({"path": path, "body": client.get(path)})
        except BexioError as err:
            rows.append({"path": path, "error": "HTTP {}".format(err.status)})
    return rows


def by_month(index):
    """The number of payslips per month, as "YYYY-MM": counts only."""
    counts = {}
    for row in index:
        key = "{}-{:02d}".format(row["year"], row["month"])
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _payroll_entity(out, manifest, name, required, produce):
    """Run produce() for one payroll entity, write its rows to name.json and record it in the manifest."""
    entry = {"file": name + ".json", "required": required}
    manifest["entities"][name] = entry
    try:
        rows = produce()
    except BexioError as err:
        # A stale file from an earlier run must not pass for this run's data.
        stale = os.path.join(out, entry["file"])
        if os.path.exists(stale):
            os.remove(stale)
        entry.update(status="error", error="HTTP {} on {}".format(err.status, err.path), count=0)
        return []
    write_private(os.path.join(out, entry["file"]), rows)
    entry.update(status="ok", count=len(rows))
    return rows


def export_payroll(client, out, years, today=None):
    """Write the payroll to `out` and return the manifest (also written there). Idempotent: a rerun rewrites the same files.

    Without the employees (the first entity) nothing else can be asked, so the run stops there.
    """
    today = today or datetime.date.today()
    os.makedirs(os.path.join(out, PAYSTUBS), mode=0o700, exist_ok=True)
    os.chmod(out, 0o700)
    os.chmod(os.path.join(out, PAYSTUBS), 0o700)
    manifest = {"exported_on": today.isoformat(), "years": sorted(years), "entities": {}}
    employees = _payroll_entity(out, manifest, "employees", True,
                                lambda: payroll_rows(client.get(PAYROLL_EMPLOYEES), PAYROLL_EMPLOYEES))
    if manifest["entities"]["employees"]["status"] == "ok":
        _payroll_entity(out, manifest, "company", False, lambda: payroll_company(client))
        _payroll_entity(out, manifest, "absences", False, lambda: payroll_absences(client, employees, years))
        _payroll_entity(out, manifest, "paystubs_overview", False,
                        lambda: payroll_rows(client.get(PAYROLL_PAYSTUBS_OVERVIEW), PAYROLL_PAYSTUBS_OVERVIEW))
        paystubs = _payroll_entity(out, manifest, "paystubs", False,
                                   lambda: payroll_paystubs(client, out, employees, years, today))
        if manifest["entities"]["paystubs"]["status"] == "ok":
            manifest["entities"]["paystubs"]["by_month"] = by_month(paystubs)
    write_private(os.path.join(out, "manifest.json"), manifest)
    return manifest


def failed_required(manifest):
    return [n for n, e in manifest["entities"].items() if e["required"] and e["status"] != "ok"]


def main(argv):
    parser = argparse.ArgumentParser(description="Export bexio data to private JSON files (read-only).")
    parser.add_argument("--out", default=None, help="target directory (default: <private>/bexio-export/<today>/)")
    parser.add_argument("--only", action="append", choices=sorted(ENTITIES), metavar="ENTITY",
                        help="export only this entity (repeatable); the manifest keeps the others")
    parser.add_argument("--payroll", action="store_true",
                        help="export the payroll instead, to <private>/bexio-payroll/ (employees, absences, payslips)")
    parser.add_argument("--year", action="append", type=int, metavar="YEAR",
                        help="payroll year for absences and payslips (repeatable; default: this year)")
    args = parser.parse_args(argv)
    if args.payroll:
        if args.only or args.out:
            parser.error("--payroll takes neither --only nor --out")
        manifest = export_payroll(Client(), PAYROLL, args.year or [datetime.date.today().year])
        print("exported payroll to {}".format(PAYROLL))
        for name, entry in manifest["entities"].items():
            if entry["status"] != "ok":
                print("  {:<18} {}".format(name, entry["error"]))
            else:
                print("  {:<18} {}".format(name, entry["count"]))
        missing = failed_required(manifest)
        if missing:
            print("missing required entities: {}".format(", ".join(missing)), file=sys.stderr)
            return 2
        return 0
    if args.year:
        parser.error("--year goes with --payroll")
    out = args.out or default_out()
    names = list(args.only or ENTITIES)
    # Each login is only opened when a requested entity needs it: the export login
    # stops the run before anything is written when it is not in the keychain.
    client = Client() if any(name not in EXPORT_SCOPE_ENTITIES for name in names) else None
    export_client = Client(export_scope=True) if any(name in EXPORT_SCOPE_ENTITIES for name in names) else None
    manifest = export(client, out, only=args.only, export_client=export_client)
    print("exported to {}".format(out))
    for name, entry in manifest["entities"].items():
        if entry["status"] != "ok":
            print("  {:<18} {}".format(name, entry["error"]))
            continue
        line = "  {:<18} {}".format(name, entry["count"])
        if name in CONTENT_PATHS:
            line += " (downloaded {}, {} bytes, failed {})".format(
                entry["downloaded"], entry["bytes"], len(entry["failed"]))
        print(line)
    missing = failed_required(manifest)
    if missing:
        print("missing required entities: {}".format(", ".join(missing)), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
