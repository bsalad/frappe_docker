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
yet" and stops. --extra FILE adds entries that are not in the export, in
bexio's shape as a list (a correction, say); the file stays private, as the
amounts in it do. The totals are printed only; the bexio ids of the entries
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
code (import_purchase's VAT_OF_TAX_ID). The VAT goes to the account of the code's
kind (VAT_NUMBER_OF_CODE: 2200 for a sales code, 1170 or 1171 for a purchase one),
the account bexio's journal books it on, not to tax_account_id: in a manual-entry
row that names the taxed account itself, and it is only checked to be one of the row's own accounts. A code
without an Item Tax Template in ERPNext is reported, not guessed. Entries of type banking_transaction are
mapped too, but counted apart: the posting plan decides whether they or the
bank transactions carry the booking.

Two shapes the export has beyond the single line. A compound entry has one row
without id: its counterpart, the account each id row is booked against (its
journal line carries it on the other side); the counterpart is not posted
itself, the id rows take it as their missing side. A reverse-charge code
(import_purchase's BEZUG) is net, and its tax goes on the Vorsteuer account of
its kind and is taken back on 2203, as bexio's journal books it for a manual
entry (the bills book 1172 and 2202). An entry with no type is booked as a single one.

An entry on a depreciation account is a Depreciation Entry, as ERPNext requires
for those accounts; an entry on a receivable or payable account needs a party,
which a bexio manual entry does not carry, so it is reported, not written.

Standard library only, apart from import_master and import_purchase.
"""

import argparse
import collections
import datetime
import json
import os
import sys
from decimal import Decimal, ROUND_HALF_UP

import import_master as im
import import_purchase as ip

ENTRIES_FILE = "manual_entries.json"
JOURNAL_FILE = "journal.json"
CURRENCIES_FILE = "currencies.json"
PROBLEMS_FILE = "bexio-manual-entries-dry-run.txt"
JOURNAL_CHECK_FILE = "bexio-manual-entries-journal-check.txt"
# the entries bexio books differently on purpose (D11): their expected side is bexio's journal, see load_journal_wins
JOURNAL_WINS_FILE = "bexio-manual-journal-wins.txt"
JOURNAL = "Journal Entry"
# the bexio journal lines of documents (invoices, bills, credit vouchers, payments): the rest are bank and manual lines
DOCUMENT_CLASSES = ("KbInvoice", "KbBill", "KbCreditVoucher", "KbClientAccountEntry")
# the accounts the journal check compares besides the profit-and-loss ones: the VAT accounts the manual entries book on
CHECK_NUMBERS = ("1170", "1171", "2200")
TOLERANCE = Decimal("0.005")
SINGLE, COMPOUND, GROUP = "manual_single_entry", "manual_compound_entry", "manual_group_entry"
# the type of the entries a bank import created; bexio's docs do not list it, the name is from the mapping notes
BANKING = "banking_transaction"
TYPES = (SINGLE, COMPOUND, GROUP)
# the side a VAT account carries: Vorsteuer (assets) is debited, Umsatzsteuer (liabilities) credited
SIDE_OF_ROOT = {"Asset": "debit", "Expense": "debit", "Liability": "credit", "Income": "credit", "Equity": "credit"}
PROFIT_AND_LOSS = ("Income", "Expense")
# the bexio codes with a rate of 0 % in BEXIO_TAXES (swiss-setup), with the purchase importer's: no VAT split on an entry
MANUAL_ZERO_RATE_IDS = ip.ZERO_RATE_IDS + (3, 4, 5, 6, 13, 14, 48)
# the account a manual entry's VAT is booked on, by the kind of its code: bexio's journal books it there, on the final
# accounts, not on the transitory 1172 or 2202. The sales codes are those of import_master's VAT_OF_TAX_ID (S, rates > 0)
SALES_IDS = (16, 28, 17, 29, 18, 30)
VAT_NUMBER_OF_CODE = dict([(i, "2200") for i in SALES_IDS] + [(i, "1170") for i in ip.MAT_SV_IDS] + [(i, "1171") for i in ip.INV_BA_IDS])
# a reverse-charge code's Vorsteuer by its kind (BZM Material 19 and 33, BZB Investitionen 20 and 32), taken back on 2203:
# the liability bexio's manual entries book the reverse charge on (the bills book 1172 and 2202, import_purchase's BEZUG)
REVERSE_CHARGE_NUMBER_OF_CODE = {19: "1170", 33: "1170", 20: "1171", 32: "1171"}
REVERSE_CHARGE_LIABILITY = "2203"
# the account types ERPNext wants a voucher of its own for (Depreciation Entry), and the ones that need a party
DEPRECIATION_TYPES = ("Depreciation", "Accumulated Depreciation")
DEPRECIATION_ENTRY = "Depreciation Entry"
PARTY_TYPES = ("Receivable", "Payable")
CENT = Decimal("0.01")
ZERO = Decimal("0")


class Lookups:
    """What the mapping reads from ERPNext: Accounts by bexio_id with their root type, Accounts by number (the reverse-charge
    accounts), the currency of each Account, Item Tax Templates by bexio_id, and the names of the depreciation and party accounts."""

    def __init__(self, accounts, taxes, by_number=None, currencies=None, depreciation=None, party=None):
        self.accounts = accounts      # bexio account id -> (Account name, root type)
        self.taxes = taxes            # bexio tax id -> Item Tax Template name
        self.by_number = by_number or {}  # account number -> (Account name, root type)
        self.currencies = currencies or {}  # Account name -> the account's currency
        self.depreciation = set(depreciation or ())  # Account names of the depreciation types
        self.party = set(party or ())  # Account names of the receivable and payable types

    def currency_of(self, account):
        return self.currencies.get(account)

    @classmethod
    def from_erp(cls, erp):
        rows = erp.list("Account", [["company", "=", im.COMPANY], ["bexio_id", "is", "set"]],
                        ["name", "bexio_id", "root_type", "account_currency", "account_type"])
        numbers = sorted(set(VAT_NUMBER_OF_CODE.values()) | set(REVERSE_CHARGE_NUMBER_OF_CODE.values()) | {REVERSE_CHARGE_LIABILITY})
        by_number = erp.list("Account", [["company", "=", im.COMPANY], ["account_number", "in", numbers]],
                             ["name", "account_number", "root_type", "account_currency", "account_type"])
        return cls(
            accounts={r["bexio_id"]: (r["name"], r["root_type"]) for r in rows},
            taxes={r["bexio_id"]: r["name"] for r in erp.list("Item Tax Template", [["company", "=", im.COMPANY], ["bexio_id", "is", "set"]], ["name", "bexio_id"])},
            by_number={r["account_number"]: (r["name"], r["root_type"]) for r in by_number},
            currencies={r["name"]: r["account_currency"] for r in rows + by_number},
            depreciation={r["name"] for r in rows + by_number if r["account_type"] in DEPRECIATION_TYPES},
            party={r["name"] for r in rows + by_number if r["account_type"] in PARTY_TYPES},
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
    """(rate, VAT account name, side of the VAT account) of a line, or None when the line carries no VAT. The account
    comes from the code (VAT_NUMBER_OF_CODE), not from tax_account_id, which _check_tax_accounts only checks."""
    tax_id = line.get("tax_id")
    if not tax_id or tax_id in MANUAL_ZERO_RATE_IDS:
        return None
    if tax_id not in im.VAT_OF_TAX_ID:
        raise ip.MappingError("unknown VAT code {}".format(tax_id))
    if tax_id not in VAT_NUMBER_OF_CODE:
        raise ip.MappingError("no VAT account for code {}".format(tax_id))
    if not lookups.taxes.get(str(tax_id)):
        raise ip.MappingError("no Item Tax Template with bexio_id {} in ERPNext".format(tax_id))
    tax_account, root = _by_number(lookups, VAT_NUMBER_OF_CODE[tax_id])
    if root not in SIDE_OF_ROOT:
        raise ip.MappingError("VAT account {} has root type {}".format(VAT_NUMBER_OF_CODE[tax_id], root))
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


def _check_tax_accounts(lines):
    """A VAT row's tax_account_id is one of the row's own accounts, as bexio writes it. It is checked before a compound
    row takes its counterpart as the missing side: a row that names another account is listed, not guessed."""
    for line in lines:
        tax_id = line.get("tax_id")
        if not tax_id or tax_id in MANUAL_ZERO_RATE_IDS or tax_id in ip.BEZUG:
            continue
        own = {str(line[side]) for side in ("debit_account_id", "credit_account_id") if line.get(side) is not None}
        if str(line.get("tax_account_id")) not in own:
            raise ip.MappingError("tax account {} is not an account of its row".format(line.get("tax_account_id")))


def _reverse_charge(line, lookups, debit, credit, amount, factor, side):
    """The parts of a reverse-charge line (Bezugsteuer): the amount is net, and the tax is booked on the Vorsteuer account
    of its kind and taken back on 2203, as bexio's journal books a manual entry (its net is the same on both sides). On
    the credit side (a reversal) the tax goes the other way, as ordinary VAT does."""
    rate = ip.BEZUG[line["tax_id"]][1]
    vorsteuer, _ = _by_number(lookups, REVERSE_CHARGE_NUMBER_OF_CODE[line["tax_id"]])
    bezugsteuer, _ = _by_number(lookups, REVERSE_CHARGE_LIABILITY)
    tax = (amount * _money(rate) / 100).quantize(CENT, rounding=ROUND_HALF_UP)
    other = "credit" if side == "debit" else "debit"
    parts = [(debit, "debit", amount, _chf(amount, factor)), (credit, "credit", amount, _chf(amount, factor)),
             (vorsteuer, side, tax, _chf(tax, factor)), (bezugsteuer, other, tax, _chf(tax, factor))]
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
        # the expense or income side decides, as for ordinary VAT: a reversal credits the expense and takes the tax back
        side = "credit" if credit_root in PROFIT_AND_LOSS and debit_root not in PROFIT_AND_LOSS else "debit"
        return _reverse_charge(line, lookups, debit, credit, amount, factor, side), factor
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
    _check_tax_accounts(lines)
    for line in _postings(kind, lines):
        parts, factor = _line_parts(line, lookups, currencies)
        code = currencies[str(line.get("currency_id"))]
        for account, side, value, chf in parts:
            if account in lookups.party:
                raise ip.MappingError("account {} needs a party, and a manual entry has none".format(account))
            if side == "debit":
                debit += chf
            else:
                credit += chf
            value, row_factor = _account_amount(lookups, account, code, value, chf, factor)
            rows.append(_row(account, side, value, chf, row_factor, line.get("description") or ""))
    if debit != credit:
        raise ip.MappingError("debit {} and credit {} differ".format(debit, credit))
    day = _day(entry).isoformat()
    depreciation = any(row["account"] in lookups.depreciation for row in rows)
    return {
        "doctype": JOURNAL, "company": im.COMPANY,
        "voucher_type": DEPRECIATION_ENTRY if depreciation else "Journal Entry",
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


def entry_of_lines(entries, journal):
    """{journal line id: manual entry id} of the bexio journal lines an entry books: a line whose id is one of an entry's
    row ids, and a VAT line of a row, found by the entry's date and the row's description (posting_plan's pairing). A
    line that two entries could claim is left out. The document lines (invoices, bills, payments) are not an entry's."""
    by_id, by_text, rows_by_text = {}, collections.defaultdict(set), collections.defaultdict(list)
    for entry in entries:
        for row in entry.get("entries") or []:
            if row.get("id") is not None:
                by_id[row["id"]] = str(entry["id"])
                key = (str(entry["date"])[:10], row.get("description"))
                by_text[key].add(str(entry["id"]))
                rows_by_text[key].append(row)
    amounts = {line["id"]: _money(line["base_currency_amount"]) for line in journal}
    owner = {}
    for line in journal:
        if line["ref_class"] in DOCUMENT_CLASSES:
            continue
        if line["id"] in by_id:
            owner[line["id"]] = by_id[line["id"]]
            continue
        key = (line["date"][:10], line.get("description"))
        claimants = by_text.get(key, set())
        if len(claimants) == 1:
            owner[line["id"]] = next(iter(claimants))
        elif by_id.get(line["id"] - 1) in claimants:
            # two entries of the day carry the same text; bexio numbers a row's VAT line right after the row, so the row before decides
            owner[line["id"]] = by_id[line["id"] - 1]
        else:
            # bexio can book a row's VAT line later, after other rows: then the row whose gross is its net line and this line decides
            grossed = {by_id[row["id"]] for row in rows_by_text[key]
                       if row["id"] in amounts and _gross_matches(row, amounts[row["id"]], amounts[line["id"]])}
            if len(grossed) == 1:
                owner[line["id"]] = next(iter(grossed))
    return owner


def _gross_matches(row, net, vat):
    """Whether the row's amount is its journal net line plus the VAT line (base currency, to the tolerance)."""
    gross = _money(row.get("base_currency_amount", row.get("amount")))
    return abs(abs(gross) - abs(net) - abs(vat)) <= TOLERANCE


def load_journal_wins(path):
    """The entries bexio books differently on purpose, from the private file: one bexio id per line, then its reason.
    {bexio id: reason}. Blank lines and lines starting with # are skipped; a missing file lists none."""
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


def journal_check(entries, totals, journal, lookups, wins=None):
    """The mapping against bexio's journal, per manual entry and account (CHF, debit minus credit). entries are the export's
    own (not the --extra ones, which bexio does not book). Returns (problems, per year: [accounts compared, accounts that
    differ, the sum of the differences in CHF], entries compared, journal wins, unmapped entries).

    The accounts compared are 1170, 1171, 2200 and every profit-and-loss account. An entry in wins (load_journal_wins) is not
    compared: bexio books it differently on purpose, so it is counted as a journal win, not as a difference. An entry bexio
    books that the mapping does not hold (an unmapped one, D10) has no expected side: it is counted apart as unmapped, an
    open import item, not as a difference.
    """
    wins = wins or {}
    owner = entry_of_lines(entries, journal)
    exported = {manual_key(entry["id"]) for entry in entries}
    names = {str(bexio_id): name for bexio_id, (name, _) in lookups.accounts.items()}
    roots = {name: root for name, root in lookups.accounts.values()}
    mapped, booked = collections.defaultdict(collections.Counter), collections.defaultdict(collections.Counter)
    years = {}
    for document in totals.documents:
        if document["bexio_id"] not in exported:
            continue
        entry_id = document["bexio_id"][len("manual-"):]
        years[entry_id] = document["values"]["posting_date"][:4]
        for row in document["values"]["accounts"]:
            mapped[entry_id][row["account"]] += _money(row.get("debit", 0)) - _money(row.get("credit", 0))
    for line in journal:
        entry_id = owner.get(line["id"])
        if entry_id is None:
            continue
        amount = _money(line["base_currency_amount"])
        debit, credit = names.get(str(line["debit_account_id"])), names.get(str(line["credit_account_id"]))
        booked[entry_id][debit] += amount
        booked[entry_id][credit] -= amount
        years.setdefault(entry_id, str(line["date"])[:4])
    problems, per_year = [], collections.defaultdict(lambda: [0, 0, ZERO])
    seen = set(mapped) | set(booked)
    won = {entry_id for entry_id in seen if entry_id in wins}
    unmapped = {entry_id for entry_id in seen - won if entry_id not in mapped}
    for entry_id in sorted(seen - won - unmapped, key=str):
        year = years.get(entry_id)
        for account in sorted(set(mapped[entry_id]) | set(booked[entry_id]), key=str):
            if account is None:
                problems.append("entry {} ({}): a bexio account has no Account in ERPNext".format(entry_id, year))
                continue
            if not (account.split()[0] in CHECK_NUMBERS or roots.get(account) in PROFIT_AND_LOSS):
                continue
            want, have = mapped[entry_id][account], booked[entry_id][account]
            cell = per_year[year]
            cell[0] += 1
            if abs(want - have) > TOLERANCE:
                cell[1] += 1
                cell[2] += abs(want - have)
                problems.append("entry {} ({}): {} mapped {:.2f} bexio {:.2f}".format(entry_id, year, account, want, have))
    return problems, dict(per_year), len(seen - won - unmapped), len(won), len(unmapped)


def check_report(per_year, compared, unexplained, wins, unmapped):
    """The journal check as totals per year: accounts compared, accounts that differ, and the sum of the differences. The
    header counts the entries compared, the differences left unexplained, the journal wins (D11) and the unmapped entries (D10)."""
    header = ("manual entries against bexio's journal: {} entries compared, {} unexplained, {} journal wins (D11), "
              "{} unmapped (D10), nothing was written")
    lines = [header.format(compared, unexplained, wins, unmapped),
             "{:<6}{:>10}{:>10}{:>16}".format("year", "accounts", "differ", "sum of differences")]
    for year, (count, differ, total) in sorted(per_year.items(), key=lambda item: (item[0] is None, item[0] or "")):
        lines.append("{:<6}{:>10}{:>10}{:>16,.2f}".format(year if year is not None else "?", count, differ, total))
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


def load_extra(path):
    """The entries of a private file that the export does not hold: a list in bexio's shape, as manual_entries.json."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv):
    parser = argparse.ArgumentParser(description="Map the bexio manual entries to ERPNext Journal Entries (dry run).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals")
    parser.add_argument("--write", metavar="FILE", default=None, help="write the loader plan to a private file (bexio-drafts.sh FILE submit does the live run)")
    parser.add_argument("--extra", metavar="FILE", default=None, help="entries that are not in the export, in bexio's shape, as a private list")
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
    exported = entries
    if args.extra:
        entries = entries + load_extra(args.extra)
    try:
        lookups = Lookups.from_erp(im.Erp.from_file(args.token_file))
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    totals = dry_run(entries, lookups, currencies)
    print(report(totals, export_dir))
    with open(os.path.join(export_dir, JOURNAL_FILE), encoding="utf-8") as f:
        journal = json.load(f)
    wins = load_journal_wins(os.path.join(im.PRIVATE, JOURNAL_WINS_FILE))
    check_problems, per_year, compared, won, unmapped = journal_check(exported, totals, journal, lookups, wins)
    print(check_report(per_year, compared, len(check_problems), won, unmapped))
    ip.write_private(os.path.join(im.PRIVATE, JOURNAL_CHECK_FILE), check_problems or ["none"])
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
