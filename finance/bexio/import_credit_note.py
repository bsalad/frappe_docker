"""Map bexio's credit note to ERPNext: a Sales Invoice return against the invoice it credits.

bexio's credit-voucher endpoint answers 404, so the note is built from what the export has of it: its two journal
lines (KbCreditVoucher, the revenue against the receivables and the VAT against the receivables), the payment row
of the invoice it is applied to (its kb_credit_voucher_id), and that invoice (customer and currency). The note's
number is the one bexio's own text gives it. The export has one credit note; a second one would need its own
mapping, and is refused here rather than guessed.

Run it as:

    python3 finance/bexio/import_credit_note.py [--export DIR] [--write FILE]

Without --write it reads ERPNext and the export, writes nothing, and prints the totals. With --write it also writes
the return as a draft plan for the loader, which submits it (see bexio-drafts.sh). The output is totals only.
Standard library only, plus import_master and import_sales.
"""

import argparse
import json
import os
import sys

import import_master as im
import import_sales as isl

COMPANY = im.COMPANY
BASE_CURRENCY = "CHF"
CREDIT_CLASS = "KbCreditVoucher"
NUMBER_PREFIX = "Gutschrift "
# the free-text item the return's revenue line is booked on, as the other sales documents book theirs
GENERIC_ITEM = isl.GENERIC_ITEM


def _by_id(records, record_id, what):
    found = [record for record in records if record["id"] == record_id]
    if len(found) != 1:
        raise isl.Unmapped("expected one {} with id {}, found {}".format(what, record_id, len(found)))
    return found[0]


def applied_row(payments):
    """(invoice id, payment row) of the credit note: the one payment row with a kb_credit_voucher_id."""
    found = [(row["kb_invoice_id"], row) for parent in payments for row in parent["rows"] if row.get("kb_credit_voucher_id")]
    if len(found) != 1:
        raise isl.Unmapped("expected one payment row with a credit voucher, found {}".format(len(found)))
    return found[0]


def voucher_lines(journal, voucher_id):
    """The two journal lines of the credit voucher: the export's KbCreditVoucher lines with its id as ref_id."""
    lines = [line for line in journal if line.get("ref_class") == CREDIT_CLASS and line.get("ref_id") == voucher_id]
    if len(lines) != 2:
        raise isl.Unmapped("credit voucher {} has {} journal lines, expected 2".format(voucher_id, len(lines)))
    return lines


