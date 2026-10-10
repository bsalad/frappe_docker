"""Map the bexio manual entries to ERPNext Journal Entries, offline.

One step of the bexio pipeline after import_master.py: map_entry() returns the
Journal Entry dict for one bexio manual entry, as import_sales.py and
import_purchase.py do for their documents. The command line either reads
ERPNext and prints totals (--dry-run), or writes the loader plan to a private
file (--write FILE); bexio-drafts.sh FILE submit does the live run (erp-fd93).

Run it as:

    python3 finance/bexio/import_manual_entries.py --dry-run [--export DIR]
    python3 finance/bexio/import_manual_entries.py --write FILE [--export DIR]

--export defaults to the newest directory under <private>/bexio-export/. The
entries are read from manual_entries.json in it. While that file is not
there (the export is still pending, HTTP 403) the command says "not exported
yet" and stops. The totals are printed only; the bexio ids of the entries
it cannot map go to <private>/bexio-manual-entries-dry-run.txt, never to the
screen or the repository. An entry that cannot map is left out of the plan
and listed there, so the plan never holds half an entry.

Each entry is one Journal Entry, keyed bexio_id manual-<id> (manual_key), with a row for every debit and credit of its
lines, so it always has at least two rows. Assumptions the posting plan has
to confirm: the amount of a line is gross (incl. VAT) when the line has a
tax_id; the VAT is then split off on the side of the income or expense account
of the line (a purchase debits the Vorsteuer, a sale credits the Umsatzsteuer,
a reversal the other way round; with no such account the root type of the tax
account decides) and booked to tax_account_id at the rate of its
code (import_purchase's VAT_OF_TAX_ID). A code without an Item Tax Template in
ERPNext is reported, not guessed. Entries of type banking_transaction are
mapped too, but counted apart: the posting plan decides whether they or the
bank transactions carry the booking.

Two shapes the export has beyond the single line. A compound entry has one row
without id: its counterpart, the account each id row is booked against (its
journal line carries it on the other side); the counterpart is not posted
itself, the id rows take it as their missing side. A reverse-charge code
(import_purchase's BEZUG) is net, and its tax goes on the Vorsteuer account and
is taken back on the Bezugsteuer liability, as the bills do. An entry with no
type is booked as a single one.

Standard library only, apart from import_master and import_purchase.
"""

import argparse
import datetime
import json
import os
import sys
from decimal import Decimal, ROUND_HALF_UP

import import_master as im
import import_purchase as ip

ENTRIES_FILE = "manual_entries.json"
CURRENCIES_FILE = "currencies.json"
PROBLEMS_FILE = "bexio-manual-entries-dry-run.txt"
JOURNAL = "Journal Entry"
SINGLE, COMPOUND, GROUP = "manual_single_entry", "manual_compound_entry", "manual_group_entry"
# the type of the entries a bank import created; bexio's docs do not list it, the name is from the mapping notes
BANKING = "banking_transaction"
TYPES = (SINGLE, COMPOUND, GROUP)
# the side a VAT account carries: Vorsteuer (assets) is debited, Umsatzsteuer (liabilities) credited
SIDE_OF_ROOT = {"Asset": "debit", "Expense": "debit", "Liability": "credit", "Income": "credit", "Equity": "credit"}
PROFIT_AND_LOSS = ("Income", "Expense")
CENT = Decimal("0.01")
ZERO = Decimal("0")


class Lookups:
    """What the mapping reads from ERPNext: Accounts by bexio_id with their root type, Accounts by number (the reverse-charge
    accounts), the currency of each Account, and Item Tax Templates by bexio_id."""

    def __init__(self, accounts, taxes, by_number=None, currencies=None):
        self.accounts = accounts      # bexio account id -> (Account name, root type)
        self.taxes = taxes            # bexio tax id -> Item Tax Template name
        self.by_number = by_number or {}  # account number -> (Account name, root type)
        self.currencies = currencies or {}  # Account name -> the account's currency

    def currency_of(self, account):
        return self.currencies.get(account)

    @classmethod
    def from_erp(cls, erp):
        rows = erp.list("Account", [["company", "=", im.COMPANY], ["bexio_id", "is", "set"]], ["name", "bexio_id", "root_type", "account_currency"])
        numbers = sorted({account for account, _ in ip.BEZUG.values()} | {ip.BEZUGSTEUER})
        by_number = erp.list("Account", [["company", "=", im.COMPANY], ["account_number", "in", numbers]],
                             ["name", "account_number", "root_type", "account_currency"])
        return cls(
            accounts={r["bexio_id"]: (r["name"], r["root_type"]) for r in rows},
            taxes={r["bexio_id"]: r["name"] for r in erp.list("Item Tax Template", [["company", "=", im.COMPANY], ["bexio_id", "is", "set"]], ["name", "bexio_id"])},
            by_number={r["account_number"]: (r["name"], r["root_type"]) for r in by_number},
            currencies={r["name"]: r["account_currency"] for r in rows + by_number},
        )


