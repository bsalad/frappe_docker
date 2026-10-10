"""Fill Bank Transaction.booking_date on the lines imported before the field existed, from bexio's book_date.

import_bank.py now stores the booking date on new lines; the lines already in ERPNext have none, so the Desk list
(sorted by Booking Date) would show them last and blank. This reads the export's bank_transactions.json and the
Bank Transactions with a bexio_id, and for each line without a Booking Date takes bexio's book_date (the value date
when the export has none, as import_bank does). A line that has one is left alone, so a second run changes nothing.

    python3 finance/bexio/backfill_booking_date.py --dry-run [--export DIR] [--field value_date]
    python3 finance/bexio/backfill_booking_date.py --write [--export DIR] [--field value_date]

--field says which date to fill: booking_date (the default) or value_date, which is the export's value date for each
line. --dry-run reads ERPNext and prints counts only. --write sets the dates through bi_finance.bank_list.set_booking_dates
(a plain set_value, no Comment per line, `modified` unchanged), in batches; run it only after the dry run and the go.
Standard library only, plus import_master.
"""

import argparse
import collections
import json
import os
import sys

import import_master as im

BATCH = 200


def booking_date(record):
    """import_bank's rule: the day of book_date, else the value date."""
    return (record.get("book_date") or "")[:10] or record["value_date"]


def export_date(record, field):
    """The date the field takes from the export: the booking date by import_bank's rule, the value date as it is."""
    return booking_date(record) if field == "booking_date" else record["value_date"]


def plan(records, rows, field="booking_date"):
    """The dates to fill and the counts. rows: the live Bank Transactions (name, bexio_id, and the field)."""
    export = {str(r["id"]): export_date(r, field) for r in records}
    dates = {}
    counts = collections.Counter(lines=len(rows))
    for row in rows:
        if row.get(field):
            counts["already_set"] += 1
        elif str(row["bexio_id"]) in export:
            dates[row["name"]] = export[str(row["bexio_id"])]
        else:
            counts["not_in_export"] += 1
    counts["to_fill"] = len(dates)
    # the lines whose booking date is not their value date: the ones the two Desk columns show differently
    by_id = {str(r["id"]): r for r in records}
    counts["differs"] = sum(
        1 for row in rows
        if str(row["bexio_id"]) in by_id and booking_date(by_id[str(row["bexio_id"])]) != by_id[str(row["bexio_id"])]["value_date"])
    return dates, counts


def summary(counts, field="booking_date"):
    return "\n".join([
        "bank transactions with a bexio id: {}".format(counts["lines"]),
        "{} already set: {}".format(field.replace("_", " "), counts["already_set"]),
        "not in the export: {}".format(counts["not_in_export"]),
        "to fill: {}".format(counts["to_fill"]),
        "booking date differs from value date: {}".format(counts["differs"]),
    ])


def write(erp, dates, field="booking_date"):
    """Hand the dates to ERPNext in batches; returns the number it filled."""
    names = sorted(dates)
    filled = 0
    for i in range(0, len(names), BATCH):
        chunk = {n: dates[n] for n in names[i:i + BATCH]}
        filled += erp.call("bi_finance.bank_list.set_booking_dates", dates=json.dumps(chunk), field=field)
    return filled


def main(argv):
    parser = argparse.ArgumentParser(description="Fill the Booking Date of the imported Bank Transactions from bexio's book_date.")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the counts")
    parser.add_argument("--write", action="store_true", help="set the dates in ERPNext (after the dry run and the go)")
    parser.add_argument("--field", choices=["booking_date", "value_date"], default="booking_date", help="the date to fill (default: booking_date)")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if args.dry_run == args.write:
        parser.error("give --dry-run or --write")

    export_dir = args.export or im.newest_export()
    file = os.path.join(export_dir, "bank_transactions.json")
    if not os.path.exists(file):
        print("bank transactions: not exported yet (no bank_transactions.json in the export)")
        return 0
    with open(file, encoding="utf-8") as f:
        records = json.load(f)
    erp = im.Erp.from_file(args.token_file)
    try:
        rows = erp.list("Bank Transaction", [["bexio_id", "is", "set"]], ["name", "bexio_id", args.field])
        dates, counts = plan(records, rows, args.field)
        print(summary(counts, args.field))
        if args.write:
            print("filled: {}".format(write(erp, dates, args.field)))
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
