"""Map the bexio journal lines that no exported document carries to ERPNext Journal Entries, one per line, offline.

The lines are the posting plan's bucket `unsourced` (posting_plan.py, section 4 of the private plan): payroll and social
insurance, which no export entity carries. Each line becomes one Journal Entry on its own date, with a
debit row and a credit row on the ERPNext accounts of the two bexio account ids. The Journal Entry is keyed by bexio's
journal line id (bexio_id journal-<id>), so a rerun of the loader inserts nothing and erp-fd93 and erp-7avs can skip
or reconcile these lines by that key.

Left out, each listed by journal id with the reason in <private>/bexio-payroll-left-out.txt:
- a line that a banking entry and a manual entry both claim (posting plan rule 5): it is not guessed at here;
- a line already booked by an earlier VAT Journal Entry (the ids in bexio-vat-on-payment-ids.txt and
  bexio-vat-on-payment-out-ids.txt, as import_vat_fix lists them).

Run it as:

    python3 finance/bexio/import_payroll.py --dry-run [--export DIR]
    python3 finance/bexio/import_payroll.py --write FILE [--export DIR]

--dry-run reads ERPNext's accounts (by bexio_id) and prints totals per year only; --write FILE writes the loader plan
(finance/scripts/bexio-drafts.sh FILE submit does the live run). Nothing goes into ERPNext here. A line whose account has
no ERPNext Account is not written: the dry run lists it, and --write stops. The journal ids that are not written go to
<private>, never to the screen. Standard library only, apart from import_master and posting_plan.
"""

import argparse
import collections
import json
import os
import sys
from decimal import Decimal

import import_master as im
import import_purchase as ip
import posting_plan as pp

PAYROLL_BUCKET = "unsourced"
LEFT_OUT_FILE = "bexio-payroll-left-out.txt"
IDS_FILE = "bexio-payroll-ids.txt"
DIFFERENCES_FILE = "bexio-payroll-live-differences.txt"
# the journal ids a VAT Journal Entry of import_vat_fix already books (the same files erp-fd93 reads)
VAT_ID_FILES = ("bexio-vat-on-payment-ids.txt", "bexio-vat-on-payment-out-ids.txt")
JOURNAL = "Journal Entry"
CURRENCY = "CHF"
RULE_5 = "a banking entry and a manual entry of the same day and text (posting plan rule 5)"
VAT_BOOKED = "already booked by a VAT Journal Entry (import_vat_fix)"


class Unmapped(Exception):
    """A line with an account that has no ERPNext Account by bexio id: listed, never guessed."""


def rule5_conflicts(data):
    """The journal ids that a banking entry and a manual entry both claim by date and text (posting plan rule 5).

    The same key as posting_plan.classify uses for a manual entry's tax line; a line is in conflict when its parents
    are of two kinds. classify puts such a line in `unsourced`, which is why it is left out here.
    """
    kinds = collections.defaultdict(set)
    for entry in data["manual_entries"]:
        for row in entry["entries"]:
            if row.get("id") is not None:
                kinds[(str(entry["date"])[:10], row["description"])].add(pp._manual_bucket(entry))
    return {line["id"] for line in data["journal"]
            if len(kinds.get((line["date"][:10], line["description"]), ())) > 1}


def vat_booked_ids(private=im.PRIVATE):
    """The journal ids the VAT Journal Entries of import_vat_fix book, from the private id files (second word of each line)."""
    ids = set()
    for name in VAT_ID_FILES:
        with open(os.path.join(private, name), encoding="utf-8") as f:
            for text in f:
                words = text.split()
                if len(words) > 1 and words[0] == "journal" and words[1].isdigit():
                    ids.add(int(words[1]))
    return ids


def select(data, buckets, vat_ids):
    """(lines to import, left out as (journal id, reason)): the unsourced lines, less rule 5 and the VAT-booked ids."""
    conflicts = rule5_conflicts(data)
    lines, left_out = [], []
    for line in data["journal"]:
        if buckets[line["id"]] != PAYROLL_BUCKET:
            continue
        if line["id"] in conflicts:
            left_out.append((line["id"], RULE_5))
        elif line["id"] in vat_ids:
            left_out.append((line["id"], VAT_BOOKED))
        else:
            lines.append(line)
    return lines, left_out


