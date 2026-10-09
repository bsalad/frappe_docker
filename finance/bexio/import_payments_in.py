"""Map the bexio payments on invoices to ERPNext Payment Entries (receive).

Step four of the bexio pipeline, after import_master.py and import_sales.py: each
payment row of invoice_payments.json becomes a Payment Entry of payment_type Receive,
allocated against the Sales Invoice it pays. The invoice is found by its bexio_id in
ERPNext; when erp-tvjk has not drafted it yet, the planned name is bexio's document
number, which erp-tvjk names its drafts by. Each function takes one export record and
the lookups and returns the ERPNext document as a dict, not yet inserted.

Whether a receipt posts GL and how a foreign currency is settled is the posting plan of
finance-3qsp; the live run is erp-a2ma's. So nothing is written here: --dry-run reads
ERPNext and prints totals only.

A payment that cannot be mapped raises Unmapped with a reason, and the reason is counted
in the summary. A payment larger than what its invoice still owes is unmapped, not
capped. What an invoice owes is counted over the mapped payments in date order, so an
unmapped payment does not reduce it. The reasons never name a company; the bexio ids of
the unmapped payments and the reconciliation lines go to <private>, never to the screen
or the repository.

Amounts are in the invoice currency. bexio's payments settle the invoice's `total`, the
gross including VAT; `total_gross` is not the gross for most VAT invoices in the export.
A foreign-currency payment is received in CHF at the Currency Exchange rate on or before
its date (the records erp-tvjk creates).

Run it as:

    python3 finance/bexio/import_payments_in.py --dry-run [--export DIR]

--export defaults to the newest directory under <private>/bexio-export/. Standard
library only, plus import_master and import_sales.
"""

import argparse
import collections
import json
import os
import sys
from decimal import ROUND_HALF_UP, Decimal

import import_master as im
import import_sales as isl

BASE_CURRENCY = "CHF"
CENT = Decimal("0.01")
ZERO = Decimal("0")
PROBLEMS_FILE = "bexio-payments-dry-run.txt"

# bexio payment flags that are not money in a bank: each is unmapped, never booked as a receipt
NOT_A_RECEIPT = (
    ("kb_credit_voucher_id", "credit voucher offset, not a bank receipt"),
    ("is_cash_discount", "cash discount, not a bank receipt"),
    ("is_client_account_redemption", "client account redemption, not a bank receipt"),
)


class Unmapped(Exception):
    """A payment the importer does not map. The message names the reason, never a company name."""


def _dec(value):
    return Decimal(str(value))


