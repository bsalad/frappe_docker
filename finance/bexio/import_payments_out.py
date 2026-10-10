"""Map the bexio bill payments to ERPNext Payment Entries (pay) against the Purchase Invoices.

Step four of the bexio pipeline, after import_purchase.py has put the bills into ERPNext. The source is the
journal, not the payment orders (payments.json): an order is an instruction, not a booking (posting plan
section 8), so a Payment Entry made from it would book the bank twice. A bill payment is the group of journal
lines that share one ref_uuid (KbClientAccountEntry). The debit on the payables account 2000 is what the bill
settles, the credit on the bank 1020 is what left the bank, and a credit to any other account is a deduction bexio
booked on the payment (a discount, say); the deduction goes on the Payment Entry as a deduction.

The export has no id that links a group to its bill, so the link is by amount, as the posting plan has it: the
payments and the bills of one CHF amount are paired in date order, the earliest payment with the earliest bill.
Every bill is paid once in bexio, so each bill has one group. A payment or a bill left over is listed, not booked.

The VAT bexio moves on payment (VAT_MOVES: 1172 to 1170 or 1171, and 2202 to 2203 on a reverse-charge bill, in the
same group) is not a Journal Entry here. ERPNext's Purchase Invoice books that VAT at the bill date already
(import_purchase.py), so the move is in the GL, and a second one would book the VAT twice. The lines are listed by
journal id in <private>.

ERPNext rounds a CHF bill to the rappen step, so for some bills its outstanding is 0.01 to 0.04 less than bexio's
payment. The payment is allocated at that outstanding, and the rest stays unallocated on the Payment Entry (ROUNDING).

The live run is --write FILE, which hands the Payment Entries to the loader (finance/scripts/bexio-drafts.sh FILE
submit). Nothing goes into ERPNext here. The dry run reads ERPNext and prints totals only.

Run it as:

    python3 finance/bexio/import_payments_out.py --dry-run [--export DIR]
    python3 finance/bexio/import_payments_out.py --write FILE [--export DIR]

--export defaults to the newest directory under <private>/bexio-export/. Standard library only, apart from
import_master and import_purchase. The bexio ids of what is unmapped go to <private>, never to the screen or the
repository.
"""

import argparse
import collections
import json
import os
import sys
from decimal import Decimal, ROUND_HALF_UP

import import_master as im
import import_purchase as ip

CURRENCY = "CHF"  # the bills are in CHF in ERPNext, and so are the bank accounts that pay them
CENT = Decimal("0.01")
ZERO = Decimal("0")
PAYABLE = "2000"                    # Verbindlichkeiten aus Lieferungen und Leistungen: what a bill settles
BANKS = ("1020", "1021")            # the bank accounts; bill payments credit 1020
# the VAT bexio moves on payment, (debit, credit): input VAT from its clearing account to the Vorsteuer account of its
# kind, and on a reverse-charge bill the output VAT to the Bezugsteuer account. ERPNext's Purchase Invoice books both at
# the bill date already, so none of them is a Journal Entry here
VAT_MOVES = (("1170", "1172"), ("1171", "1172"), ("2202", "2203"))
# ERPNext rounds a CHF invoice total to the rappen step (the Rounded Total): a bill of bexio's 703.11 is outstanding
# 703.10 in ERPNext. The gap up to ROUNDING is left unallocated on the Payment Entry, so the bank still gets bexio's
# amount; a larger gap is not a rounding and is not booked
ROUNDING = Decimal("0.05")
OVER_ALLOCATED = "payment exceeds the outstanding amount of its Purchase Invoice"
UNMATCHED_FILE = "bexio-payments-out-unmatched.txt"
VAT_IDS_FILE = "bexio-vat-on-payment-out-ids.txt"


class Unmapped(Exception):
    """A payment the importer does not map. The message names the reason, never a company name."""


class Lookups:
    """What the mapping reads from ERPNext: the submitted Purchase Invoices by bexio_id, the accounts by number, the cost
    center, and the bexio ids of the submitted Payment Entries already loaded (a rerun leaves them as they are)."""

    def __init__(self, invoices, gl, cost_center, loaded=frozenset()):
        self.invoices = invoices        # bexio bill id -> the submitted Purchase Invoice (name, supplier, credit_to, currency, grand_total, outstanding)
        self.gl = gl                    # account number -> Account name of the company
        self.cost_center = cost_center  # the one Cost Center of the company; None when there is not exactly one
        self.loaded = set(loaded)       # bexio ids (bill payment group uuids) of the submitted Payment Entries of an earlier run

    @classmethod
    def from_erp(cls, erp):
        invoices = erp.list("Purchase Invoice", [["company", "=", im.COMPANY], ["docstatus", "=", 1], ["bexio_id", "is", "set"]],
                            ["name", "bexio_id", "supplier", "credit_to", "currency", "grand_total", "outstanding_amount"])
        accounts = erp.list("Account", [["company", "=", im.COMPANY], ["is_group", "=", 0]], ["name", "account_number"])
        centers = erp.list("Cost Center", [["company", "=", im.COMPANY], ["is_group", "=", 0]], ["name"])
        loaded = erp.list("Payment Entry", [["company", "=", im.COMPANY], ["payment_type", "=", "Pay"], ["docstatus", "=", 1],
                                            ["bexio_id", "is", "set"]], ["bexio_id"])
        return cls(
            invoices={r["bexio_id"]: r for r in invoices},
            gl={r["account_number"]: r["name"] for r in accounts},
            cost_center=centers[0]["name"] if len(centers) == 1 else None,
            loaded={r["bexio_id"] for r in loaded},
        )