def credit_note(journal, payments, invoices, lookups):
    """(Sales Invoice return as a dict, the note's number, its totals in CHF: net, tax, grand total).

    The revenue line is the one at the invoice's net, the VAT line the one at its VAT, and both must be booked against
    the same account, and the total must be the payment row's and the invoice's own credit amount. The return's
    item is the revenue at minus one, and its VAT row is the VAT line at minus one, so the grand total is the
    negative of bexio's. update_outstanding_for_self is 0: the return reduces the invoice's outstanding, and the
    return itself has none.
    """
    invoice_id, row = applied_row(payments)
    invoice = _by_id(invoices, invoice_id, "invoice")
    voucher_id = row["kb_credit_voucher_id"]
    lines = voucher_lines(journal, voucher_id)
    if len({line["credit_account_id"] for line in lines}) != 1:
        raise isl.Unmapped("the credit voucher's lines do not credit one account")

    net = [line for line in lines if isl._dec(line["amount"]) == isl._dec(invoice["total_net"])]
    tax = [line for line in lines if isl._dec(line["amount"]) == isl._dec(invoice["total_taxes"])]
    if len(net) != 1 or len(tax) != 1 or net[0] is tax[0]:
        raise isl.Unmapped("the credit voucher's lines are not the invoice's net and VAT")
    revenue, vat = net[0], tax[0]
    positions = {pos.get("account_id") for pos in invoice.get("positions") or []}
    if revenue["debit_account_id"] not in positions:
        raise isl.Unmapped("the revenue line is not on an account of the invoice's positions")

    total = isl._dec(revenue["amount"]) + isl._dec(vat["amount"])
    if total != isl._dec(row["value"]) or total != isl._dec(invoice["total_credit_vouchers"]):
        raise isl.Unmapped("the credit voucher's total differs from the payment row's or the invoice's")

    if lookups["account"].get(str(vat["debit_account_id"])) != lookups["vat"]:
        raise isl.Unmapped("the VAT line's account is not the VAT account {}".format(lookups["vat"]))
    income = lookups["account"].get(str(revenue["debit_account_id"]))
    if income is None:
        raise isl.Unmapped("account {} has no Account".format(revenue["debit_account_id"]))
    customer = lookups["customer"].get(str(invoice["contact_id"]))
    if customer is None:
        raise isl.Unmapped("contact {} has no Customer".format(invoice["contact_id"]))
    if lookups["currency"].get(str(invoice["currency_id"])) != BASE_CURRENCY:
        raise isl.Unmapped("the invoice is not in {}".format(BASE_CURRENCY))
    original = lookups["invoice"].get(str(invoice["id"]))
    if original is None or original == isl.PLANNED:
        raise isl.Unmapped("invoice {} is not in ERPNext".format(invoice["id"]))
    text = row.get("kb_credit_voucher_text") or ""
    if not text.startswith(NUMBER_PREFIX):
        raise isl.Unmapped("the credit voucher's text names no number")
    number = text[len(NUMBER_PREFIX):]

    net_amount = isl._dec(revenue["amount"])
    tax_amount = isl._dec(vat["amount"])
    doc = {
        "doctype": "Sales Invoice", "company": COMPANY, "currency": BASE_CURRENCY, "conversion_rate": 1.0,
        "bexio_id": "credit-{}".format(voucher_id), "customer": customer,
        "is_return": 1, "return_against": original, "update_outstanding_for_self": 0,
        "posting_date": revenue["date"][:10], "set_posting_time": 1,
        "remarks": "bexio Gutschrift {} zu Rechnung {}".format(voucher_id, invoice["id"]),
        "items": [{"item_code": GENERIC_ITEM, "description": revenue.get("description") or NUMBER_PREFIX.strip(),
                   "qty": -1.0, "rate": float(net_amount), "income_account": income}],
        "taxes": [{"charge_type": "Actual", "account_head": lookups["vat"], "description": "bexio MWST",
                   "tax_amount": -float(tax_amount)}],
    }
    totals = (-net_amount, -tax_amount, -total)
    return doc, number, totals


def drafts(doc, number):
    """The return as the loader takes it: named by bexio's number, so the series does not number it."""
    values = {key: value for key, value in doc.items() if key != "doctype"}
    return {"documents": [{"doctype": doc["doctype"], "name": number, "bexio_id": doc["bexio_id"], "values": values}],
            "exchange_rates": []}


def _read(export_dir, name):
    with open(os.path.join(export_dir, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def main(argv):
    parser = argparse.ArgumentParser(description="Map bexio's credit note to a Sales Invoice return (a dry run by default).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--write", default=None, help="private file for the loader: the return as a draft plan")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)

    export_dir = args.export or im.newest_export()
    journal, payments, invoices = _read(export_dir, "journal"), _read(export_dir, "invoice_payments"), _read(export_dir, "invoices")
    data = {"currencies": _read(export_dir, "currencies"), "invoices": invoices}
    erp = im.Erp.from_file(args.token_file)
    try:
        lookups = isl.lookups_from_erp(erp, data)
    except (im.ErpError, isl.Unmapped) as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    try:
        doc, number, totals = credit_note(journal, payments, invoices, lookups)
    except isl.Unmapped as err:
        print("not mapped: {}".format(err), file=sys.stderr)
        return 1

    invoice_id, _row = applied_row(payments)
    bexio_total = isl._dec(_by_id(invoices, invoice_id, "invoice")["total_credit_vouchers"])
    print("credit note from {}".format(export_dir))
    print("  net {:>12,.2f}  tax {:>10,.2f}  grand total {:>12,.2f}".format(*[float(x) for x in totals]))
    print("  bexio's total {:,.2f}: {}".format(float(bexio_total),
                                               "equal to the cent" if -totals[2] == bexio_total else "DIFFERS"))
    state = erp.list("Sales Invoice", [["name", "=", doc["return_against"]]], ["docstatus", "status", "outstanding_amount"])
    for row in state:
        print("  invoice {}: docstatus {}, status {}, outstanding {:,.2f}".format(
            invoice_id, row["docstatus"], row["status"], float(row["outstanding_amount"])))
    if not state:
        print("  invoice {} is not in ERPNext".format(invoice_id))
    if args.write:
        isl.write_private(args.write, json.dumps(drafts(doc, number), indent=1))
        print("written: the return as a draft plan to {}".format(args.write))
    else:
        print("dry run: nothing was written")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