def _cents(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _money(value):
    """A bexio amount as cents: bexio's payment values carry six decimals."""
    return _cents(_dec(value))


def invoice_total(invoice):
    """The amount the payments settle: bexio's total, the gross including VAT."""
    return _dec(invoice["total"])


def rate_on(lookups, currency, day):
    """The Currency Exchange rate to CHF on or before the day, the newest record; None when there is none."""
    if currency == BASE_CURRENCY:
        return Decimal("1")
    for rate_day, rate in lookups["exchange"].get((currency, BASE_CURRENCY), []):
        if rate_day <= day:
            return rate
    return None


def map_payment(row, invoice, lookups, owing):
    """(Payment Entry dict, CHF received) for one bexio payment row; raises Unmapped.

    `invoice` is the export record of the payment's invoice, or None; `owing` is what that
    invoice still owes after the payments mapped before this one.
    """
    for flag, reason in NOT_A_RECEIPT:
        if row.get(flag):
            raise Unmapped(reason)
    if invoice is None:
        raise Unmapped("the payment's invoice is not in the export")
    customer = lookups["customer"].get(str(invoice.get("contact_id")))
    if customer is None:
        raise Unmapped("the invoice's contact has no Customer")
    if not row.get("bank_account_id"):
        # the export names no bank, and a payment row carries no booking account of its own
        raise Unmapped("no bank account and no booking account in the export")
    bank = lookups["bank"].get(str(row["bank_account_id"]))
    if bank is None:
        raise Unmapped("the bank account has no ERPNext Bank Account")
    if bank["currency"] != BASE_CURRENCY:
        raise Unmapped("the bank account is not in CHF")
    currency = lookups["currency"].get(str(invoice.get("currency_id")))
    if currency is None:
        raise Unmapped("the invoice's currency is not in the export")
    receivable = lookups["receivable"].get(currency)
    if receivable is None:
        raise Unmapped("no receivable account in {}".format(currency))
    rate = rate_on(lookups, currency, row["date"])
    if rate is None:
        raise Unmapped("no Currency Exchange {} to CHF on or before the payment date".format(currency))
    value = _money(row["value"])
    if value > owing:
        raise Unmapped("payment exceeds the outstanding amount of its invoice")
    name = lookups["invoice"].get(str(invoice["id"])) or invoice.get("document_nr")
    if not name:
        raise Unmapped("the invoice has no document number")
    received = _cents(value * rate)
    doc = {
        "doctype": "Payment Entry", "company": im.COMPANY, "payment_type": "Receive",
        "party_type": "Customer", "party": customer, "posting_date": row["date"],
        "paid_from": receivable, "paid_from_account_currency": currency,
        "paid_to": bank["account"], "paid_to_account_currency": BASE_CURRENCY,
        "paid_amount": float(value), "received_amount": float(received),
        "source_exchange_rate": float(rate), "target_exchange_rate": 1.0,
        "bexio_id": str(row["id"]),
        "references": [{
            "reference_doctype": "Sales Invoice", "reference_name": name,
            "allocated_amount": float(value), "total_amount": float(invoice_total(invoice)),
            "outstanding_amount": float(owing),
        }],
    }
    return doc, received


def plan(data, lookups):
    """Map every payment of the export, in date order; one result per payment row."""
    invoices = {str(i["id"]): i for i in data["invoices"]}
    rows = sorted((row for p in data["payments"] for row in p["rows"]), key=lambda r: (r["date"], r["id"]))
    owing = {}  # bexio invoice id -> what it still owes, after the mapped payments
    results = []
    for row in rows:
        key = str(row["kb_invoice_id"])
        invoice = invoices.get(key)
        remaining = owing.get(key, invoice_total(invoice) if invoice else ZERO)
        result = {"bexio_id": str(row["id"]), "invoice": key, "year": row["date"][:4],
                  "currency": lookups["currency"].get(str(invoice.get("currency_id"))) if invoice else None,
                  "value": _money(row["value"]), "doc": None, "chf": None, "error": None}
        try:
            doc, received = map_payment(row, invoice, lookups, remaining)
        except Unmapped as err:
            result["error"] = str(err)
        else:
            owing[key] = remaining - _money(row["value"])
            result.update(doc=doc, chf=received)
        results.append(result)
    return results


def reconciliation(results, data):
    """Per invoice with a mapped payment: the mapped amount against bexio's own received total, where they differ."""
    invoices = {str(i["id"]): i for i in data["invoices"]}
    mapped = collections.defaultdict(lambda: ZERO)
    for r in results:
        if r["doc"]:
            mapped[r["invoice"]] += r["value"]
    lines = []
    for key in sorted(mapped):
        have = _dec(invoices[key]["total_received_payments"])
        if have != mapped[key]:
            lines.append("invoice {}: mapped {} against bexio's received {}".format(key, mapped[key], have))
    return lines


def unknown_fields(doc, metas):
    """Fields of the Payment Entry and its references that ERPNext's doctype does not have."""
    found = ["Payment Entry.{}".format(k) for k in doc if k not in ("doctype", "references") and k not in metas["Payment Entry"]]
    for ref in doc["references"]:
        found.extend("Payment Entry Reference.{}".format(k) for k in ref if k not in metas["Payment Entry Reference"])
    return sorted(set(found))


def lookups_from_erp(erp, data):
    """What the mapping needs from ERPNext, read only: customers, bank accounts, receivables, rates, invoices."""
    def bexio_names(doctype):
        return {r["bexio_id"]: r["name"] for r in erp.list(doctype, [["bexio_id", "is", "set"]], ["name", "bexio_id"])}

    currency = {str(c["id"]): c["name"] for c in data["currencies"]}
    gl_bank = {r["bexio_id"]: r["account"] for r in erp.list("Bank Account", [["bexio_id", "is", "set"]], ["name", "bexio_id", "account"])}
    bank = {}
    for b in data["bank_accounts"]:
        if str(b["id"]) in gl_bank:
            bank[str(b["id"])] = {"account": gl_bank[str(b["id"])], "currency": currency.get(str(b["currency_id"]))}
    # the receivable of a currency: the lowest-named one (1100 for CHF; the KMU chart has 1102 as a second CHF one)
    receivable = {}
    for r in sorted(erp.list("Account", [["account_type", "=", "Receivable"], ["company", "=", im.COMPANY], ["is_group", "=", 0]],
                             ["name", "account_currency"]), key=lambda r: r["name"]):
        receivable.setdefault(r["account_currency"], r["name"])
    exchange = collections.defaultdict(list)
    for r in erp.list("Currency Exchange", [], ["from_currency", "to_currency", "date", "exchange_rate"]):
        exchange[(r["from_currency"], r["to_currency"])].append((str(r["date"]), _dec(r["exchange_rate"])))
    for rates in exchange.values():
        rates.sort(reverse=True)
    return {
        "currency": currency,
        "customer": bexio_names("Customer"),
        "invoice": bexio_names("Sales Invoice"),
        "bank": bank,
        "receivable": receivable,
        "exchange": dict(exchange),
    }


def load_payments(path):
    """The export's payments, invoices, bank accounts and currencies."""
    data = {}
    for name in ("invoice_payments", "invoices", "bank_accounts", "currencies"):
        with open(os.path.join(path, name + ".json"), encoding="utf-8") as f:
            data[name] = json.load(f)
    data["payments"] = data.pop("invoice_payments")
    return data


def summary(results):
    """Totals per year of the payment date, and the reasons; no names, no bexio ids."""
    lines = ["{:<8}{:>10}{:>8}{:>10}{:>18}".format("year", "payments", "mapped", "unmapped", "CHF received")]
    per_year = collections.OrderedDict()
    for r in results:
        acc = per_year.setdefault(r["year"], [0, 0, ZERO])
        acc[0] += 1
        if r["doc"]:
            acc[1] += 1
            acc[2] += r["chf"]
    for year, (count, mapped, chf) in per_year.items():
        lines.append("{:<8}{:>10}{:>8}{:>10}{:>18,.2f}".format(year, count, mapped, count - mapped, chf))
    lines.append("{:<8}{:>10}{:>8}{:>10}{:>18,.2f}".format("total", len(results), sum(a[1] for a in per_year.values()),
                                                           sum(a[0] - a[1] for a in per_year.values()), sum(a[2] for a in per_year.values())))

    unmapped = [r for r in results if r["error"]]
    if unmapped:
        lines.append("")
        lines.append("unmapped by reason:")
        for reason, count in collections.Counter(r["error"] for r in unmapped).most_common():
            lines.append("  {:>4}  {}".format(count, reason))
        # not converted: the amounts stay in the invoice's currency, so nothing is hidden in CHF
        lines.append("unmapped, in the invoice's currency, by year:")
        by_currency = collections.OrderedDict()
        for r in unmapped:
            key = (r["year"], r["currency"] or "?")
            by_currency[key] = by_currency.get(key, ZERO) + r["value"]
        for (year, currency), value in by_currency.items():
            lines.append("  {:<8}{:<5}{:>16,.2f}".format(year, currency, value))
    return "\n".join(lines)


def detail_lines(results, reconciled):
    """The unmapped payments by bexio id, and the reconciliation lines, for the private report file."""
    lines = ["payment {} (invoice {}): unmapped: {}".format(r["bexio_id"], r["invoice"], r["error"]) for r in results if r["error"]]
    return lines + reconciled


def main(argv):
    parser = argparse.ArgumentParser(description="Map the bexio payments on invoices to ERPNext Payment Entries (dry run only for now).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals")
    parser.add_argument("--report", default=os.path.join(im.PRIVATE, PROBLEMS_FILE),
                        help="private file for the unmapped payments and the reconciliation, by bexio id")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if not args.dry_run:
        parser.error("dry run only for now: the live run is erp-a2ma's, after the posting plan (finance-3qsp)")

    export_dir = args.export or im.newest_export()
    data = load_payments(export_dir)
    erp = im.Erp.from_file(args.token_file)
    try:
        lookups = lookups_from_erp(erp, data)
        metas = {dt: isl.doctype_fields(erp, dt) for dt in ("Payment Entry", "Payment Entry Reference")}
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2

    results = plan(data, lookups)
    for r in results:
        if r["doc"]:
            r["unknown"] = unknown_fields(r["doc"], metas)
    lines = detail_lines(results, reconciliation(results, data))
    lines += ["Payment Entry {}: ERPNext has no field {}".format(r["bexio_id"], f)
              for r in results if r["doc"] for f in r["unknown"]]
    print("payments from {}".format(export_dir))
    print(summary(results))
    print("dry run: nothing was written; {} lines of unmapped payments or reconciliation in {}".format(len(lines), args.report))
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
