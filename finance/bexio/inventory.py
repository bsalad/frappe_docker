"""Read-only inventory of a bexio account, printed as Markdown.

For each entity it records the record count, the date range, the counts of
one grouping field (type or status) and one sample record with personal
data masked. Chart of accounts, account groups and VAT rates are printed in
full, because the Swiss setup needs them and they hold no personal data.

Uses client.py, which only issues GET requests. An entity the token has no
scope for (403) or the API does not know (404) is reported and skipped, so
one gap does not stop the run.

Run it as:
    varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/inventory.py > report.md

The output holds counts and masked samples, but it is still the account's
data: keep it out of the repository.
"""

import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client import BexioError, Client  # noqa: E402

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def offset(path, params=None):
    return ("offset", path, params)


def pages(path, size_param, rows_key=None):
    return ("pages", path, {"size_param": size_param, "rows_key": rows_key})


# One entry per entity. `endpoints` are tried in order; a 404 moves on to the
# next candidate, so the report says which path answered. `date_key` may be
# a tuple (the range covers all of them). `safe` lists string fields that are
# business data and may show in the masked sample; every other string is masked.
ENTITIES = [
    {"section": "Contacts", "label": "contacts", "endpoints": [offset("/2.0/contact")],
     "date_key": "updated_at", "group_by": "contact_type_id", "safe": ["contact_type_id"]},
    {"section": "Contacts", "label": "contact groups", "endpoints": [offset("/2.0/contact_group")],
     "safe": ["name"]},
    {"section": "Contacts", "label": "contact sectors",
     "endpoints": [offset("/2.0/contact_sector"), offset("/2.0/contact_sectors"), offset("/3.0/contact_sectors")],
     "safe": ["name"]},
    {"section": "Contacts", "label": "contact relations", "endpoints": [offset("/2.0/contact_relation")],
     "date_key": "updated_at"},
    {"section": "Articles", "label": "articles", "endpoints": [offset("/2.0/article")],
     "date_key": "updated_at", "safe": ["intern_code", "intern_name", "unit_id", "article_type_id"]},
    {"section": "Chart of accounts", "label": "accounts (chart of accounts)", "endpoints": [offset("/2.0/accounts")],
     "group_by": "account_type", "safe": ["name", "account_no", "account_type"]},
    {"section": "Chart of accounts", "label": "account groups", "endpoints": [offset("/2.0/account_groups")],
     "safe": ["name", "account_no"]},
    {"section": "Taxes", "label": "taxes (all VAT codes, incl. historic rates)", "endpoints": [offset("/3.0/taxes")],
     "date_key": ("start_year", "end_year"), "group_by": "type", "safe": ["name", "code", "display_name", "type"]},
    {"section": "Currencies", "label": "currencies", "endpoints": [offset("/3.0/currencies")], "safe": ["name", "id"]},
    {"section": "Fiscal years", "label": "business years", "endpoints": [offset("/3.0/accounting/business_years")],
     "date_key": ("start", "end"), "group_by": "status", "safe": ["status"]},
    {"section": "Journal", "label": "manual entries", "endpoints": [offset("/3.0/accounting/manual_entries")],
     "date_key": "date", "group_by": "type", "safe": ["type", "booking_type"]},
    {"section": "Journal", "label": "journal lines", "endpoints": [offset("/3.0/accounting/journal")],
     "date_key": "date"},
    {"section": "Sales", "label": "invoices (kb_invoice)", "endpoints": [offset("/2.0/kb_invoice")],
     "date_key": "is_valid_from", "group_by": "kb_item_status_id", "safe": ["document_nr", "kb_item_status_id"]},
    {"section": "Sales", "label": "credit notes", "endpoints": [
        offset("/2.0/kb_credit_voucher"), pages("/4.0/sales/credit-notes", "page_size"),
        pages("/4.0/sales/credit_notes", "page_size")],
     "safe": ["document_nr"]},
    {"section": "Sales", "label": "orders (kb_order)", "endpoints": [offset("/2.0/kb_order")],
     "date_key": "is_valid_from", "group_by": "kb_item_status_id", "safe": ["document_nr", "kb_item_status_id"]},
    {"section": "Sales", "label": "offers (kb_offer)", "endpoints": [offset("/2.0/kb_offer")],
     "date_key": "is_valid_from", "group_by": "kb_item_status_id", "safe": ["document_nr", "kb_item_status_id"]},
    {"section": "Purchase", "label": "bills (purchase)", "endpoints": [pages("/4.0/purchase/bills", "page_size", "data")],
     "date_key": "bill_date", "group_by": "status", "safe": ["document_no", "status", "reference_type"]},
    {"section": "Purchase", "label": "expenses", "endpoints": [pages("/4.0/expenses", "page_size", "data")],
     "date_key": "paid_on", "group_by": "status", "safe": ["document_no", "status"]},
    {"section": "Payments", "label": "payments (incoming and outgoing)",
     "endpoints": [pages("/4.0/banking/payments", "per-page", "results")],
     "date_key": "execution_date", "group_by": "type", "safe": ["type", "status", "currency", "document_no"]},
    {"section": "Banking", "label": "bank accounts", "endpoints": [offset("/3.0/banking/accounts")],
     "group_by": "type", "safe": ["type", "currency_id", "name"]},
    {"section": "Banking", "label": "banking transactions", "endpoints": [offset("/3.0/banking/transactions")],
     "date_key": "book_date", "group_by": "type", "safe": ["type", "status", "currency_id"]},
    {"section": "Projects", "label": "projects", "endpoints": [offset("/3.0/projects"), offset("/2.0/pr_project")],
     "safe": ["status"]},
    {"section": "Projects", "label": "timesheets", "endpoints": [offset("/2.0/timesheet")], "safe": []},
    {"section": "Files", "label": "files and attachments", "endpoints": [offset("/3.0/files")],
     "date_key": "created_at", "group_by": "processing_status", "sum_key": "size_in_bytes",
     "safe": ["extension", "mime_type", "processing_status", "source_type"]},
]


