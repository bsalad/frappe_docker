"""Compare bexio's VAT postings with ERPNext's, per quarter, on the accounts that carry the VAT on payment.

bexio declares on the effective method (vereinnahmte Entgelte): the sales VAT moves from 2202 to 2200 on the
receipt date, the purchase VAT from 1172 to 1170 or 1171 on the payment date. The net of 2200 and of 1170 and 1171
is zero in a settled quarter, because bexio's settlement clears it, so the gross flows are compared too: the credits
of 2200 and the debits of 1170 and 1171 per quarter, and the debits of 2202 and the credits of 1172 (transit). Both
sides are compared here account by account and quarter by quarter: bexio's from the journal of the export (read
only), ERPNext's from its General Ledger (read only). On ERPNext's side the gross leaves out the invoice-date vouchers
that bexio books to 2202 and 1172. 2203 (Bezugsteuer) is compared on the net only.

The per-rate Ziffern (302 to 343, 200) are not in the journal: bexio's invoices carry them, and they are not compared
here. See the docs of erp-agpf's report for what remains.

Run it as:

    python3 finance/bexio/mwst_compare.py

The detail goes to one private CSV under <private>; the stdout shows counts only. Standard library only, plus
import_master for the ERPNext client.
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal

import import_master as im
import posting_plan as pp

COMPANY = im.COMPANY
CENT = Decimal("0.01")
ZERO = Decimal("0")

# The accounts that carry the VAT on payment, by number, compared net (debit minus credit) per quarter: the net of 2200,
# 1170 and 1171 is zero in a settled quarter, so it proves little on its own, and it stays as the second column.
COMPARED = ("2200", "1170", "1171", "2203")
ALL = COMPARED + ("2202", "1172")

# The gross side per account: the flows that bexio's journal holds in full. 2200 is credited by the receipt's move from
# 2202, 1170 and 1171 are debited by the move from 1172 and by the direct and manual input tax; 2202 and 1172 (transit)
# are debited and credited by those moves only. Their other side is left out: bexio books it at the invoice and bill
# date, and the export's journal holds only the manual part of it.
GROSS = {"2200": "credit", "1170": "debit", "1171": "debit", "2202": "debit", "1172": "credit"}

# ERPNext books the invoice-date VAT on these vouchers (2200, 1170, 1171); bexio books it to 2202 and 1172 instead,
# so the ERPNext side of the gross leaves them out.
INVOICE_VOUCHER = {"2200": "Sales Invoice", "1170": "Purchase Invoice", "1171": "Purchase Invoice"}

JOURNAL = os.path.join(im.PRIVATE, "bexio-export", "2026-10-10-complete", "journal.json")
ACCOUNTS = os.path.join(im.PRIVATE, "bexio-export", "2026-10-10-complete", "accounts.json")


def quarter_of(day):
    """'2026-08-14' -> '2026Q3'."""
    return "{}Q{}".format(day[:4], (int(day[5:7]) - 1) // 3 + 1)


def numbers_by_id(accounts):
    """bexio account id -> account number, for the accounts this check compares (net or gross)."""
    return {a["id"]: str(a["account_no"]) for a in accounts if str(a["account_no"]) in ALL}


def is_carry_forward(line):
    """The 1 January line that carries last year's closing balance into the new year (posting_plan's test)."""
    return line["date"][5:10] == "01-01" and any(word in line["description"] for word in pp.CARRY_FORWARD_WORDS)


def post(out, quarter, number, side, amount):
    """Adds one leg of a posting to the balances of its account: the net (debit minus credit) for the net accounts,
    and the amount of its side for the gross accounts, where that side is the one this check keeps."""
    if number in COMPARED:
        out[(quarter, number, "net")] += amount if side == "debit" else -amount
    if GROSS.get(number) == side:
        out[(quarter, number, "gross")] += amount


def bexio_balances(journal, numbers):
    """Per (quarter, account number, measure) of the journal lines on the compared accounts, in CHF; the measure is
    'net' or 'gross'. A foreign-currency line gives its CHF in base_currency_amount, as ERPNext books it; each line is
    rounded to the rappen, as ERPNext posts it. A carry-forward line is left out: ERPNext holds the closing balance it
    repeats."""
    out = defaultdict(lambda: ZERO)
    for line in journal:
        if is_carry_forward(line):
            continue
        quarter = quarter_of(line["date"][:10])
        amount = Decimal(str(line["base_currency_amount"])).quantize(CENT, rounding=ROUND_HALF_UP)
        if line["debit_account_id"] in numbers:
            post(out, quarter, numbers[line["debit_account_id"]], "debit", amount)
        if line["credit_account_id"] in numbers:
            post(out, quarter, numbers[line["credit_account_id"]], "credit", amount)
    return dict(out)


def erp_balances(entries, numbers_by_name):
    """Per (quarter, account number, measure) of the GL entries on the compared accounts. The gross side leaves out
    the invoice-date vouchers that bexio books to 2202 and 1172 instead (INVOICE_VOUCHER); the net keeps all."""
    out = defaultdict(lambda: ZERO)
    for e in entries:
        number = numbers_by_name.get(e["account"])
        if number is None:
            continue
        quarter = quarter_of(str(e["posting_date"])[:10])
        debit = Decimal(str(e["debit"]))
        credit = Decimal(str(e["credit"]))
        if number in COMPARED:
            out[(quarter, number, "net")] += debit - credit
        if number in GROSS and e.get("voucher_type") != INVOICE_VOUCHER.get(number):
            out[(quarter, number, "gross")] += debit if GROSS[number] == "debit" else credit
    return dict(out)


def compare(bexio, erp):
    """One row per (quarter, account, measure) on either side: (quarter, number, measure, bexio, erp, difference), all
    to the cent."""
    rows = []
    for key in sorted(set(bexio) | set(erp)):
        b = bexio.get(key, ZERO).quantize(CENT, rounding=ROUND_HALF_UP)
        e = erp.get(key, ZERO).quantize(CENT, rounding=ROUND_HALF_UP)
        rows.append((key[0], key[1], key[2], b, e, e - b))
    return rows


def differences(rows):
    """The rows whose two sides differ by at least a rappen."""
    return [r for r in rows if r[5] != ZERO]


def write_csv(path, rows):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["quarter", "account", "measure", "bexio_chf", "erpnext_chf", "difference_chf"])
        for quarter, number, measure, b, e, d in rows:
            writer.writerow([quarter, number, measure, "{:.2f}".format(b), "{:.2f}".format(e), "{:.2f}".format(d)])


def summary_lines(rows):
    """Counts only: the stdout names no amounts and no accounts' balances."""
    quarters = {r[0] for r in rows}
    bad = differences(rows)
    return [
        "quarters compared: {}".format(len(quarters)),
        "comparisons compared: {} (net of {} accounts, gross of {})".format(len(rows), len(COMPARED), len(GROSS)),
        "comparisons with a difference: {}".format(len(bad)),
        "quarters with a difference: {}".format(len({r[0] for r in bad})),
    ]


