"""Map the exported bexio sales documents to ERPNext: invoices, credit notes, orders, offers.

Step three of the bexio pipeline, after import_master.py: the customers, items
and accounts it imported are looked up here by their bexio_id. Each function
takes one export record and the lookups and returns the ERPNext document as a
dict, not yet inserted or submitted. Whether these documents post GL, and how
payments reach them, is the posting plan of finance-3qsp; the live run is
erp-a2ma's. So nothing is written here: --dry-run reads ERPNext and reports
what a run would map.

A record that cannot be mapped (an unknown VAT code, account, contact, item or
position type, a credit note whose invoice is missing, a total that differs
from bexio's by more than 5 rappen) raises Unmapped, and the report names it by
bexio id only. Amounts are checked against bexio's own totals: the net, the
taxes per rate and the total. A difference is reported; the total is the one
exception, a difference of up to 5 rappen goes into the last tax row so that
the grand total is bexio's to the rappen.

The export directory holds the documents with their positions (the
single-document calls), next to the entity files of export.py:
invoices.json, orders.json, offers.json and credit_vouchers.json when bexio
has credit notes. Amounts are in the document currency; the CHF figures are
the document amounts times the exchange rate bexio gives for the document.

Run it as:

    python3 finance/bexio/import_sales.py --dry-run [--export DIR]

--export defaults to the newest directory under <private>/bexio-export/. The
output is totals only; the differences and unmapped records go to a file under
<private>, never to the repository. Standard library only, plus import_master.
"""

import argparse
import collections
import json
import os
import sys
from decimal import ROUND_HALF_UP, Decimal

import import_master as im

COMPANY = im.COMPANY
BASE_CURRENCY = "CHF"
# free-text positions and text lines: one service item, the description keeps the bexio text
GENERIC_ITEM = "bexio Position"
# the lookup value of an invoice that is in the export but not yet in ERPNext (dry run only)
PLANNED = "(planned)"
# the credit note's link to its invoice; the field name is taken from the bexio record and
# still needs confirming against the full export (see README)
ORIGINAL = "invoice_id"

# the documents of the export: file, ERPNext doctype, and whether a credit note
DOCUMENTS = (
    ("invoices", "Sales Invoice", False),
    ("credit_vouchers", "Sales Invoice", True),
    ("orders", "Sales Order", False),
    ("offers", "Quotation", False),
)

# bexio position type -> how it maps. A type not listed here is refused, not guessed.
POSITION_TYPES = {
    "KbPositionCustom": "free",        # free-text position with a price and an account
    "KbPositionArticle": "article",    # a position of an article: the Item by bexio_id
    "KbPositionText": "text",          # a text line without a price: a row of 0
    "KbPositionPagebreak": "skip",     # layout only
    "KbPositionSubtotal": "skip",      # a display row of the sum above: ERPNext totals the rows itself
    "KbPositionDiscount": "discount",  # a document discount: no amount in the export, see _document
}

CENT = Decimal("0.01")
ZERO = Decimal("0")
# the most a document's total may differ from bexio's; the difference goes into the last tax row
TOTAL_TOLERANCE = Decimal("0.05")


class Unmapped(Exception):
    """A record the importer does not map. The message names the reason, never a company name."""


def _dec(value):
    return Decimal(str(value))