def _money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def groups_of(journal):
    """The bill payments: the KbClientAccountEntry lines of the journal, grouped by ref_uuid, in journal order."""
    groups = collections.OrderedDict()
    for line in journal:
        if line.get("ref_class") == "KbClientAccountEntry" and line.get("ref_uuid"):
            groups.setdefault(line["ref_uuid"], []).append(line)
    return groups


def bill_bookings(journal, numbers):
    """What bexio booked each bill at in CHF: bill uuid -> the credit on the payables account, as cents."""
    booked = collections.defaultdict(lambda: ZERO)
    for line in journal:
        if line.get("ref_class") != "KbBill":
            continue
        base = Decimal(str(line["base_currency_amount"]))
        if numbers.get(line["credit_account_id"]) == PAYABLE:
            booked[line["ref_uuid"]] += base
        if numbers.get(line["debit_account_id"]) == PAYABLE:
            booked[line["ref_uuid"]] -= base
    return {uuid: _money(amount) for uuid, amount in booked.items()}


def settle(uuid, lines, numbers):
    """What one bill payment settles: payable (the debit on 2000), bank (the credit on the bank), the bank's account
    number, the deductions (account number, amount), the VAT lines it moves, and its day. Raises Unmapped.
    """
    payable = bank = ZERO
    bank_account, days, deductions, vat = None, set(), [], []
    for line in lines:
        debit, credit = numbers.get(line["debit_account_id"]), numbers.get(line["credit_account_id"])
        amount = _money(line["base_currency_amount"])
        days.add(line["date"][:10])
        if (debit, credit) in VAT_MOVES:
            vat.append(line)
        elif amount == ZERO:
            continue  # a zero line moves nothing
        elif debit == PAYABLE and credit in BANKS:
            payable += amount
            bank += amount
            bank_account = credit
        elif debit == PAYABLE:
            payable += amount
            deductions.append((credit, amount))
        else:
            raise Unmapped("a journal line of the payment has no home: debit {}, credit {}".format(debit, credit))
    if bank_account is None:
        raise Unmapped("no credit on a bank account")
    if len(days) != 1:
        raise Unmapped("the lines of the payment are on different days")
    return {"uuid": uuid, "day": days.pop(), "payable": payable, "bank": bank, "bank_account": bank_account,
            "deductions": deductions, "vat": vat}


def pair(settled, booked, bill_dates):
    """Pair each settled payment with the bill it pays: payments and bills of one CHF amount, in date order, one to one.

    Returns (pairs, unpaired payments, open bills). A payment with no bill of its amount is unpaired; a bill with no
    payment is open, and it stays open in ERPNext.
    """
    by_amount = collections.defaultdict(lambda: ([], []))
    for item in settled:
        by_amount[item["payable"]][0].append(item)
    for uuid, amount in booked.items():
        by_amount[amount][1].append((bill_dates.get(uuid, ""), uuid))
    pairs, unpaired, open_bills = [], [], []
    for payments, bills in by_amount.values():
        payments.sort(key=lambda item: (item["day"], item["uuid"]))
        bills.sort()
        pairs.extend(zip(payments, (uuid for _, uuid in bills)))
        unpaired.extend(payments[len(bills):])
        open_bills.extend(uuid for _, uuid in bills[len(payments):])
    return pairs, unpaired, open_bills