def _account(accounts, bexio_id):
    name = accounts.get(str(bexio_id))
    if name is None:
        raise Unmapped("no ERPNext Account for bexio account {}".format(bexio_id))
    return name


def journal_entry(line, accounts):
    """The loader's document for one bexio journal line: a Journal Entry with a debit row and a credit row.

    The row follows import_manual_entries._row: the CHF amount on the row, and the line's own amount in its currency with
    the line's rate, so a line in another currency keeps its original amount. The payroll lines are all CHF in the export.
    """
    debit = _account(accounts, line["debit_account_id"])
    credit = _account(accounts, line["credit_account_id"])
    factor = float(line.get("currency_factor") or 1)
    chf = float(line["base_currency_amount"])
    amount = float(line["amount"])
    remark = "bexio journal {}: {}".format(line["id"], line["description"])
    rows = [
        {"account": debit, "user_remark": remark, "exchange_rate": factor, "debit": chf, "debit_in_account_currency": amount},
        {"account": credit, "user_remark": remark, "exchange_rate": factor, "credit": chf, "credit_in_account_currency": amount},
    ]
    key = "journal-{}".format(line["id"])
    return {"doctype": JOURNAL, "name": None, "bexio_id": key, "values": {
        "company": im.COMPANY, "voucher_type": "Journal Entry", "bexio_id": key,
        "posting_date": line["date"][:10], "user_remark": remark,
        "multi_currency": int(factor != 1), "accounts": rows,
    }}


def plan(lines, accounts):
    """(documents for the loader, unmapped as (journal id, reason)) for the lines to import."""
    documents, unmapped = [], []
    for line in lines:
        try:
            documents.append(journal_entry(line, accounts))
        except Unmapped as err:
            unmapped.append((line["id"], str(err)))
    return documents, unmapped


def totals(lines):
    """Per year: (count, sum of the CHF amounts), of the lines to import."""
    per_year = collections.defaultdict(lambda: [0, Decimal("0")])
    for line in lines:
        cell = per_year[line["date"][:4]]
        cell[0] += 1
        cell[1] += Decimal(str(line["base_currency_amount"]))
    return dict(per_year)


def report(per_year, left_out, unmapped, export_dir):
    out = ["payroll lines dry run from {}".format(export_dir),
           "{:<6}{:>8}{:>18}".format("year", "lines", "CHF")]
    for year in sorted(per_year):
        count, total = per_year[year]
        out.append("{:<6}{:>8}{:>18,.2f}".format(year, count, total))
    out.append("{:<6}{:>8}{:>18,.2f}".format("all", sum(c for c, _ in per_year.values()), sum((t for _, t in per_year.values()), Decimal("0"))))
    out.append("left out: {}".format(len(left_out)))
    out.append("unmapped: {}".format(len(unmapped)))
    return "\n".join(out)


def load_accounts(token_file):
    """bexio account id -> ERPNext Account name, read from ERPNext (read only)."""
    erp = im.Erp.from_file(token_file)
    rows = erp.list("Account", [["company", "=", im.COMPANY], ["bexio_id", "is", "set"]], ["name", "bexio_id"])
    return {str(r["bexio_id"]): r["name"] for r in rows}