def fetch(client, spec):
    """Return (rows, path) from the first candidate endpoint that answers."""
    last = None
    for kind, path, extra in spec["endpoints"]:
        try:
            if kind == "offset":
                rows = list(client.paginate(path, extra))
            else:
                rows = list(client.paginate_pages(path, extra["size_param"], extra.get("rows_key")))
            return rows, path
        except BexioError as err:
            last = err
            if err.status != 404:
                raise
    raise last


def date_range(rows, keys):
    if isinstance(keys, str):
        keys = (keys,)
    values = []
    for row in rows:
        for key in keys:
            value = row.get(key)
            if isinstance(value, str) and DATE_RE.match(value):
                values.append(value[:10])
    if not values:
        return None
    return min(values), max(values)


def mask(value, key, safe):
    """Mask personal data in a sample: keep structure, ids, numbers, dates, currency codes and `safe` text."""
    if isinstance(value, dict):
        return {k: mask(v, k, safe) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v, key, safe) for v in value]
    if isinstance(value, str):
        if value == "" or DATE_RE.match(value) or ("currency" in (key or "") and CURRENCY_RE.match(value)):
            return value
        if key in safe or key in ("uuid", "url"):
            return value if key != "url" else "<url>"
        return "<masked len={}>".format(len(value))
    return value


def sample_of(rows, spec):
    if not rows:
        return None
    return mask(rows[0], None, set(spec.get("safe", [])))


def md_value(value):
    if value is None:
        return "-"
    return str(value).replace("|", "/")


def section_entity(client, spec, out):
    try:
        rows, path = fetch(client, spec)
    except BexioError as err:
        status = {403: "no access (token scope)", 404: "not found on any probed path"}.get(
            err.status, "error HTTP {}".format(err.status))
        out.append({"section": spec["section"], "label": spec["label"], "status": status,
                    "count": "-", "range": "-", "groups": "-", "path": "-", "sample": None})
        return
    dates = date_range(rows, spec["date_key"]) if spec.get("date_key") else None
    groups = None
    if spec.get("group_by"):
        counts = {}
        for row in rows:
            key = row.get(spec["group_by"])
            counts[key] = counts.get(key, 0) + 1
        groups = ", ".join("{}: {}".format(k, v) for k, v in sorted(counts.items(), key=lambda kv: str(kv[0])))
    count = str(len(rows))
    if spec.get("sum_key"):
        total = sum(row.get(spec["sum_key"]) or 0 for row in rows)
        count += " ({:.1f} MB)".format(total / 1048576.0)
    out.append({"section": spec["section"], "label": spec["label"], "status": "ok", "count": count,
                "range": "{} to {}".format(*dates) if dates else "-", "groups": groups or "-",
                "path": path, "sample": sample_of(rows, spec)})


def chart_of_accounts(client):
    rows = list(client.paginate("/2.0/accounts"))
    rows.sort(key=lambda r: str(r.get("account_no")))
    lines = ["| account_no | name | account_type | tax_id | active |", "|---|---|---|---|---|"]
    for r in rows:
        lines.append("| {} | {} | {} | {} | {} |".format(
            md_value(r.get("account_no")), md_value(r.get("name")), md_value(r.get("account_type")),
            md_value(r.get("tax_id")), md_value(r.get("is_active"))))
    return lines, len(rows)


def vat_codes(client):
    rows = list(client.paginate("/3.0/taxes"))
    rows.sort(key=lambda r: (str(r.get("type")), str(r.get("code"))))
    lines = ["| id | code | name | value | net_tax_value | type | start | end | active | account_id |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        start = "{}-{:02d}".format(r.get("start_year"), r.get("start_month") or 1) if r.get("start_year") else "-"
        end = "{}-{:02d}".format(r.get("end_year"), r.get("end_month") or 12) if r.get("end_year") else "-"
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            md_value(r.get("id")), md_value(r.get("code")), md_value(r.get("name")), md_value(r.get("value")),
            md_value(r.get("net_tax_value")), md_value(r.get("type")), start, end,
            md_value(r.get("is_active")), md_value(r.get("account_id"))))
    return lines, len(rows)


def main():
    client = Client()
    today = datetime.date.today().isoformat()
    results = []
    for spec in ENTITIES:
        section_entity(client, spec, results)

    print("# bexio inventory (read-only), run {}".format(today))
    print()
    print("| section | entity | status | count | date range | groups | endpoint | sample (masked) |")
    print("|---|---|---|---|---|---|---|---|")
    for r in results:
        sample = "yes" if r["sample"] else "-"
        print("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            r["section"], r["label"], r["status"], r["count"], r["range"], md_value(r["groups"]),
            r["path"], sample))
    print()
    print("## Masked samples")
    for r in results:
        if r["sample"]:
            print()
            print("### {}".format(r["label"]))
            print("```json")
            print(json.dumps(r["sample"], indent=2, ensure_ascii=False, sort_keys=True))
            print("```")

    lines, n = chart_of_accounts(client)
    print()
    print("## Chart of accounts ({} accounts)".format(n))
    print("\n".join(lines))

    lines, n = vat_codes(client)
    print()
    print("## VAT codes ({} rows, incl. historic)".format(n))
    print("\n".join(lines))

    print()
    print("Requests issued: {}".format(client.requests))


if __name__ == "__main__":
    main()