def _money(value):
    return Decimal(str(value))


def _chf(value, factor):
    return (value * factor).quantize(CENT, rounding=ROUND_HALF_UP)


def _day(entry):
    try:
        return datetime.date.fromisoformat(entry["date"])
    except (KeyError, TypeError, ValueError):
        raise ip.MappingError("no valid date")


def _account(lookups, bexio_id):
    found = lookups.accounts.get(str(bexio_id))
    if not found:
        raise ip.MappingError("no Account for account {}".format(bexio_id))
    return found


def _vat(line, lookups):
    """(rate, tax account name, side of the tax account) of a line, or None when the line carries no VAT."""
    tax_id = line.get("tax_id")
    if not tax_id or tax_id in ip.ZERO_RATE_IDS:
        return None
    if tax_id not in im.VAT_OF_TAX_ID:
        raise ip.MappingError("unknown VAT code {}".format(tax_id))
    if not lookups.taxes.get(str(tax_id)):
        raise ip.MappingError("no Item Tax Template with bexio_id {} in ERPNext".format(tax_id))
    tax_account, root = _account(lookups, line.get("tax_account_id"))
    if root not in SIDE_OF_ROOT:
        raise ip.MappingError("VAT account {} has root type {}".format(line.get("tax_account_id"), root))
    rate, _ = im.VAT_OF_TAX_ID[tax_id]
    return rate, tax_account, SIDE_OF_ROOT[root]


def _postings(kind, lines):
    """The lines that are postings. A compound entry has one row without id: its counterpart, the account every id row
    is booked against (the journal line of each id row carries it on the other side). The counterpart is not posted
    itself, or the total would be booked twice; each id row takes its missing side from it.
    """
    if kind != COMPOUND:
        if any(line.get("id") is None for line in lines):
            raise ip.MappingError("row without id in a {} entry".format(kind or "untyped"))
        return lines
    counterparts = [line for line in lines if line.get("id") is None]
    if len(counterparts) != 1:
        raise ip.MappingError("compound entry with {} counterpart rows".format(len(counterparts)))
    counterpart = counterparts[0].get("debit_account_id") or counterparts[0].get("credit_account_id")
    if not counterpart:
        raise ip.MappingError("counterpart row without an account")
    postings = []
    for line in lines:
        if line.get("id") is None:
            continue
        line = dict(line)
        if line.get("debit_account_id") and not line.get("credit_account_id"):
            line["credit_account_id"] = counterpart
        elif line.get("credit_account_id") and not line.get("debit_account_id"):
            line["debit_account_id"] = counterpart
        postings.append(line)
    return postings


