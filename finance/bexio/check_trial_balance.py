"""Compare ERPNext's trial balance per business year and account with bexio's journal.

The closing check of the accounting history (erp-a2ma). For every business year of the export
and every account (by bexio_id), bexio's figure comes from journal.json and ERPNext's from its
GL Entry rows (is_cancelled 0, read through the API user):

- balance-sheet accounts (roots 1 and 2): the closing balance at the year's end;
- profit-and-loss accounts (roots 3 to 9): the net movement within the year. ERPNext has no
  Period Closing Voucher, so its P&L balances accumulate across years by design; bexio's
  year-start carry-forward lines (posting_plan's carry_forward bucket) and every line on
  account 9100 are left out on bexio's side. ERPNext's copies of those carry-forward entries
  are left out on its side too: every voucher with a leg on 9100 (the year-start Journal
  Entries of 2970 and 9100), so both sides compare the same postings.

The output of a run goes to a private CSV, one row per year and account. Stdout carries
totals only: years, accounts, rows, rows with a difference, and the band of the largest one.
Nothing here writes to ERPNext.

Run it as:

    python3 finance/bexio/check_trial_balance.py [--export DIR] [--out CSV]

--export defaults to the newest directory under <private>/bexio-export/, --out to
<private>/bexio-trial-balance-<today>.csv. Standard library only, plus import_master and posting_plan.
"""

import argparse
import collections
import csv
import datetime
import json
import os
import sys
from decimal import Decimal

import import_master as im
import posting_plan as pp

TOLERANCE = Decimal("0.005")  # a difference of a rappen or less is rounding, not a difference
CLOSING_ROOTS = ("1", "2")    # balance-sheet accounts compare the closing balance
CLOSING_ACCOUNT_NO = "9100"   # bexio's closing account: its lines are left out on bexio's side
BANDS = ((Decimal("0.005"), "up to a rappen"), (Decimal("1"), "up to 1 CHF"), (None, "above 1 CHF"))
FIELDS = ["year", "account_no", "bexio_id", "erp_account", "basis", "bexio", "erpnext", "difference"]


def load_years(export_dir):
    """The business years of the export, oldest first: [{id, start, end}]."""
    with open(os.path.join(export_dir, "business_years.json"), encoding="utf-8") as f:
        years = json.load(f)
    return sorted(({"id": y["id"], "start": y["start"], "end": y["end"]} for y in years), key=lambda y: y["start"])


def year_of(date, years):
    """The id of the business year a date (YYYY-MM-DD, possibly with a time) falls in, or None."""
    day = date[:10]
    for y in years:
        if y["start"] <= day <= y["end"]:
            return y["id"]
    return None


def _movements(entries, years):
    """{(year id, key): net movement} from (date, key, amount) entries, plus the dates outside every year."""
    moves = collections.defaultdict(Decimal)
    outside = []
    for date, key, amount in entries:
        year = year_of(date, years)
        if year is None:
            outside.append(date[:10])
            continue
        moves[(year, key)] += amount
    return moves, outside


def bexio_entries(data, buckets, accounts):
    """(date, bexio account id, amount) for every journal line that bexio's side counts: debit positive, credit negative."""
    closing_ids = {str(a["id"]) for a in accounts if a["account_no"] == CLOSING_ACCOUNT_NO}
    entries = []
    for line in data["journal"]:
        if buckets[line["id"]] == "carry_forward":
            continue
        debit, credit = str(line["debit_account_id"]), str(line["credit_account_id"])
        if debit in closing_ids or credit in closing_ids:
            continue
        amount = Decimal(str(line["base_currency_amount"]))
        entries.append((line["date"], debit, amount))
        entries.append((line["date"], credit, -amount))
    return entries


def erp_entries(gl_rows, erp_bexio_id, closing_ids):
    """(posting date, key, amount) per GL row: the bexio id of its account, or ("erp", account name) when it has none.

    A voucher with a leg on a closing account (closing_ids: bexio ids of 9100) is left out whole, as bexio's
    carry-forward lines are on bexio's side.
    """
    closing_vouchers = {row["voucher_no"] for row in gl_rows if erp_bexio_id.get(row["account"]) in closing_ids}
    entries = []
    for row in gl_rows:
        if row["voucher_no"] in closing_vouchers:
            continue
        key = erp_bexio_id.get(row["account"]) or ("erp", row["account"])
        amount = Decimal(str(row["debit"] or 0)) - Decimal(str(row["credit"] or 0))
        entries.append((row["posting_date"], key, amount))
    return entries


