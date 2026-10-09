"""Build the Opening Journal Entry of bexio's first business year, offline.

Step 2b-6 of the bexio pipeline. The opening bookings of the first business
year (journal lines of type opening, usually against 9100 Eröffnungsbilanz and
9900 Korrekturen) become ONE Journal Entry in ERPNext: voucher_type Opening
Entry, is_opening Yes, posted on the first day of that business year. It is
keyed by bexio_id "opening-<business year id>", so a rerun finds it again.

Every line goes to the ERPNext account with the same number, as in import_master:
bexio's 9100 Eröffnungsbilanz and 9900 Korrekturen are already accounts of the
chart (Equity), so the equity side needs no mapping of its own. A missing account
is unmapped, never created here. ERPNext's "Temporary Opening" is not used: this
site's chart has no such account.

Two cases build no entry, and the dry run says which one applies:
- the export has no journal (the journal call needs the accounting scope, which
  the bexio login does not have yet; see README), so the opening bookings are
  unknown;
- the journal has no opening lines in the first year (the company started in
  bexio), so no opening entry is needed.

Nothing is written here. --dry-run reads ERPNext (the Temporary Opening account
and the Opening Entries already there) and prints what the export gives. The
live run is erp-a2ma's, after the posting plan (finance-3qsp).

Run it as:

    python3 finance/bexio/import_opening.py --dry-run [--export DIR]

--export defaults to the newest directory under <private>/bexio-export/. The
output is counts and dates only, no company data. Standard library only, plus
import_master.
"""

import argparse
import json
import os
import sys
from decimal import Decimal

import import_master as im

COMPANY = im.COMPANY
OPENING_ENTRY = "Opening Entry"
# bexio's clearing accounts of the opening balance, equity accounts of the chart
OPENING_ACCOUNTS = ("9100", "9900")
ZERO = Decimal("0")


class Unmapped(Exception):
    """An opening line or year the importer does not map. The message names the reason, never a company name."""


def _dec(value):
    return Decimal(str(value))


def first_year(years):
    """The earliest business year by start date: bexio's ids are not in date order."""
    if not years:
        raise Unmapped("business_years.json has no business year")
    return min(years, key=lambda y: y["start"])


def journal_status(export_dir):
    """Whether the export has the journal, and if not, what the manifest says about it."""
    if os.path.exists(os.path.join(export_dir, "journal.json")):
        return "exported"
    with open(os.path.join(export_dir, "manifest.json"), encoding="utf-8") as f:
        entry = json.load(f)["entities"].get("journal")
    if entry is None:
        return "not in the export"
    return entry.get("error") or entry["status"]


def survey(export_dir):
    """What the export says about the opening: the business years, the first one, the journal."""
    with open(os.path.join(export_dir, "business_years.json"), encoding="utf-8") as f:
        years = json.load(f)
    return {
        "years": len(years),
        "closed": sum(1 for y in years if y["status"] == "closed"),
        "first": first_year(years),
        "journal": journal_status(export_dir),
    }


def map_account(number, by_number):
    """The ERPNext account with the bexio account number: the same number, as in import_master."""
    if number not in by_number:
        raise Unmapped("account {} is not in ERPNext".format(number))
    return by_number[number]


def opening_entry(lines, year, by_number, company=COMPANY):
    """ONE Journal Entry from (account number, debit, credit) lines of the first year.

    None when there are no lines, or none with an amount: no opening entry is needed.
    Lines of the same ERPNext account are added up. The entry must balance, or Unmapped.
    """
    totals = {}
    for number, debit, credit in lines:
        name = map_account(number, by_number)
        totals[name] = totals.get(name, ZERO) + _dec(debit) - _dec(credit)
    if sum(totals.values(), ZERO) != ZERO:
        raise Unmapped("the opening lines do not balance: difference {}".format(sum(totals.values(), ZERO)))
    rows = [
        {"account": name, "debit_in_account_currency": float(net) if net > 0 else 0,
         "credit_in_account_currency": float(-net) if net < 0 else 0}
        for name, net in sorted(totals.items()) if net
    ]
    if not rows:
        return None
    return {
        "doctype": "Journal Entry", "company": company, "voucher_type": OPENING_ENTRY, "is_opening": "Yes",
        "posting_date": year["start"], "bexio_id": "opening-{}".format(year["id"]),
        "user_remark": "bexio opening balances, business year {}".format(year["id"]),
        "accounts": rows,
    }


def main(argv):
    parser = argparse.ArgumentParser(description="The Opening Journal Entry of the first bexio business year (dry run only for now).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print what the export gives")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if not args.dry_run:
        parser.error("dry run only for now: the live run is erp-a2ma's, after the posting plan (finance-3qsp)")

    export_dir = args.export or im.newest_export()
    s = survey(export_dir)
    first = s["first"]
    print("opening of the first business year, from {}".format(export_dir))
    print("business years: {}, closed: {}".format(s["years"], s["closed"]))
    print("first business year: id {}, starts {}, status {}".format(first["id"], first["start"], first["status"]))

    erp = im.Erp.from_file(args.token_file)
    try:
        accounts = erp.list("Account", [["company", "=", COMPANY]], ["name", "account_number"])
        entries = erp.list("Journal Entry", [["bexio_id", "like", "opening-%"]], ["name"])
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    numbers = {r["account_number"] for r in accounts}
    missing = [n for n in OPENING_ACCOUNTS if n not in numbers]
    print("ERPNext: opening accounts {}; Opening Entries with bexio_id: {}".format(
        "missing: " + ", ".join(missing) if missing else "all found", len(entries)))

    if s["journal"] != "exported":
        print("opening entry: not built, the export has no journal ({}); the opening bookings are unknown".format(s["journal"]))
        return 1
    print("opening entry: not built, the export has journal.json, which this module does not read yet")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