def payment_entry(item, bill_uuid, lookups):
    """The Payment Entry dict (pay) of a settled payment against its bill's Purchase Invoice. Raises Unmapped."""
    invoice = lookups.invoices.get(bill_uuid)
    if invoice is None:
        raise Unmapped("the bill has no submitted Purchase Invoice in ERPNext")
    if invoice["currency"] != CURRENCY:
        raise Unmapped("Purchase Invoice in a currency other than {}".format(CURRENCY))
    if _money(invoice["grand_total"]) != item["payable"]:
        raise Unmapped("the Purchase Invoice's total differs from bexio's booking of the bill")
    owing = _money(invoice["outstanding_amount"])
    allocated = item["payable"]
    if allocated > owing:
        if item["deductions"] or allocated - owing > ROUNDING:
            raise Unmapped(OVER_ALLOCATED)
        allocated = owing  # the rounding gap stays unallocated, see ROUNDING
    bank = lookups.gl.get(item["bank_account"])
    if bank is None:
        raise Unmapped("no Account {} for the bank".format(item["bank_account"]))
    doc = {
        "doctype": "Payment Entry", "company": im.COMPANY, "payment_type": "Pay",
        "party_type": "Supplier", "party": invoice["supplier"], "posting_date": item["day"],
        "paid_from": bank, "paid_from_account_currency": CURRENCY,
        "paid_to": invoice["credit_to"], "paid_to_account_currency": CURRENCY,
        "paid_amount": float(item["bank"]), "received_amount": float(item["bank"]),
        "unallocated_amount": float(item["bank"] - allocated),
        "source_exchange_rate": 1.0, "target_exchange_rate": 1.0,
        "reference_no": "bexio payment {}".format(item["uuid"]), "reference_date": item["day"],
        "references": [{
            "reference_doctype": "Purchase Invoice", "reference_name": invoice["name"],
            "allocated_amount": float(allocated), "total_amount": float(invoice["grand_total"]),
            "outstanding_amount": float(owing),
        }],
        "bexio_id": item["uuid"],
    }
    if item["deductions"]:
        if lookups.cost_center is None:
            raise Unmapped("no single Cost Center of the company for a deduction")
        doc["deductions"] = []
        for number, amount in item["deductions"]:
            account = lookups.gl.get(number)
            if account is None:
                raise Unmapped("no Account {} for the deduction".format(number))
            # ERPNext takes a deduction on a Pay as a debit: paid - allocated - deductions must be zero. bexio's discount
            # is a credit on its account, so the row carries it negative
            doc["deductions"].append({"account": account, "cost_center": lookups.cost_center, "amount": float(-amount)})
    return doc


def bill_dates_of(bills):
    """The bill_date of each bill of the export, by bill uuid: the order the pairs are made in."""
    return {b["id"]: b["bill_date"] for b in bills}


def plan(data, lookups):
    """Settle, pair and map every bill payment of the export; one result per group, and the open bills.

    A result has the group's uuid, its day and year, the payable, the bank amount, the bill it is paired with, the
    Payment Entry (doc) or the reason it is unmapped (error), and the VAT lines it moves (not booked here). A payment
    whose submitted Payment Entry an earlier run loaded has no doc: it is counted as loaded, and not checked again.
    """
    numbers = {a["id"]: str(a["account_no"]) for a in data["accounts"]}
    settled, results = [], []
    for uuid, lines in groups_of(data["journal"]).items():
        try:
            settled.append(settle(uuid, lines, numbers))
        except Unmapped as err:
            results.append({"uuid": uuid, "day": lines[0]["date"][:10], "error": str(err), "vat": []})
    booked = bill_bookings(data["journal"], numbers)
    bill_dates = bill_dates_of(data["bills"])
    pairs, unpaired, open_bills = pair(settled, booked, bill_dates)
    for item in unpaired:
        results.append({"uuid": item["uuid"], "day": item["day"], "payable": item["payable"], "bank": item["bank"],
                        "error": "no bill of this amount in bexio's journal", "vat": item["vat"]})
    for item, bill_uuid in pairs:
        result = {"uuid": item["uuid"], "day": item["day"], "bill": bill_uuid, "bill_date": bill_dates[bill_uuid],
                  "payable": item["payable"], "bank": item["bank"], "deducted": sum((amount for _, amount in item["deductions"]), ZERO),
                  "vat": item["vat"], "doc": None, "error": None, "loaded": item["uuid"] in lookups.loaded}
        if not result["loaded"]:
            try:
                result["doc"] = payment_entry(item, bill_uuid, lookups)
            except Unmapped as err:
                result["error"] = str(err)
        results.append(result)
    return results, open_bills


def vat_lines(results):
    """The VAT lines of every group, mapped or not: each is in the Purchase Invoice's GL already, so none is booked here."""
    return [line for r in results for line in r["vat"]]