def _cents(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _currency(record, lookups):
    """(currency code, rate to CHF) of a document. A foreign currency needs the rate bexio gives."""
    code = lookups["currency"].get(str(record.get("currency_id")))
    if code is None:
        raise Unmapped("currency {} is not in the export".format(record.get("currency_id")))
    if code == BASE_CURRENCY:
        return code, Decimal("1")
    if not record.get("exchange_rate"):
        raise Unmapped("no exchange rate for {}".format(code))
    return code, _dec(record["exchange_rate"])


def _tax(tax_id, lookups):
    """The ERPNext tax template whose bexio_id is this bexio tax id. Never chosen by rate."""
    template = lookups["tax"].get(str(tax_id))
    if template is None:
        raise Unmapped("bexio tax {} has no ERPNext template with that bexio_id".format(tax_id))
    if template["rate"] is None:
        raise Unmapped("bexio tax {}: its ERPNext template has no single rate row".format(tax_id))
    return template


def _rows(record, lookups):
    """The item rows in bexio's order, the amounts per bexio tax id, and whether the document has a discount row.

    A position's rate is bexio's unit price. A position discount goes to ERPNext's own discount fields
    (price list rate and discount percentage), with the rate worked out in cents the way ERPNext does it,
    so that the row amount is the one ERPNext computes. A line without a tax id has no tax: its amount is
    kept under the key None and gets no tax row.
    """
    rows, amounts, discount_row = [], {}, False
    for pos in record.get("positions") or []:
        kind = POSITION_TYPES.get(pos.get("type"))
        if kind is None:
            raise Unmapped("position type {} is not mapped".format(pos.get("type")))
        if kind == "skip":
            continue
        if kind == "discount":
            discount_row = True
            continue
        text = pos.get("text") or ""
        if kind == "text":
            rows.append({"item_code": GENERIC_ITEM, "description": text, "qty": 1.0, "rate": 0.0, "amount": 0.0})
            continue
        qty, price = _dec(pos["amount"]), _dec(pos["unit_price"])
        percent = _dec(pos.get("discount_in_percent") or 0)
        if kind == "article":
            item = lookups["item"].get(str(pos.get("article_id")))
            if item is None:
                raise Unmapped("article {} has no Item".format(pos.get("article_id")))
        else:
            item = GENERIC_ITEM
        account = lookups["account"].get(str(pos.get("account_id")))
        if account is None:
            raise Unmapped("account {} has no Account".format(pos.get("account_id")))
        unit_discount = _cents(price * percent / 100)
        rate = price - unit_discount
        row = {"item_code": item, "description": text, "qty": float(qty), "rate": float(rate),
               "income_account": account}
        if percent:
            row.update(price_list_rate=float(price), discount_percentage=float(percent))
        rows.append(row)
        key = None
        if pos.get("tax_id") is not None:
            _tax(pos["tax_id"], lookups)
            key = str(pos["tax_id"])
        amounts[key] = amounts.get(key, ZERO) + _cents(qty * rate)
    return rows, amounts, discount_row


def _document(doctype, record, lookups, credit=False):
    """(ERPNext document, differences, (net, tax, gross) in the document currency, rate to CHF).

    Prices are net, unless bexio says they include the VAT (mwst_is_net false): then the tax is taken out of
    the gross and its row is marked as included. The tax is worked out per rate from the lines, the way
    ERPNext would; bexio's own tax per rate is compared, never copied in. The total is bexio's `total`, the
    one that includes the VAT after the discounts (total_gross is before them). A difference of up to
    5 rappen against it goes into the last tax row; a larger one is unmapped.
    """
    bexio_id = ("credit-" if credit else "") + str(record["id"])
    customer = lookups["customer"].get(str(record.get("contact_id")))
    if customer is None:
        raise Unmapped("contact {} has no Customer".format(record.get("contact_id")))
    included = record.get("mwst_is_net") is False
    currency, rate = _currency(record, lookups)
    rows, amounts, discount_row = _rows(record, lookups)
    if discount_row and (included or credit):
        raise Unmapped("a document discount with prices including the VAT or on a credit note")
    lines = sum(amounts.values(), ZERO)

    # a document discount: bexio gives none as an amount, so it is the lines less bexio's net; the
    # taxes are worked out on what is left, shared out over the lines
    discount = ZERO
    if discount_row:
        if record.get("total_net") is None:
            raise Unmapped("record has no total_net")
        discount = lines - _dec(record["total_net"])
        if discount <= ZERO:
            raise Unmapped("the document discount does not reduce the net")
    taxable = lines - discount

    taxes, computed, differences = [], {}, []
    last_rate = None
    for key, amount in amounts.items():
        if key is None:
            continue
        template = _tax(key, lookups)
        base = amount * taxable / lines if discount else amount
        if included:
            tax_amount = _cents(base * template["rate"] / (100 + template["rate"]))
        else:
            tax_amount = _cents(base * template["rate"] / 100)
        row = {"charge_type": "Actual", "account_head": template["account"],
               "description": template["name"], "tax_amount": float(tax_amount)}
        if included:
            row["included_in_print_rate"] = 1
        taxes.append(row)
        computed[template["rate"]] = computed.get(template["rate"], ZERO) + tax_amount
        last_rate = template["rate"]
    tax = sum(computed.values(), ZERO)

    given = {}
    for tax_line in record.get("taxs") or []:
        given[_dec(tax_line["percentage"])] = _cents(_dec(tax_line["value"]))
    for percentage in sorted(set(computed) | set(given)):
        diff = given.get(percentage, ZERO) - computed.get(percentage, ZERO)
        if diff:
            differences.append("tax {:f}% {:+}".format(percentage.normalize(), diff))

    if record.get("total") is None:
        raise Unmapped("record has no total")
    target = _dec(record["total"])
    grand = lines if included else taxable + tax
    absorb = target - grand
    if abs(absorb) > TOTAL_TOLERANCE:
        raise Unmapped("total differs from bexio's by {:+}".format(absorb))
    if absorb:
        if last_rate is None:
            raise Unmapped("nothing to take a total difference of {:+} in".format(absorb))
        taxes[-1]["tax_amount"] = float(_dec(taxes[-1]["tax_amount"]) + absorb)
        computed[last_rate] += absorb
        tax += absorb
        differences.append("total {:+} taken into the last tax row".format(absorb))
    net = target - tax if included else taxable
    for label, have, field in (("net", net, "total_net"), ("taxes", tax, "total_taxes")):
        if record.get(field) is None:
            raise Unmapped("record has no {}".format(field))
        diff = have - _dec(record[field])
        if diff:
            differences.append("{} {:+}".format(label, diff))

    doc = {
        "doctype": doctype, "company": COMPANY, "currency": currency, "conversion_rate": float(rate),
        "bexio_id": bexio_id,
        "terms": "\n".join(part for part in (record.get("header"), record.get("footer")) if part),
        "items": rows, "taxes": taxes,
    }
    if discount:
        doc.update(apply_discount_on="Net Total", discount_amount=float(discount))
    if doctype == "Quotation":
        doc.update(quotation_to="Customer", party_name=customer, transaction_date=record["is_valid_from"],
                   valid_till=record.get("is_valid_until"))
    else:
        doc["customer"] = customer
    if doctype == "Sales Invoice":
        doc.update(remarks="bexio Nr. {}".format(record.get("document_nr") or ""),
                   posting_date=record["is_valid_from"], due_date=record.get("is_valid_to"),
                   set_posting_time=1)
    else:
        # ERPNext's Sales Order and Quotation have neither a remarks field nor an income_account on their items
        for row in rows:
            row.pop("income_account", None)
    if doctype == "Sales Order":
        doc["transaction_date"] = record["is_valid_from"]
        for row in rows:
            row["delivery_date"] = record["is_valid_from"]

    sign = -1 if credit else 1
    if credit:
        original = lookups["invoice"].get(str(record.get(ORIGINAL)))
        if original is None:
            raise Unmapped("original invoice {} is not in ERPNext or the export".format(record.get(ORIGINAL)))
        doc.update(is_return=1, return_against=original)
        for row in rows:
            row["qty"] = -row["qty"]
        for row in taxes:
            row["tax_amount"] = -row["tax_amount"]
    totals = tuple(sign * x for x in (net, tax, target))
    return doc, differences, totals, rate


def sales_invoice(record, lookups):
    """The Sales Invoice of a bexio invoice (kb_invoice with its positions)."""
    return _document("Sales Invoice", record, lookups)[0]


def credit_note(record, lookups):
    """A Sales Invoice with is_return against the invoice it credits."""
    return _document("Sales Invoice", record, lookups, credit=True)[0]


def sales_order(record, lookups):
    """The Sales Order of a bexio order (kb_order)."""
    return _document("Sales Order", record, lookups)[0]


def quotation(record, lookups):
    """The Quotation of a bexio offer (kb_offer)."""
    return _document("Quotation", record, lookups)[0]


def unknown_fields(doc, metas):
    """Fields of the document and its rows that the ERPNext doctype does not have."""
    found = []
    for key in doc:
        if key in ("doctype", "items", "taxes"):
            continue
        if key not in metas[doc["doctype"]]:
            found.append("{}.{}".format(doc["doctype"], key))
    for table, doctype in (("items", doc["doctype"] + " Item"), ("taxes", "Sales Taxes and Charges")):
        for row in doc.get(table, []):
            found.extend("{}.{}".format(doctype, key) for key in row if key not in metas[doctype])
    return sorted(set(found))


def plan(data, lookups, metas=None):
    """Map every record of the export. Nothing is written; returns one result per record."""
    results = []
    for key, doctype, credit in DOCUMENTS:
        for record in data.get(key, []):
            bexio_id = ("credit-" if credit else "") + str(record["id"])
            result = {"key": key, "doctype": doctype, "bexio_id": bexio_id, "record": record,
                      "year": str(record.get("is_valid_from") or "????")[:4], "doc": None,
                      "differences": [], "chf": None, "unknown": [], "error": None}
            try:
                doc, differences, totals, rate = _document(doctype, record, lookups, credit)
            except Unmapped as err:
                result["error"] = str(err)
            else:
                result.update(doc=doc, differences=differences, chf=tuple(x * rate for x in totals))
                if metas:
                    result["unknown"] = unknown_fields(doc, metas)
            results.append(result)
    return results


def load_documents(path):
    """The export's documents and currencies; a file the export does not have is left out."""
    data = {}
    for name in [key for key, _doctype, _credit in DOCUMENTS] + ["currencies"]:
        file = os.path.join(path, name + ".json")
        if os.path.exists(file):
            with open(file, encoding="utf-8") as f:
                data[name] = json.load(f)
    data.setdefault("currencies", [])
    return data


def lookups_from_erp(erp, data):
    """What the mapping needs from ERPNext, read only: the documents by bexio_id, the tax account."""
    def bexio_names(doctype):
        rows = erp.list(doctype, [["bexio_id", "is", "set"]], ["name", "bexio_id"])
        return {r["bexio_id"]: r["name"] for r in rows}

    invoices = bexio_names("Sales Invoice")
    for record in data.get("invoices", []):
        invoices.setdefault(str(record["id"]), PLANNED)
    # the tax templates by their bexio tax id; a template with one rate row gives its rate and account
    tax = {}
    for template in erp.list("Sales Taxes and Charges Template", [["bexio_id", "is", "set"]], ["name", "bexio_id"]):
        rows = erp.get("Sales Taxes and Charges Template", template["name"]).get("taxes") or []
        single = rows[0] if len(rows) == 1 else None
        tax[template["bexio_id"]] = {
            "name": template["name"],
            "account": single and single["account_head"],
            "rate": single and _dec(single["rate"]),
        }
    return {
        "currency": {str(c["id"]): c["name"] for c in data["currencies"]},
        "customer": bexio_names("Customer"),
        "item": bexio_names("Item"),
        "account": bexio_names("Account"),
        "tax": tax,
        "invoice": invoices,
        "existing": {dt: set(bexio_names(dt)) for dt in ("Sales Invoice", "Sales Order", "Quotation")},
    }


def doctype_fields(erp, doctype):
    """The field names of a doctype, custom fields included: the meta ERPNext builds, which the agent API user may read."""
    return {f["fieldname"] for f in erp.meta(doctype)["fields"]}


def summary(results, lookups):
    """The totals of a dry run, one line per export file and per year; no names, no bexio ids."""
    lines = ["{:<17}{:>9}{:>8}{:>10}{:>13}{:>12}{:>11}".format(
        "export", "records", "mapped", "unmapped", "differences", "in ERPNext", "to create")]
    for key, doctype, _credit in DOCUMENTS:
        rows = [r for r in results if r["key"] == key]
        if not rows:
            continue
        mapped = [r for r in rows if r["doc"]]
        exists = sum(1 for r in mapped if r["bexio_id"] in lookups["existing"][doctype])
        lines.append("{:<17}{:>9}{:>8}{:>10}{:>13}{:>12}{:>11}".format(
            key, len(rows), len(mapped), len(rows) - len(mapped), sum(1 for r in mapped if r["differences"]),
            exists, len(mapped) - exists))

    per_year = collections.OrderedDict()
    for r in sorted((r for r in results if r["doc"]), key=lambda r: (r["key"], r["year"])):
        acc = per_year.setdefault((r["key"], r["year"]), [0, ZERO, ZERO, ZERO])
        acc[0] += 1
        for i, value in enumerate(r["chf"]):
            acc[i + 1] += value
    lines.append("")
    lines.append("CHF by year of the document date (net, tax, gross):")
    for (key, year), (count, net, tax, gross) in per_year.items():
        lines.append("  {:<16}{}  {:>5} docs  {:>14,.2f}{:>12,.2f}{:>14,.2f}".format(key, year, count, net, tax, gross))

    statuses = collections.Counter(str(r["record"].get("kb_item_status_id")) for r in results if r["key"] == "invoices")
    if statuses:
        lines.append("")
        lines.append("invoices by status: " + ", ".join("{}: {}".format(s, n) for s, n in sorted(statuses.items())))
    return "\n".join(lines)


def detail_lines(results):
    """The differences and unmapped records, by bexio id, for the private report file."""
    lines = []
    for r in results:
        if r["error"]:
            lines.append("{} {}: unmapped: {}".format(r["doctype"], r["bexio_id"], r["error"]))
        elif r["differences"]:
            lines.append("{} {}: {}".format(r["doctype"], r["bexio_id"], "; ".join(r["differences"])))
        for field in r["unknown"]:
            lines.append("{} {}: ERPNext has no field {}".format(r["doctype"], r["bexio_id"], field))
    return lines


def main(argv):
    parser = argparse.ArgumentParser(description="Map the exported bexio sales documents to ERPNext (dry run only for now).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals")
    parser.add_argument("--report", default=os.path.join(im.PRIVATE, "bexio-sales-differences.txt"),
                        help="private file for the unmapped and differing records, by bexio id")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if not args.dry_run:
        parser.error("dry run only for now: the live run is erp-a2ma's, after the posting plan (finance-3qsp)")

    export_dir = args.export or im.newest_export()
    data = load_documents(export_dir)
    erp = im.Erp.from_file(args.token_file)
    try:
        lookups = lookups_from_erp(erp, data)
        metas = {dt: doctype_fields(erp, dt) for dt in (
            "Sales Invoice", "Sales Invoice Item", "Sales Order", "Sales Order Item",
            "Quotation", "Quotation Item", "Sales Taxes and Charges")}
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2

    results = plan(data, lookups, metas)
    print("sales documents from {}".format(export_dir))
    print(summary(results, lookups))
    lines = detail_lines(results)
    print("dry run: nothing was written; {} lines of differences or unmapped records in {}".format(len(lines), args.report))
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    unmapped = sum(1 for r in results if r["error"])
    return 1 if unmapped or lines else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