def balances(entries, years):
    """{(year id, key): (movement, closing)} for every key with an entry; closing is the running total to the year's end."""
    moves, outside = _movements(entries, years)
    keys = {key for (_, key) in moves}
    result = {}
    running = collections.defaultdict(Decimal)
    for y in years:
        for key in keys:
            movement = moves.get((y["id"], key), Decimal("0"))
            running[key] += movement
            result[(y["id"], key)] = (movement, running[key])
    return result, outside


def compare(bexio, erp, years, accounts):
    """One row per year and account: bexio's and ERPNext's figure (the closing balance or the movement) and their difference.

    accounts: {bexio id: account_no}, the chart of the export; an account of ERPNext without a bexio id is compared against zero.
    """
    rows = []
    keys = {key for (_, key) in list(bexio) + list(erp)}
    for y in years:
        for key in sorted(keys, key=str):
            b_move, b_close = bexio.get((y["id"], key), (Decimal("0"), Decimal("0")))
            e_move, e_close = erp.get((y["id"], key), (Decimal("0"), Decimal("0")))
            if isinstance(key, tuple):
                account_no, erp_account, bexio_id = "", key[1], ""
            else:
                account_no, erp_account, bexio_id = accounts.get(key, ""), "", key
            basis = "closing" if account_no[:1] in CLOSING_ROOTS else "movement"
            want, have = (b_close, e_close) if basis == "closing" else (b_move, e_move)
            if want == 0 and have == 0:
                continue
            rows.append({
                "year": y["id"], "account_no": account_no, "bexio_id": bexio_id, "erp_account": erp_account,
                "basis": basis, "bexio": want, "erpnext": have, "difference": have - want,
            })
    return rows


def band(difference):
    magnitude = abs(difference)
    for limit, name in BANDS:
        if limit is None or magnitude <= limit:
            return name
    raise AssertionError("unreachable")


def summary(rows, years, accounts_compared):
    """The totals of a run as lines for stdout: counts only, no amounts."""
    differing = [r for r in rows if abs(r["difference"]) > TOLERANCE]
    lines = [
        "years compared: {}".format(len(years)),
        "accounts compared: {}".format(accounts_compared),
        "rows (year and account) compared: {}".format(len(rows)),
        "rows with a difference: {}".format(len(differing)),
        "accounts with a difference: {}".format(len({r["bexio_id"] or r["erp_account"] for r in differing})),
    ]
    if differing:
        largest = max(differing, key=lambda r: abs(r["difference"]))
        lines.append("largest difference: {} (rows in that band: {})".format(
            band(largest["difference"]),
            sum(1 for r in differing if band(r["difference"]) == band(largest["difference"]))))
    else:
        lines.append("largest difference: none")
    return "\n".join(lines)


def write_csv(path, rows):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r, bexio=format(r["bexio"], "f"), erpnext=format(r["erpnext"], "f"), difference=format(r["difference"], "f")))


def main(argv):
    parser = argparse.ArgumentParser(description="Compare ERPNext's trial balance per business year and account with bexio's journal.")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--out", default=None, help="CSV for the rows (default: <private>/bexio-trial-balance-<today>.csv)")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)

    export_dir = args.export or im.newest_export()
    out = args.out or os.path.join(im.PRIVATE, "bexio-trial-balance-{}.csv".format(datetime.date.today().isoformat()))
    years = load_years(export_dir)
    with open(os.path.join(export_dir, "accounts.json"), encoding="utf-8") as f:
        accounts_json = json.load(f)
    accounts = {str(a["id"]): a["account_no"] for a in accounts_json}
    data = pp.load(export_dir)
    buckets = pp.classify(data)

    bexio, outside_bexio = balances(bexio_entries(data, buckets, accounts_json), years)

    erp = im.Erp.from_file(args.token_file)
    try:
        erp_bexio_id = {r["name"]: r["bexio_id"] for r in erp.list("Account", [["bexio_id", "is", "set"]], ["name", "bexio_id"])}
        gl = erp.list("GL Entry", [["is_cancelled", "=", 0]], ["account", "debit", "credit", "posting_date", "voucher_no"])
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    closing_ids = {str(a["id"]) for a in accounts_json if a["account_no"] == CLOSING_ACCOUNT_NO}
    erp_side, outside_erp = balances(erp_entries(gl, erp_bexio_id, closing_ids), years)

    rows = compare(bexio, erp_side, years, accounts)
    write_csv(out, rows)
    if outside_bexio or outside_erp:
        print("dates outside every business year: bexio {}, ERPNext {}".format(len(outside_bexio), len(outside_erp)))
    compared = len({r["bexio_id"] or r["erp_account"] for r in rows})
    print(summary(rows, years, compared))
    print("CSV written to {}".format(out))
    return 1 if any(abs(r["difference"]) > TOLERANCE for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