def _reverse_charge(line, lookups, debit, credit, amount, factor):
    """The parts of a reverse-charge line (Bezugsteuer): the amount is net, and the tax is booked on the Vorsteuer account
    and taken back on the Bezugsteuer liability, as import_purchase does for a bill (its net is the same on both sides)."""
    vorsteuer_number, rate = ip.BEZUG[line["tax_id"]]
    vorsteuer, _ = _by_number(lookups, vorsteuer_number)
    bezugsteuer, _ = _by_number(lookups, ip.BEZUGSTEUER)
    tax = (amount * _money(rate) / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    parts = [(debit, "debit", amount, _chf(amount, factor)), (credit, "credit", amount, _chf(amount, factor)),
             (vorsteuer, "debit", tax, _chf(tax, factor)), (bezugsteuer, "credit", tax, _chf(tax, factor))]
    return sorted(parts, key=lambda part: part[1] != "debit")


def _by_number(lookups, number):
    found = lookups.by_number.get(number)
    if not found:
        raise ip.MappingError("no Account {} in ERPNext".format(number))
    return found


def _line_parts(line, lookups, currencies):
    """The (account, side, amount in the line's currency, amount in CHF) of one bexio line, the VAT split off when it has one."""
    debit, debit_root = _account(lookups, line.get("debit_account_id"))
    credit, credit_root = _account(lookups, line.get("credit_account_id"))
    code = currencies.get(str(line.get("currency_id")))
    if not code:
        raise ip.MappingError("unknown currency {}".format(line.get("currency_id")))
    amount = _money(line["amount"])
    factor = _money(line.get("currency_factor") or 1)
    gross = _chf(amount, factor)
    if line.get("tax_id") in ip.BEZUG:
        return _reverse_charge(line, lookups, debit, credit, amount, factor), factor
    vat = _vat(line, lookups)
    if vat is None:
        return [(debit, "debit", amount, gross), (credit, "credit", amount, gross)], factor
    rate, tax_account, tax_side = vat
    # the VAT sits on the side of the income or expense account it was charged on, so a reversal (income
    # debited, receivable credited) debits the Umsatzsteuer; with no such account the tax account's own side decides
    side = tax_side
    if debit_root in PROFIT_AND_LOSS and credit_root not in PROFIT_AND_LOSS:
        side = "debit"
    elif credit_root in PROFIT_AND_LOSS and debit_root not in PROFIT_AND_LOSS:
        side = "credit"
    net = (amount / (1 + _money(rate) / 100)).quantize(CENT, rounding=ROUND_HALF_UP)
    net_chf = _chf(net, factor)
    # the CHF tax is the rest of the gross, so the rows add up to the gross in CHF to the rappen
    tax_chf = gross - net_chf
    if side == "debit":
        parts = [(debit, "debit", net, net_chf), (tax_account, "debit", amount - net, tax_chf), (credit, "credit", amount, gross)]
    else:
        parts = [(credit, "credit", net, net_chf), (tax_account, "credit", amount - net, tax_chf), (debit, "debit", amount, gross)]
    # debit rows first, as a journal reads
    return sorted(parts, key=lambda part: part[1] != "debit"), factor


def map_entry(entry, lookups, currencies):
    """The Journal Entry dict for one bexio manual entry; raises MappingError when a part has no home.

    currencies maps bexio currency ids to ERPNext currency codes (currencies.json).
    """
    kind = entry.get("type")
    # an untyped entry (type None) has the shape of a single one and is booked as one
    if kind not in TYPES + (BANKING, None):
        raise ip.MappingError("unknown entry type {!r}".format(kind))
    if entry.get("id") is None:
        raise ip.MappingError("entry without id")
    lines = entry.get("entries") or []
    if not lines:
        raise ip.MappingError("no entries lines")
    if kind == SINGLE and len(lines) != 1:
        raise ip.MappingError("single entry with {} lines".format(len(lines)))
    rows, debit, credit = [], ZERO, ZERO
    reference = entry.get("reference_nr") or ""
    for line in _postings(kind, lines):
        parts, factor = _line_parts(line, lookups, currencies)
        code = currencies[str(line.get("currency_id"))]
        for account, side, value, chf in parts:
            if side == "debit":
                debit += chf
            else:
                credit += chf
            value, row_factor = _account_amount(lookups, account, code, value, chf, factor)
            rows.append(_row(account, side, value, chf, row_factor, line.get("description") or ""))
    if debit != credit:
        raise ip.MappingError("debit {} and credit {} differ".format(debit, credit))
    day = _day(entry).isoformat()
    return {
        "doctype": JOURNAL, "company": im.COMPANY, "voucher_type": "Journal Entry",
        # ERPNext wants a reference date with a reference number: bexio's entry has none, so it is the entry's date
        "posting_date": day, "cheque_no": reference, "cheque_date": day if reference else None, "user_remark": reference,
        "multi_currency": int(any(row["exchange_rate"] != 1 for row in rows)),
        "bexio_id": manual_key(entry["id"]),
        "accounts": rows,
    }


def _account_amount(lookups, account, code, value, chf, factor):
    """(amount in the account's currency, rate) of a row. ERPNext books a row in its account's currency: a CHF account takes
    the CHF amount at rate 1, since the foreign amount would otherwise be booked as CHF. An account in the line's own
    currency keeps the amount and the rate; an account in any other currency is not mapped.
    """
    currency = lookups.currency_of(account)
    if currency is None or currency == code:
        return value, factor
    if currency == "CHF":
        return chf, _money(1)
    raise ip.MappingError("a {} line on an account in {}".format(code, currency))


def manual_key(entry_id):
    """The bexio_id of a manual entry's Journal Entry. Prefixed, because the VAT fix's Journal Entries carry bare journal
    line ids: an unprefixed manual entry id would find one of them and the loader would skip the entry.
    """
    return "manual-{}".format(entry_id)


def _row(account, side, value, chf, factor, remark):
    """One Journal Entry Account row: the amount in the account's currency, and in CHF with the rate of the line."""
    row = {"account": account, "user_remark": remark, "exchange_rate": float(factor)}
    row["debit" if side == "debit" else "credit"] = float(chf)
    row["debit_in_account_currency" if side == "debit" else "credit_in_account_currency"] = float(value)
    return row


class Totals:
    """Per year: the entries seen, what maps, what is from banking, and the CHF debit of what maps.

    documents are the loader's documents of what maps (the plan of --write); problems list what does not, by bexio id.
    """

    def __init__(self):
        self.rows = {}
        self.problems = []
        self.documents = []

    def row(self, year):
        return self.rows.setdefault(year, {"entries": 0, "mapped": 0, "banking": 0, "unmapped": 0, "debit": ZERO, "banking_debit": ZERO})


def dry_run(entries, lookups, currencies):
    """Map every entry and total it; writes nothing. Returns the Totals."""
    totals = Totals()
    for entry in entries:
        try:
            year = _day(entry).year
        except ip.MappingError:
            year = None
        row = totals.row(year)
        row["entries"] += 1
        try:
            doc = map_entry(entry, lookups, currencies)
        except ip.MappingError as err:
            row["unmapped"] += 1
            totals.problems.append("manual entry {}: unmapped, {}".format(entry.get("id"), err))
            continue
        debit = sum((_money(r.get("debit", 0)) for r in doc["accounts"]), ZERO)
        row["mapped"] += 1
        row["debit"] += debit
        if entry.get("type") == BANKING:
            row["banking"] += 1
            row["banking_debit"] += debit
        totals.documents.append({"doctype": JOURNAL, "name": None, "bexio_id": doc["bexio_id"], "values": doc})
    return totals


def report(totals, export_dir):
    lines = ["manual entries dry run from {}".format(export_dir),
             "{:<6}{:>9}{:>8}{:>9}{:>10}{:>16}{:>16}".format("year", "entries", "mapped", "banking", "unmapped", "debit CHF", "of banking")]
    for year, r in sorted(totals.rows.items(), key=lambda item: (item[0] is None, item[0] or 0)):
        lines.append("{:<6}{:>9}{:>8}{:>9}{:>10}{:>16}{:>16}".format(
            year if year is not None else "?", r["entries"], r["mapped"], r["banking"], r["unmapped"], r["debit"], r["banking_debit"]))
    lines.append("nothing was written to ERPNext")
    return "\n".join(lines)


def load_entries(export_dir):
    """(entries, currency codes by bexio id) of the export, or None when manual_entries.json is not there yet."""
    path = os.path.join(export_dir, ENTRIES_FILE)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)
    with open(os.path.join(export_dir, CURRENCIES_FILE), encoding="utf-8") as f:
        currencies = {str(c["id"]): c["name"] for c in json.load(f)}
    return entries, currencies


