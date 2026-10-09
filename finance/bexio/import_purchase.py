"""Map the bexio purchase bills (and expenses) to ERPNext Purchase Invoices.

Step three of the bexio pipeline, after import_master.py has put the master
data into ERPNext. This module maps one record at a time: map_bill() and
map_expense() return the ERPNext document dict. The write is the loader's
(bexio-drafts.py): drafts() hands it the mapped bills, and the upsert by
bexio_id happens there, inside the backend container.

Run it as:

    python3 finance/bexio/import_purchase.py --dry-run [--export DIR]
    python3 finance/bexio/import_purchase.py --apply [--export DIR]

--export defaults to the newest directory under <private>/bexio-export/. The
export holds bills.json (the list, with totals) and bills-detail.json (one
single-bill record per bill, with its positions); expenses.json is read when
it exists. The dry run prints totals per year and currency only. The bexio ids
of the records it cannot map, and the ones whose totals differ, go to
<private>/bexio-purchase-dry-run.txt, never to the screen or the repository.

Each bill: the supplier is the Supplier of its supplier_id; each line is one
line of the generic service item "bexio Aufwand", expensed to the Account of
its booking account. The lines are bills.json's line_items, which the full
export carries; bills-detail.json (the positions with quantity and unit
price) is read only where a bill has no line_items. A line's amount is its
gross when the bill's prices include the VAT (item_net false), and its VAT is
bexio's tax_calc: the net is the gross less it. A reverse-charge bill (item_net
true) is not mapped here. The VAT of a line is the Item Tax Template whose
bexio_id is the line's bexio tax id, never one picked by rate; a tax id with no
such template is reported, not guessed. The tax is booked to the Vorsteuer
account of its kind (1170 Material/DL, 1171 Invest./Aufwand) as one tax row
per account and template, so the tax total is bexio's to the rappen.

An expense with VAT is skipped: its net split is not in the export; the
bills and the expense step of erp-a2ma take it.

A reverse-charge bill (item_net true, Bezugsteuer codes) is mapped with its
net as the amount. Its VAT is booked twice, as input tax on the Vorsteuer
account of its kind and taken back on 2203 (the templates BZM77, BZM81, BZB77
and BZB81 carry both rows), so the bill's total is its net, as bexio books it.

--dry-run (the default) reads ERPNext and writes nothing. --apply hands the
mapped bills to the loader (finance/scripts/bexio-drafts.sh), which inserts
them as drafts named by bexio's document number. Expenses are not written.

Standard library only, apart from import_master.py.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
from decimal import Decimal, ROUND_HALF_UP

import import_master as im

ITEM = "bexio Aufwand"
BASE_CURRENCY = "CHF"
CENT = Decimal("0.01")
ZERO = Decimal("0")
ATTACHMENTS_FIELD = "bexio_attachment_ids"  # not a field of ERPNext's Purchase Invoice yet: an insert must drop it
DETAIL_FILE = "bills-detail.json"
EXPENSES_FILE = "expenses.json"
PROBLEMS_FILE = "bexio-purchase-dry-run.txt"
DRAFTS_FILE = "bexio-purchase-drafts.json"
LOADER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "bexio-drafts.sh")
# the most a line's VAT may differ from its rate worked out on the net
TAX_TOLERANCE = Decimal("0.05")

# bexio purchase VAT codes: the Vorsteuer account by the kind of cost
MAT_SV_IDS = (22, 35, 8, 34, 21, 36)  # VM77, VM81, VM25, VM26, VM37, VM38: Material und Dienstleistungen
INV_BA_IDS = (24, 38, 12, 37, 23, 39)  # VB77, VB81, VB25, VB26, VB37, VB38: Investitionen und Aufwand
VORSTEUER = dict([(i, "1170") for i in MAT_SV_IDS] + [(i, "1171") for i in INV_BA_IDS])
# reverse charge (Bezugsteuer): the Vorsteuer account of the kind, and the rate; the deduction is on BEZUGSTEUER
BEZUG = {19: ("1170", 7.7), 33: ("1170", 8.1), 20: ("1171", 7.7), 32: ("1171", 8.1)}  # BZM77, BZM81, BZB77, BZB81
BEZUGSTEUER = "2203"
# 0 % purchase code and the import taxes that carry no VAT on the bill: no tax row
ZERO_RATE_IDS = (47, 7, 10)

class MappingError(Exception):
    """A record that has no ERPNext home as it is: the dry run lists it, the live run does not write it."""


class Skipped(Exception):
    """A record that is left out on purpose (the reason is printed)."""


class Lookups:
    """What the mapping reads from ERPNext: suppliers, accounts and Item Tax Templates by bexio_id."""

    def __init__(self, suppliers, accounts, account_by_number, taxes):
        self.suppliers = suppliers              # bexio contact id -> Supplier name
        self.accounts = accounts                # bexio account id -> Account name
        self.account_by_number = account_by_number  # KMU account number -> Account name
        self.taxes = taxes                      # bexio tax id -> Item Tax Template name

    @classmethod
    def from_erp(cls, erp):
        accounts = erp.list("Account", [["company", "=", im.COMPANY]], ["name", "account_number", "bexio_id"])
        return cls(
            suppliers={r["bexio_id"]: r["name"] for r in erp.list("Supplier", [["bexio_id", "is", "set"]], ["name", "bexio_id"])},
            accounts={r["bexio_id"]: r["name"] for r in accounts if r["bexio_id"]},
            account_by_number={r["account_number"]: r["name"] for r in accounts if r["account_number"]},
            taxes={r["bexio_id"]: r["name"] for r in erp.list("Item Tax Template", [["company", "=", im.COMPANY], ["bexio_id", "is", "set"]], ["name", "bexio_id"])},
        )


def line_vat(tax_id):
    """(rate, kind, Vorsteuer account) of a purchase line; rate and account are None for a line without VAT.

    The rate is bexio's own for the code, and only checks the VAT of the line; the template is looked up by the code.
    """
    if tax_id is None or tax_id in ZERO_RATE_IDS:
        return 0.0, None, None
    if tax_id in BEZUG:
        account, rate = BEZUG[tax_id]
        return rate, None, account
    if tax_id not in VORSTEUER:
        raise MappingError("unknown purchase VAT code {}".format(tax_id))
    rate, kind = im.VAT_OF_TAX_ID[tax_id]
    return rate, kind, VORSTEUER[tax_id]


def _day(record):
    return datetime.date.fromisoformat(record["bill_date"])


def _money(value):
    return Decimal(str(value))


def _tax_of(net, rate):
    return (net * Decimal(str(rate)) / 100).quantize(CENT, rounding=ROUND_HALF_UP)


def _sign(row):
    """1 for input tax, -1 for a deduction (the reverse charge's take-back)."""
    return -1 if row.get("add_deduct_tax") == "Deduct" else 1


def _document(bexio_id, record, supplier, currency, rate_of_exchange, lines, taxes):
    """The Purchase Invoice dict; lines are (item row, Decimal net), taxes are {(account, title, Add or Deduct): Decimal}."""
    return {
        "doctype": "Purchase Invoice", "company": im.COMPANY, "supplier": supplier,
        "bill_no": record.get("vendor_ref") or record.get("document_no") or str(bexio_id),
        "bill_date": record["bill_date"], "posting_date": record["bill_date"], "set_posting_time": 1,
        "due_date": record.get("due_date") or record["bill_date"],
        "currency": currency, "conversion_rate": rate_of_exchange,
        "bexio_id": str(bexio_id),
        ATTACHMENTS_FIELD: ",".join(record.get("attachment_ids") or []),
        "items": [row for row, _ in lines],
        "taxes": [
            {"charge_type": "Actual", "account_head": account, "description": title,
             "tax_amount": float(amount), "add_deduct_tax": kind}
            for (account, title, kind), amount in taxes.items()
        ],
    }


def _lines_of(bill):
    """The lines of a bill as dicts: text, account_id, tax_id, net, and the VAT bexio gives (None: worked out from the rate).

    The lines are line_items where the bill has them; otherwise the detail positions, with a quantity and a unit price.
    """
    if bill.get("line_items"):
        if bill.get("item_net") and any(line.get("tax_calc") and line.get("tax_id") not in BEZUG for line in bill["line_items"]):
            raise MappingError("net amounts with VAT other than reverse charge are not mapped")
        lines = []
        for line in bill["line_items"]:
            if bill.get("item_net"):
                # a reverse-charge line carries bexio's VAT on the net, which the deduction takes back
                net = _money(line["amount"])
                given = _money(line["tax_calc"]) if line.get("tax_calc") else None
            else:
                given = _money(line.get("tax_calc") or 0)
                net = _money(line["amount"]) - given
            lines.append({"text": line.get("title") or "", "account_id": line["booking_account_id"],
                          "tax_id": line.get("tax_id"), "net": net, "given": given})
        return lines
    lines = []
    for pos in bill.get("positions") or []:
        quantity, price = _money(pos["amount"]), _money(pos["unit_price"])
        lines.append({"text": pos.get("text") or "", "account_id": pos["booking_account_id"],
                      "tax_id": pos.get("tax_id"), "net": (quantity * price).quantize(CENT, rounding=ROUND_HALF_UP),
                      "given": None})
    return lines


def map_bill(bill, lookups):
    """The Purchase Invoice dict for one bexio bill; raises MappingError when a part has no home."""
    lines_in = _lines_of(bill)
    if not lines_in:
        raise MappingError("no positions in the export")
    contact = bill.get("supplier_id", bill.get("contact_id"))
    supplier = lookups.suppliers.get(str(contact))
    if not supplier:
        raise MappingError("no Supplier for contact {}".format(contact))
    currency = bill["currency_code"]
    if currency != BASE_CURRENCY and not bill.get("exchange_rate"):
        raise MappingError("no exchange rate for {}".format(currency))
    lines, taxes = [], {}
    for line in lines_in:
        expense = lookups.accounts.get(str(line["account_id"]))
        if not expense:
            raise MappingError("no Account for booking account {}".format(line["account_id"]))
        net = line["net"]
        lines.append(({
            "item_code": ITEM, "item_name": ITEM, "description": line["text"] or ITEM, "uom": "Nos",
            "qty": 1.0, "rate": float(net), "amount": float(net), "expense_account": expense,
        }, net))
        rate, kind, account = line_vat(line["tax_id"])
        if rate == 0:
            if line["given"]:
                raise MappingError("VAT {} on a line without a VAT code".format(line["given"]))
            continue
        template = lookups.taxes.get(str(line["tax_id"]))
        if not template:
            raise MappingError("no Item Tax Template with bexio_id {} in ERPNext".format(line["tax_id"]))
        account_name = lookups.account_by_number.get(account)
        if not account_name:
            raise MappingError("no Account {} in ERPNext".format(account))
        tax = line["given"] if line["given"] is not None else _tax_of(net, rate)
        if abs(tax - _tax_of(net, rate)) > TAX_TOLERANCE:
            raise MappingError("VAT {} is not {}% of the net {}".format(tax, rate, net))
        if line["tax_id"] in BEZUG:
            deduction = lookups.account_by_number.get(BEZUGSTEUER)
            if not deduction:
                raise MappingError("no Account {} in ERPNext".format(BEZUGSTEUER))
            for key in ((account_name, template, "Add"), (deduction, template, "Deduct")):
                taxes[key] = taxes.get(key, ZERO) + tax
            continue
        key = (account_name, template, "Add")
        taxes[key] = taxes.get(key, ZERO) + tax
    rate_of_exchange = 1.0 if currency == BASE_CURRENCY else float(bill["exchange_rate"])
    return _document(bill["id"], bill, supplier, currency, rate_of_exchange, lines, taxes)


def map_expense(expense, lookups):
    """The Purchase Invoice dict for one bexio expense. An expense with VAT needs its net split, which the export does not carry."""
    gross = _money(expense["gross"])
    if expense.get("status") == "draft" and gross == ZERO:
        raise Skipped("draft with gross 0")
    if expense.get("tax_id") not in ZERO_RATE_IDS:
        raise Skipped("expense with VAT: the net split is not in the export; left to erp-a2ma")
    supplier = lookups.suppliers.get(str(expense.get("contact_id")))
    if not supplier:
        raise MappingError("no Supplier for contact {}".format(expense.get("contact_id")))
    account = lookups.accounts.get(str(expense["booking_account_id"]))
    if not account:
        raise MappingError("no Account for booking account {}".format(expense["booking_account_id"]))
    record = dict(expense, bill_date=expense["paid_on"])
    item = {"item_code": ITEM, "item_name": ITEM, "description": expense.get("text") or ITEM, "uom": "Nos",
            "qty": 1, "rate": float(gross), "amount": float(gross), "expense_account": account}
    return _document(expense["id"], record, supplier, expense["currency_code"],
                     float(expense.get("exchange_rate") or 1), [(item, gross)], {})


def document_totals(doc):
    """(net, tax, grand total) of a mapped document, from its rows: a deduction takes its tax back off."""
    net = sum((_money(row["amount"]) for row in doc["items"]), ZERO)
    tax = sum((_money(row["tax_amount"]) * _sign(row) for row in doc["taxes"]), ZERO)
    return net, tax, net + tax


def drafts(bills, lookups):
    """The bills that map and whose total is bexio's, as the loader takes them: each named by bexio's document
    number, its ERPNext name. A bill whose total differs is left out; the dry run lists it."""
    out, names = [], set()
    for bill in bills:
        try:
            doc = map_bill(bill, lookups)
        except MappingError:
            continue
        if document_totals(doc)[2] != _money(bill["gross"]):
            continue
        name = str(bill["document_no"])
        if name in names:
            raise MappingError("two bills with the document number {}".format(name))
        names.add(name)
        out.append({"doctype": "Purchase Invoice", "name": name, "bexio_id": doc["bexio_id"],
                    "values": {k: v for k, v in doc.items() if k != "doctype"}})
    return out


class Totals:
    """Per year and currency: the bexio sums, the sums of what maps, and what differs."""

    def __init__(self):
        self.rows = {}
        self.problems = []

    def row(self, year, currency):
        return self.rows.setdefault((year, currency), {
            "records": 0, "mapped": 0, "skipped": 0, "unmapped": 0, "differ": 0,
            "net": ZERO, "tax": ZERO, "gross": ZERO, "erp_net": ZERO, "erp_tax": ZERO, "erp_gross": ZERO,
        })


def dry_run(bills, expenses, lookups):
    """Map every record and compare the totals with bexio's; writes nothing. Returns the Totals."""
    totals = Totals()
    for kind, records, mapper in (("bill", bills, map_bill), ("expense", expenses, map_expense)):
        for rec in records:
            day = _day(rec) if kind == "bill" else datetime.date.fromisoformat(rec["paid_on"])
            row = totals.row(day.year, rec["currency_code"])
            row["records"] += 1
            gross = _money(rec["gross"])
            net = _money(rec["net"]) if rec.get("net") not in (None, "") else gross
            row["net"] += net
            row["gross"] += gross
            row["tax"] += gross - net
            ref = "{} {}".format(kind, rec["id"])
            try:
                doc = mapper(rec, lookups)
            except Skipped as err:
                row["skipped"] += 1
                totals.problems.append("{}: skipped, {}".format(ref, err))
                continue
            except MappingError as err:
                row["unmapped"] += 1
                totals.problems.append("{}: unmapped, {}".format(ref, err))
                continue
            row["mapped"] += 1
            erp_net, erp_tax, erp_gross = document_totals(doc)
            row["erp_net"] += erp_net
            row["erp_tax"] += erp_tax
            row["erp_gross"] += erp_gross
            if (erp_net, erp_tax, erp_gross) != (net, gross - net, gross):
                row["differ"] += 1
                totals.problems.append("{}: differs, bexio {} {} {} against ERPNext {} {} {}".format(
                    ref, net, gross - net, gross, erp_net, erp_tax, erp_gross))
    return totals


def write_private(path, lines):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def report(totals, export_dir):
    lines = ["purchase dry run from {}".format(export_dir),
             "{:<6}{:<5}{:>8}{:>8}{:>9}{:>9}{:>9}{:>14}{:>12}{:>14}{:>10}".format(
                 "year", "cur", "records", "mapped", "skipped", "unmapped", "differ", "net", "tax", "gross", "erp gross")]
    for (year, currency), r in sorted(totals.rows.items()):
        lines.append("{:<6}{:<5}{:>8}{:>8}{:>9}{:>9}{:>9}{:>14}{:>12}{:>14}{:>10}".format(
            year, currency, r["records"], r["mapped"], r["skipped"], r["unmapped"], r["differ"],
            r["net"], r["tax"], r["gross"], r["erp_gross"]))
    lines.append("dry run: nothing was written")
    return "\n".join(lines)


def load_records(export_dir):
    """The bills with their positions (the detail file where it exists) and the expenses."""
    with open(os.path.join(export_dir, "bills.json"), encoding="utf-8") as f:
        listed = json.load(f)
    detail = {}
    path = os.path.join(export_dir, DETAIL_FILE)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            detail = {str(b["id"]): b for b in json.load(f)}
    bills = [dict(b, **detail.get(str(b["id"]), {})) for b in listed]
    expenses = []
    path = os.path.join(export_dir, EXPENSES_FILE)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            expenses = json.load(f)
    return bills, expenses


def main(argv):
    parser = argparse.ArgumentParser(description="Map the bexio purchase bills to ERPNext Purchase Invoices (a dry run by default).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals (the default)")
    parser.add_argument("--apply", action="store_true", help="write the bills as drafts into ERPNext, through the loader")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are one or the other")
    export_dir = args.export or im.newest_export()
    bills, expenses = load_records(export_dir)
    try:
        lookups = Lookups.from_erp(im.Erp.from_file(args.token_file))
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    totals = dry_run(bills, expenses, lookups)
    print(report(totals, export_dir))
    if totals.problems:
        write_private(os.path.join(im.PRIVATE, PROBLEMS_FILE), totals.problems)
        print("{} record(s) listed by bexio id in {}".format(len(totals.problems), os.path.join(im.PRIVATE, PROBLEMS_FILE)), file=sys.stderr)
    if args.apply:
        path = os.path.join(im.PRIVATE, DRAFTS_FILE)
        documents = drafts(bills, lookups)
        write_private(path, [json.dumps({"documents": documents, "exchange_rates": []}, indent=1)])
        print("{} bills handed to the loader, in {}".format(len(documents), path))
        subprocess.run(["sh", LOADER, path], check=True)
    bad = sum(r["unmapped"] + r["differ"] for r in totals.rows.values())
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