def main(argv):
    parser = argparse.ArgumentParser(description="bexio's VAT postings per quarter against ERPNext's (read only).")
    parser.add_argument("--journal", default=JOURNAL)
    parser.add_argument("--accounts", default=ACCOUNTS)
    parser.add_argument("--report", default=os.path.join(im.PRIVATE, "bexio-mwst-compare.csv"))
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)

    with open(args.journal, encoding="utf-8") as f:
        journal = json.load(f)
    with open(args.accounts, encoding="utf-8") as f:
        numbers = numbers_by_id(json.load(f))
    bexio = bexio_balances(journal, numbers)

    erp = im.Erp.from_file(args.token_file)
    try:
        accounts = erp.list("Account", [["company", "=", COMPANY], ["account_number", "in", list(ALL)]],
                            ["name", "account_number"])
        names = {a["name"]: str(a["account_number"]) for a in accounts}
        entries = erp.list("GL Entry", [["account", "in", list(names)], ["is_cancelled", "=", 0]],
                           ["account", "voucher_type", "posting_date", "debit", "credit"]) if names else []
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2

    rows = compare(bexio, erp_balances(entries, names))
    for line in summary_lines(rows):
        print(line)
    write_csv(args.report, rows)
    print("written: {}".format(args.report))
    return 1 if differences(rows) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