def main(argv):
    parser = argparse.ArgumentParser(description="Map the bexio manual entries to ERPNext Journal Entries (dry run).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals")
    parser.add_argument("--write", metavar="FILE", default=None, help="write the loader plan to a private file (bexio-drafts.sh FILE submit does the live run)")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if sum(bool(x) for x in (args.dry_run, args.write is not None)) != 1:
        parser.error("exactly one of --dry-run and --write FILE")
    export_dir = args.export or im.newest_export()
    loaded = load_entries(export_dir)
    if loaded is None:
        print("manual entries: not exported yet ({} is not in {})".format(ENTRIES_FILE, export_dir))
        return 0
    entries, currencies = loaded
    try:
        lookups = Lookups.from_erp(im.Erp.from_file(args.token_file))
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    totals = dry_run(entries, lookups, currencies)
    print(report(totals, export_dir))
    if totals.problems:
        ip.write_private(os.path.join(im.PRIVATE, PROBLEMS_FILE), totals.problems)
        print("{} entry(ies) listed by bexio id in {}".format(len(totals.problems), os.path.join(im.PRIVATE, PROBLEMS_FILE)), file=sys.stderr)
    if args.write:
        # an entry that does not map is left out and listed, never half written
        ip.write_private(args.write, [json.dumps({"documents": totals.documents, "exchange_rates": []}, indent=1)])
        print("{} Journal Entries handed to the loader in {}".format(len(totals.documents), args.write))
    return 1 if any(r["unmapped"] for r in totals.rows.values()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
