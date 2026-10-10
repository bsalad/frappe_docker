"""Correct the VAT of the bexio manual entries already in ERPNext, with Journal Entries keyed by bexio id.

The manual entries were loaded with their VAT on the expense or income account the row names (import_manual_entries
took tax_account_id for the VAT account). The mapping now books it by the code's kind: 2200 for a sales code, 1170 or 1171
for a purchase one, and 1171 against 2203 for a reverse charge. The submitted Journal Entries are not changed or cancelled.
For each live entry, the difference per account between what the mapping books and what ERPNext holds is one Journal Entry
on the entry's posting date, keyed vatfix-manual-<id>:

- a live entry is a submitted Journal Entry keyed manual-<id>. Its group is that entry and its own corrections: the
  entries keyed manual-<id>-<suffix> (a correction the bexio export does not hold, such as manual-1099-correction) and
  the vatfix-manual-<id> entry an earlier run submitted, so a finished group has no difference left;
- the expected side is the mapping of the export's entry alone, not of the --extra entries (they are corrections of
  what is live, not bexio's entries);
- a group whose correction does not balance, or whose entry no longer maps, is listed by bexio id and not written;
- an entry listed in the private journal-wins file is the exception to the mapping: its expected side is what bexio's
  journal books for it (the lines entry_of_lines pairs with it), because bexio booked it differently on purpose.
  Each listed entry says why in that file; the dry run counts them apart.

The dry run reads ERPNext and prints counts and totals per year and account. The live run is --write FILE, which hands
the Journal Entries to the loader (finance/scripts/bexio-drafts.sh FILE submit). Nothing goes into ERPNext here.

    python3 finance/bexio/import_manual_fix.py --dry-run [--export DIR]
    python3 finance/bexio/import_manual_fix.py --write FILE [--export DIR]

The export is read as import_manual_entries reads it. Standard library only, apart from import_master,
import_manual_entries and import_purchase. The bexio ids of what is not written go to <private>, never to the screen.
"""

import argparse
import collections
import json
import os
import sys
from decimal import Decimal, ROUND_HALF_UP

import import_manual_entries as ime
import import_master as im
import import_purchase as ip

CENT = Decimal("0.01")
ZERO = Decimal("0")
KEY_PREFIX = "manual-"
CORRECTION_PREFIX = "vatfix-manual-"
PROBLEMS_FILE = "bexio-manual-fix-problems.txt"
DETAILS_FILE = "bexio-manual-fix-documents.txt"
JOURNAL_WINS_FILE = "bexio-manual-journal-wins.txt"


def group_of(key):
    """The bexio entry a Journal Entry's bexio_id belongs to: manual-1099 and manual-1099-correction are 1099, and
    vatfix-manual-1099 is 1099 too. None for any other key."""
    if key.startswith(CORRECTION_PREFIX):
        return key[len(CORRECTION_PREFIX):].split("-")[0]
    if key.startswith(KEY_PREFIX):
        return key[len(KEY_PREFIX):].split("-")[0]
    return None