def check(lines, accounts, erp, private=im.PRIVATE):
    """Read ERPNext (read only) after the loader run and compare it with the lines.

    Counts the submitted Journal Entries keyed journal-%, and per voucher compares the GL with the line: the debit account
    and the credit account at the line's CHF amount. Per year and account, the GL sum of these vouchers is shown against
    bexio's lines. The posted journal ids go to <private>/bexio-payroll-ids.txt (erp-fd93 and erp-7avs read it), and the
    lines that are missing or differ to <private>/bexio-payroll-live-differences.txt. Totals only on the screen.

    Returns the number of lines that are missing or differ.
    """
    posted = erp.list("Journal Entry", [["company", "=", im.COMPANY], ["bexio_id", "like", "journal-%"], ["docstatus", "=", 1]],
                      ["name", "bexio_id"])
    voucher = {row["bexio_id"]: row["name"] for row in posted}
    wanted = ["journal-{}".format(line["id"]) for line in lines]
    names = [voucher[key] for key in wanted if key in voucher]
    key_of = {name: key for key, name in voucher.items()}
    # the GL of these vouchers, per bexio key and ERPNext account, debit minus credit
    gl = collections.defaultdict(lambda: collections.defaultdict(Decimal))
    for start in range(0, len(names), 50):
        for row in erp.list("GL Entry", [["company", "=", im.COMPANY], ["is_cancelled", "=", 0], ["voucher_type", "=", "Journal Entry"],
                                         ["voucher_no", "in", names[start:start + 50]]],
                            ["voucher_no", "account", "debit", "credit"]):
            gl[key_of[row["voucher_no"]]][row["account"]] += Decimal(str(row["debit"])) - Decimal(str(row["credit"]))
    differs, want_total, have_total = [], collections.defaultdict(Decimal), collections.defaultdict(Decimal)
    for line, key in zip(lines, wanted):
        chf = Decimal(str(line["base_currency_amount"]))
        debit, credit = accounts[str(line["debit_account_id"])], accounts[str(line["credit_account_id"])]
        want = {debit: chf, credit: -chf}
        have = {account: value for account, value in gl.get(key, {}).items() if value != 0}
        for account, value in want.items():
            want_total[(line["date"][:4], account)] += value
        for account, value in have.items():
            have_total[(line["date"][:4], account)] += value
        if key not in voucher:
            differs.append("journal {}: missing".format(line["id"]))
        elif want != have:
            differs.append("journal {}: differs".format(line["id"]))
    ids = sorted(int(key.split("-")[1]) for key in wanted if key in voucher)
    # the same shape as the VAT id files, so vat_booked_ids and erp-fd93 read it the same way
    ip.write_private(os.path.join(private, IDS_FILE), ["journal {} posted as a payroll Journal Entry (import_payroll)".format(i) for i in ids] or ["none"])
    ip.write_private(os.path.join(private, DIFFERENCES_FILE), differs or ["none"])
    print("submitted Journal Entries keyed journal-%: {}; payroll lines: {}; posted for them: {}".format(len(posted), len(lines), len(ids)))
    print("missing or differing: {}".format(len(differs)))
    print("{:<6}{:<32}{:>18}{:>18}{:>14}".format("year", "account", "bexio", "ERPNext", "difference"))
    for year, account in sorted(set(want_total) | set(have_total)):
        want, have = want_total[(year, account)], have_total[(year, account)]
        print("{:<6}{:<32}{:>18,.2f}{:>18,.2f}{:>14,.2f}".format(year, account[:31], want, have, have - want))
    return len(differs)


def main(argv):
    parser = argparse.ArgumentParser(description="Map the bexio payroll journal lines to ERPNext Journal Entries (offline).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="print the totals per year, write nothing to ERPNext")
    parser.add_argument("--write", metavar="FILE", default=None, help="write the loader plan to a private file (bexio-drafts.sh FILE submit)")
    parser.add_argument("--check", action="store_true", help="read ERPNext after the loader run: counts and GL sums against the lines, write nothing")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if sum(bool(x) for x in (args.dry_run, args.write is not None, args.check)) != 1:
        parser.error("exactly one of --dry-run, --write FILE and --check")

    export_dir = args.export or im.newest_export()
    data = pp.load(export_dir)
    buckets = pp.classify(data)
    lines, left_out = select(data, buckets, vat_booked_ids())
    try:
        accounts = load_accounts(args.token_file)
        if args.check:
            erp = im.Erp.from_file(args.token_file)
            # the bexio account ids on the lines, as the accounts of the check
            return 1 if check(lines, accounts, erp) else 0
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    documents, unmapped = plan(lines, accounts)
    print(report(totals(lines), left_out, unmapped, export_dir))
    listed = ["journal {}: left out, {}".format(i, reason) for i, reason in left_out]
    listed += ["journal {}: not written, {}".format(i, reason) for i, reason in unmapped]
    ip.write_private(os.path.join(im.PRIVATE, LEFT_OUT_FILE), listed or ["none"])
    print("left out and not written are listed by journal id in {}".format(os.path.join(im.PRIVATE, LEFT_OUT_FILE)))
    if args.write:
        if unmapped:
            print("not written: {} line(s) have an unmapped account".format(len(unmapped)), file=sys.stderr)
            return 1
        ip.write_private(args.write, [json.dumps({"documents": documents, "exchange_rates": []}, indent=1)])
        print("{} Journal Entries handed to the loader in {}".format(len(documents), args.write))
    else:
        print("dry run: nothing was written to ERPNext")
    return 1 if unmapped else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