def summary(results, open_bills):
    """Totals per year of the payment date, the reasons, and the open bills; no names, no bexio ids."""
    lines = ["{:<8}{:>10}{:>8}{:>10}{:>18}{:>14}".format("year", "payments", "mapped", "unmapped", "CHF paid", "deducted")]
    per_year = collections.OrderedDict()
    for r in sorted(results, key=lambda r: r["day"]):
        acc = per_year.setdefault(r["day"][:4], [0, 0, ZERO, ZERO])
        acc[0] += 1
        if r.get("doc") or r.get("loaded"):
            acc[1] += 1
            acc[2] += r["bank"]
            acc[3] += r["deducted"]
    for year, (count, mapped, paid, deducted) in per_year.items():
        lines.append("{:<8}{:>10}{:>8}{:>10}{:>18,.2f}{:>14,.2f}".format(year, count, mapped, count - mapped, paid, deducted))
    lines.append("{:<8}{:>10}{:>8}{:>10}{:>18,.2f}{:>14,.2f}".format(
        "total", len(results), sum(a[1] for a in per_year.values()), sum(a[0] - a[1] for a in per_year.values()),
        sum(a[2] for a in per_year.values()), sum(a[3] for a in per_year.values())))
    lines.append("VAT lines moved on payment, not booked here (in the Purchase Invoices already): {}".format(len(vat_lines(results))))
    lines.append("already submitted in ERPNext by an earlier run, not handed over again: {}".format(
        sum(1 for r in results if r.get("loaded"))))
    lines.append("bills with no payment in bexio, left open in ERPNext: {}".format(len(open_bills)))
    unmapped = [r for r in results if r["error"]]
    if unmapped:
        lines.append("")
        lines.append("unmapped by reason:")
        counts = collections.Counter(r["error"] for r in unmapped)
        for reason, count in counts.most_common():
            lines.append("  {:>4}  {}".format(count, reason))
    return "\n".join(lines)


def detail_lines(results, open_bills):
    """The unmapped payments and the open bills by bexio id, and the VAT lines, for the private file."""
    lines = ["group {} ({}): unmapped: {}".format(r["uuid"], r["day"], r["error"]) for r in results if r["error"]]
    lines += ["bill {}: open, no payment in bexio's journal".format(uuid) for uuid in open_bills]
    lines += ["group {} ({}): paid before its bill's date {}".format(r["uuid"], r["day"], r["bill_date"]) for r in results
              if r.get("bill") and r["day"] < r["bill_date"]]
    return lines


def load_export(path):
    """The journal, the accounts and the bills of the export."""
    data = {}
    for name in ("journal", "accounts", "bills"):
        with open(os.path.join(path, name + ".json"), encoding="utf-8") as f:
            data[name] = json.load(f)
    return data


def main(argv):
    parser = argparse.ArgumentParser(description="Map the bexio bill payments to ERPNext Payment Entries (pay).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals")
    parser.add_argument("--write", metavar="FILE", default=None,
                        help="write the Payment Entries to a private file for the loader (bexio-drafts.sh FILE submit); "
                             "nothing goes into ERPNext here. Stops, writing nothing, if a payment would over-allocate")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if args.dry_run == (args.write is not None):
        parser.error("exactly one of --dry-run and --write FILE")

    export_dir = args.export or im.newest_export()
    data = load_export(export_dir)
    try:
        lookups = Lookups.from_erp(im.Erp.from_file(args.token_file))
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2

    results, open_bills = plan(data, lookups)
    lines = detail_lines(results, open_bills)
    print("payments from {}".format(export_dir))
    print(summary(results, open_bills))
    ip.write_private(os.path.join(im.PRIVATE, UNMATCHED_FILE), lines or ["none"])
    ip.write_private(os.path.join(im.PRIVATE, VAT_IDS_FILE), [
        "journal {} group {}: not booked, the VAT is in the Purchase Invoice already".format(line["id"], line["ref_uuid"])
        for line in vat_lines(results)] or ["none"])
    over = [r["uuid"] for r in results if r["error"] == OVER_ALLOCATED]
    if args.write and over:
        # the rule of the live run: a payment that would over-allocate stops the whole run, nothing partial
        print("stop: {} payment(s) would over-allocate their Purchase Invoice, by bexio id: {}; nothing written".format(
            len(over), ", ".join(over)))
        return 1
    if args.write:
        documents = [{"doctype": "Payment Entry", "name": None, "bexio_id": r["uuid"], "values": r["doc"]}
                     for r in results if r.get("doc")]
        for document in documents:
            document["values"] = {k: v for k, v in document["values"].items() if k != "doctype"}
        ip.write_private(args.write, [json.dumps({"documents": documents, "exchange_rates": []}, indent=1)])
        print("{} Payment Entries handed to the loader in {}; {} payments listed by bexio id in {}".format(
            len(documents), args.write, sum(1 for r in results if r["error"]), os.path.join(im.PRIVATE, UNMATCHED_FILE)))
    else:
        print("dry run: nothing was written; the lines of unmapped payments and open bills are in {}".format(
            os.path.join(im.PRIVATE, UNMATCHED_FILE)))
    return 1 if any(r["error"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