def _money(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def live_groups(vouchers, gl):
    """The live groups: {group: {"name": base entry's name or None, "date": posting date, "live": {account name: debit minus
    credit}, "keys": [bexio ids]}}. vouchers are the submitted Journal Entries (name, bexio_id, posting_date); gl the GL
    Entry rows of Journal Entries (voucher_no, account, debit, credit), not cancelled."""
    groups = {}
    by_name = {}
    for voucher in vouchers:
        group = group_of(voucher["bexio_id"])
        if group is None:
            continue
        found = groups.setdefault(group, {"name": None, "date": None, "live": collections.defaultdict(lambda: ZERO), "keys": []})
        found["keys"].append(voucher["bexio_id"])
        if voucher["bexio_id"] == KEY_PREFIX + group:
            found["name"], found["date"] = voucher["name"], voucher["posting_date"]
        by_name[voucher["name"]] = group
    for row in gl:
        group = by_name.get(row["voucher_no"])
        if group is not None:
            groups[group]["live"][row["account"]] += _money(row["debit"]) - _money(row["credit"])
    return groups


def expected_group(entry, lookups, currencies):
    """The mapping of one export entry as {account name: debit minus credit}, its voucher type and posting date.
    Raises ip.MappingError when the entry does not map."""
    doc = ime.map_entry(entry, lookups, currencies)
    want = collections.defaultdict(lambda: ZERO)
    for row in doc["accounts"]:
        want[row["account"]] += _money(row.get("debit", 0)) - _money(row.get("credit", 0))
    return {"want": dict(want), "voucher_type": doc["voucher_type"], "date": doc["posting_date"]}


def load_journal_wins(path):
    """The entries whose expected side is bexio's journal, from the private file: one bexio id per line, then its reason.
    {group: reason}. Blank lines and lines starting with # are skipped; a missing file lists none."""
    if not os.path.exists(path):
        return {}
    reasons = {}
    with open(path, encoding="utf-8") as f:
        for raw in f:
            text = raw.strip()
            if not text or text.startswith("#"):
                continue
            bexio_id, _, reason = text.partition(" ")
            if not reason.strip():
                raise ValueError("a line of {} has no reason after its bexio id".format(path))
            reasons[bexio_id] = reason.strip()
    return reasons


def journal_wins(entry, export_entries, journal, lookups):
    """What bexio's journal books for one export entry, per account (CHF, debit minus credit): the journal lines that
    import_manual_entries.entry_of_lines pairs with it, over all the export's entries. Raises ip.MappingError when a line books an
    account with no Account in ERPNext."""
    owner = ime.entry_of_lines(export_entries, journal)
    names = {str(bexio_id): name for bexio_id, (name, _) in lookups.accounts.items()}
    want = collections.defaultdict(lambda: ZERO)
    for line in journal:
        if owner.get(line["id"]) != str(entry["id"]):
            continue
        amount = _money(line["base_currency_amount"])
        for side, sign in (("debit_account_id", 1), ("credit_account_id", -1)):
            account = names.get(str(line[side]))
            if account is None:
                raise ip.MappingError("bexio's journal line {} books an account with no Account in ERPNext".format(line["id"]))
            want[account] += sign * amount
    return dict(want)


def difference(want, live):
    """The corrections that take the live amounts to the expected ones: {account name: debit minus credit}, only where they differ."""
    lines = {}
    for account in set(want) | set(live):
        diff = _money(want.get(account, ZERO) - live.get(account, ZERO))
        if diff != ZERO:
            lines[account] = diff
    return lines


def correction_key(group, taken):
    """The bexio_id of a group's next correction: vatfix-manual-<group>, or -2, -3 … when that key is taken. The loader never
    rewrites a submitted document, so a group that was corrected before gets a new key rather than a skip."""
    key, number = CORRECTION_PREFIX + group, 1
    while key in taken:
        number += 1
        key = "{}{}-{}".format(CORRECTION_PREFIX, group, number)
    return key


def correction_document(group, day, voucher_type, lines, lookups, key=None):
    """The Journal Entry for the loader: one row per account of the difference, keyed by key (default vatfix-manual-<group>)."""
    rows = []
    for account in sorted(lines):
        amount = lines[account]
        row = {"account": account}
        if amount > ZERO:
            row["debit_in_account_currency"] = float(amount)
        else:
            row["credit_in_account_currency"] = float(-amount)
        rows.append(row)
    key = key or CORRECTION_PREFIX + group
    return {"doctype": "Journal Entry", "name": None, "bexio_id": key, "values": {
        "company": im.COMPANY, "voucher_type": voucher_type, "bexio_id": key, "posting_date": day,
        "user_remark": "VAT by code, bexio manual entry {}".format(group), "accounts": rows,
    }}


def plan(export_entries, vouchers, gl, lookups, currencies, journal=(), wins=None):
    """The corrections for the live groups: (documents, problems, details, counts). A live group whose entry is in the
    export is compared with its mapping, or with bexio's journal when its bexio id is in wins (the reasons by bexio id,
    see load_journal_wins); an export entry that is not live is counted, not written."""
    groups = live_groups(vouchers, gl)
    exported = {str(entry["id"]): entry for entry in export_entries}
    wins = wins or {}
    documents, problems, details = [], [], []
    counts = collections.Counter()
    for group in sorted(groups, key=str):
        counts["live groups"] += 1
        found = groups[group]
        if group not in exported:
            problems.append((group, "live Journal Entries of a bexio entry the export does not hold"))
            continue
        try:
            expected = expected_group(exported[group], lookups, currencies)
            if group in wins:
                expected["want"] = journal_wins(exported[group], export_entries, journal, lookups)
                counts["journal wins"] += 1
        except ip.MappingError as err:
            problems.append((group, "the entry does not map: {}".format(err)))
            continue
        lines = difference(expected["want"], found["live"])
        details.append("manual {} ({}){}: correction {}".format(
            group, expected["date"], " journal wins: " + wins[group] if group in wins else "", _listed(lines)))
        if not lines:
            counts["unchanged"] += 1
            continue
        if sum(lines.values(), ZERO) != ZERO:
            problems.append((group, "the correction does not balance: {}".format(_listed(lines))))
            continue
        currency = [account for account in lines if lookups.currency_of(account) not in (None, "CHF")]
        if currency:
            problems.append((group, "the correction touches an account not in CHF: {}".format(", ".join(sorted(currency)))))
            continue
        voucher_type = "Depreciation Entry" if any(account in lookups.depreciation for account in lines) else "Journal Entry"
        key = correction_key(group, found["keys"])
        documents.append(correction_document(group, expected["date"], voucher_type, lines, lookups, key))
        counts["corrections"] += 1
    for group in sorted(set(exported) - set(groups), key=str):
        counts["not live"] += 1
    return documents, problems, details, counts


def _listed(lines):
    return "{" + ", ".join("{}: {}".format(account, lines[account]) for account in sorted(lines)) + "}" if lines else "{}"


def totals_by_year(documents, numbers):
    """The corrections per year and account number: {(year, number): debit minus credit}."""
    totals = collections.defaultdict(lambda: ZERO)
    for document in documents:
        year = document["values"]["posting_date"][:4]
        for row in document["values"]["accounts"]:
            amount = _money(row.get("debit_in_account_currency", 0) - row.get("credit_in_account_currency", 0))
            totals[(year, numbers.get(row["account"], row["account"]))] += amount
    return totals


def report(counts, documents, numbers):
    lines = ["manual entries: VAT corrections, nothing was written",
             "live groups {}, corrections {}, unchanged {}, journal wins {}, not live {}".format(
                 counts["live groups"], counts["corrections"], counts["unchanged"], counts.get("journal wins", 0), counts["not live"]),
             "{:<6}{:<8}{:>16}".format("year", "acc", "correction")]
    for (year, number), amount in sorted(totals_by_year(documents, numbers).items()):
        if amount != ZERO:
            lines.append("{:<6}{:<8}{:>16,.2f}".format(year, number, amount))
    return "\n".join(lines)


def read_vouchers_and_gl(erp):
    """The submitted Journal Entries with a bexio_id of the manual kind (the entries and their corrections), their GL rows,
    and the account numbers by name."""
    company = [["company", "=", im.COMPANY]]
    vouchers = erp.list("Journal Entry", company + [["docstatus", "=", 1], ["bexio_id", "like", "%manual-%"]],
                        ["name", "bexio_id", "posting_date"])
    gl = erp.list("GL Entry", company + [["is_cancelled", "=", 0], ["voucher_type", "=", "Journal Entry"]],
                  ["voucher_no", "account", "debit", "credit"])
    numbers = {r["name"]: r["account_number"] for r in erp.list("Account", company + [["is_group", "=", 0]], ["name", "account_number"])}
    return vouchers, gl, numbers


def main(argv):
    parser = argparse.ArgumentParser(description="Correct the VAT of the live bexio manual entries (dry run or loader plan).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the counts and totals")
    parser.add_argument("--write", metavar="FILE", default=None, help="write the Journal Entries to a private file for the loader")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if args.dry_run == (args.write is not None):
        parser.error("exactly one of --dry-run and --write FILE")
    export_dir = args.export or im.newest_export()
    loaded = ime.load_entries(export_dir)
    if loaded is None:
        print("manual entries: not exported yet in {}".format(export_dir))
        return 0
    export_entries, currencies = loaded
    try:
        erp = im.Erp.from_file(args.token_file)
        lookups = ime.Lookups.from_erp(erp)
        vouchers, gl, numbers = read_vouchers_and_gl(erp)
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    wins = load_journal_wins(os.path.join(im.PRIVATE, JOURNAL_WINS_FILE))
    journal = []
    if wins:
        with open(os.path.join(export_dir, ime.JOURNAL_FILE), encoding="utf-8") as f:
            journal = json.load(f)
    documents, problems, details, counts = plan(export_entries, vouchers, gl, lookups, currencies, journal, wins)
    print(report(counts, documents, numbers))
    ip.write_private(os.path.join(im.PRIVATE, PROBLEMS_FILE), ["{}: {}".format(group, reason) for group, reason in problems] or ["none"])
    ip.write_private(os.path.join(im.PRIVATE, DETAILS_FILE), details or ["none"])
    print("{} listed by bexio id in {}".format(len(problems), os.path.join(im.PRIVATE, PROBLEMS_FILE)))
    if args.write:
        ip.write_private(args.write, [json.dumps({"documents": documents, "exchange_rates": []}, indent=1)])
        print("{} Journal Entries handed to the loader in {}".format(len(documents), args.write))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
