"""Map the exported bexio bank transactions to ERPNext Bank Transaction, offline, and match them to their bookings.

Step four of the bexio pipeline, after import_master.py has made the Bank Accounts (keyed by bexio_id). Each
function takes one export record and the lookups and returns the ERPNext Bank Transaction as a dict, not yet
inserted. nothing is written to ERPNext here: --dry-run reads ERPNext and prints totals only; --write keeps the
documents and their reconciliations in a private file for the loader (bexio-drafts.py, submit mode).

The export's bank_transactions.json has amount unsigned (always positive) and type CREDIT (money in) or DEBIT
(money out). status says whether bexio has booked the transaction: reconciled and auto_reconciled are booked
(against a payment or a banking entry, which the export does not say), unreconciled and ignored are not. The
export has no field that names the booking, so the link is found by match: a booked transaction, or one bexio never
reconciled, reconciles against the one ERPNext voucher of its bank account and amount near its date, or else by its
text, or against a combined transfer of the vouchers near its date (see match). A Bank Transaction posts no GL itself;
reconciling it posts nothing.

A record that cannot be mapped (an unknown bank account, a currency other than its account's, a zero amount)
raises Unmapped, and the report names it by bexio id only. Amounts stay in the transaction's currency: no
conversion, so no CHF totals here.

Run it as:

    python3 finance/bexio/import_bank.py --dry-run [--export DIR] [--balances]
    python3 finance/bexio/import_bank.py --write <private>/bexio-bank-docs.json [--export DIR]
    python3 finance/bexio/import_bank.py --check [--export DIR]     (after the loader ran: ERPNext against the dry run)

--export defaults to the newest directory under <private>/bexio-export/. The output is totals only; the unmapped
and unmatched records go to files under <private>, never to the repository. Standard library only, plus import_master
and posting_plan.
"""

import argparse
import collections
import datetime
import itertools
import json
import os
import re
import sys
from decimal import Decimal

import import_master as im
import posting_plan as pp

COMPANY = im.COMPANY
TRANSACTIONS = "bank_transactions"
VOUCHER_TYPES = ("Payment Entry", "Journal Entry")

# bexio's status values: these two mean bexio has booked the transaction
BOOKED = ("reconciled", "auto_reconciled")

# the transactions that are matched: the booked ones and the ones bexio never reconciled (their booking is in the GL too);
# ignored ones are out of scope
CANDIDATE = BOOKED + ("unreconciled",)

# the fields that name a voucher in its text: a bank line's title is matched against them (pass text)
VOUCHER_TEXT = {"Payment Entry": ("reference_no", "party_name", "remarks"), "Journal Entry": ("title", "user_remark")}

# the days either side of a bank line's value date or book date that a voucher may fall in (pass window, pass combined)
WINDOW_DAYS = 5

# the shortest name from a voucher's text that counts as a mention in a bank line's title (pass text)
TEXT_MIN = 4

# the passes a transaction is reconciled by, in the order they run; each is counted apart
PASS_ONE = "one voucher"
PASS_WINDOW = "date window"
PASS_TEXT = "text"
PASS_COMBINED = "combined transfer"

# journal entries posted under a key that is a correction, not a bank booking (erp-fd93: the bexio 1099 entry's correction)
CORRECTION_KEYS = ("manual-1099-correction",)

# the most vouchers of one account and window a combined transfer is searched in (2^12 subsets at most)
COMBINE_LIMIT = 12

ZERO = Decimal("0")
CENT = Decimal("0.01")


class Unmapped(Exception):
    """A record the importer does not map. The message names the reason, never a company name."""


def _dec(value):
    return Decimal(str(value))


def cents(value):
    return _dec(value).quantize(CENT)


def bank_transaction(record, lookups):
    """The ERPNext Bank Transaction of a bexio banking transaction, in its account's currency."""
    account = lookups["bank_account"].get(str(record.get("bank_account_id")))
    if account is None:
        raise Unmapped("bank account {} has no Bank Account with that bexio_id".format(record.get("bank_account_id")))
    if account["currency"] is None:
        raise Unmapped("bank account {} has no currency in the export".format(record.get("bank_account_id")))
    currency = lookups["currency"].get(str(record.get("currency_id")))
    if currency is None:
        raise Unmapped("currency {} is not in the export".format(record.get("currency_id")))
    if currency != account["currency"]:
        raise Unmapped("transaction currency {} differs from its bank account's {}".format(currency, account["currency"]))
    if not record.get("value_date"):
        raise Unmapped("no value date")
    amount = _dec(record["amount"])
    if amount == ZERO:
        raise Unmapped("zero amount")
    if record.get("type") not in ("CREDIT", "DEBIT"):
        raise Unmapped("type {} is neither CREDIT nor DEBIT".format(record.get("type")))

    # the export's amount is unsigned; its type gives the direction, which ERPNext keeps in two columns
    deposit = amount if record["type"] == "CREDIT" else ZERO
    withdrawal = amount if record["type"] == "DEBIT" else ZERO
    return {
        "doctype": "Bank Transaction", "company": COMPANY, "bexio_id": str(record["id"]),
        "date": record["value_date"], "bank_account": account["name"], "currency": currency,
        # the day the bank booked it; without one in the export, the value date (Desk lists by Booking Date)
        "booking_date": (record.get("book_date") or "")[:10] or record["value_date"],
        "deposit": float(deposit), "withdrawal": float(withdrawal),
        "description": record.get("title") or "", "reference_number": "",
    }


def plan(records, lookups, meta=None):
    """Map every record of the export. Nothing is written; returns one result per record."""
    results = []
    for record in records:
        result = {"bexio_id": str(record.get("id")), "account": str(record.get("bank_account_id")),
                  "year": str(record.get("value_date") or "????")[:4], "doc": None, "error": None,
                  "unknown": [], "currency": None, "in": ZERO, "out": ZERO,
                  "booked": record.get("status") in BOOKED, "candidate": record.get("status") in CANDIDATE,
                  "book_date": (record.get("book_date") or "")[:10] or None}
        try:
            doc = bank_transaction(record, lookups)
        except Unmapped as err:
            result["error"] = str(err)
        else:
            result.update(doc=doc, currency=doc["currency"], **{"in": _dec(doc["deposit"]), "out": _dec(doc["withdrawal"])})
            if meta:
                result["unknown"] = unknown_fields(doc, meta)
        results.append(result)
    return results


def unknown_fields(doc, meta):
    """Fields of the document that the ERPNext doctype does not have."""
    return sorted("Bank Transaction.{}".format(key) for key in doc if key != "doctype" and key not in meta)


def doctype_fields(erp, doctype):
    """The field names of a doctype, custom fields included (import_sales has the same, but is edited in parallel)."""
    return {f["fieldname"] for f in erp.meta(doctype)["fields"]}


def load_export(path):
    """The export files this module reads: the bank accounts and the currencies (the transactions are read by main)."""
    data = {}
    for name in ("bank_accounts", "currencies"):
        with open(os.path.join(path, name + ".json"), encoding="utf-8") as f:
            data[name] = json.load(f)
    return data


def lookups_from_erp(erp, data):
    """The Bank Accounts by bexio_id, read from ERPNext, with the currency the export gives each one and its GL account."""
    rows = {r["bexio_id"]: r for r in erp.list("Bank Account", [["bexio_id", "is", "set"]], ["name", "bexio_id", "account"])}
    currency = {str(c["id"]): c["name"] for c in data["currencies"]}
    accounts = {}
    for b in data["bank_accounts"]:
        row = rows.get(str(b["id"]))
        if row is not None:
            accounts[str(b["id"])] = {"name": row["name"], "currency": currency.get(str(b["currency_id"])), "account": row["account"]}
    return {"currency": currency, "bank_account": accounts}


def read_vouchers(erp, gl_accounts):
    """The bank-side lines of the submitted Payment Entries and Journal Entries that carry a bexio_id.

    One voucher per voucher and GL account and date, with its net debit (money in positive). The amounts come from the
    GL: the API user cannot read the rows of a Journal Entry, and the GL is what a Bank Transaction allocates against.
    """
    keys, texts = {}, {}
    for doctype in VOUCHER_TYPES:
        fields = ("name", "bexio_id") + VOUCHER_TEXT[doctype]
        for row in erp.list(doctype, [["bexio_id", "is", "set"], ["docstatus", "=", 1]], fields):
            keys[(doctype, row["name"])] = row["bexio_id"]
            texts[(doctype, row["name"])] = [row[f] for f in VOUCHER_TEXT[doctype] if row.get(f)]
    gl = erp.list("GL Entry", [["account", "in", gl_accounts], ["is_cancelled", "=", 0],
                               ["voucher_type", "in", list(VOUCHER_TYPES)]],
                  ["voucher_type", "voucher_no", "account", "posting_date", "debit", "credit"])
    net = collections.OrderedDict()
    for line in gl:
        key = (line["voucher_type"], line["voucher_no"], line["account"], line["posting_date"])
        net[key] = net.get(key, ZERO) + cents(line["debit"]) - cents(line["credit"])
    vouchers = []
    for (doctype, name, account, date), amount in net.items():
        bexio_id = keys.get((doctype, name))
        if bexio_id is None or bexio_id in CORRECTION_KEYS or amount == ZERO:
            continue
        vouchers.append({"doctype": doctype, "name": name, "bexio_id": bexio_id, "account": account, "date": date,
                         "amount": amount, "text": texts[(doctype, name)]})
    return vouchers


def read_allocated(erp):
    """The vouchers already allocated to a Bank Transaction in ERPNext: {(doctype, name): bexio_id of that transaction}.

    Read per reconciled transaction, since the allocation is a child table of the Bank Transaction.
    """
    allocated = {}
    for row in erp.list("Bank Transaction", [["bexio_id", "is", "set"], ["status", "!=", "Unreconciled"]], ["name", "bexio_id"]):
        for payment in erp.get("Bank Transaction", row["name"]).get("payment_entries") or []:
            allocated[(payment["payment_document"], payment["payment_entry"])] = row["bexio_id"]
    return allocated


def match_transactions(results, lookups):
    """The mapped transactions as match takes them: the GL account, the value and book dates, the amount in minus out, and the text."""
    return [{"bexio_id": r["bexio_id"], "account": lookups["bank_account"][r["account"]]["account"],
             "date": r["doc"]["date"], "book_date": r["book_date"], "amount": cents(r["in"] - r["out"]),
             "text": r["doc"]["description"], "candidate": r["candidate"], "booked": r["booked"]}
            for r in results if r["doc"]]


def _key(voucher):
    return (voucher["doctype"], voucher["name"])


def _near(voucher, transaction):
    """Whether the voucher's date is within WINDOW_DAYS of the transaction's value date or book date."""
    day = datetime.date.fromisoformat(voucher["date"])
    return any(abs((day - datetime.date.fromisoformat(d[:10])).days) <= WINDOW_DAYS
               for d in (transaction["date"], transaction["book_date"]) if d)


def _words(text):
    return " ".join(re.findall(r"\w+", text.lower()))


def _named(voucher, title):
    """Whether a name in the voucher's text (its reference, party or remark) appears as words in the bank line's title."""
    bank = " " + _words(title) + " "
    return any(len(_words(name)) >= TEXT_MIN and " " + _words(name) + " " in bank for name in voucher["text"])


def match(transactions, vouchers, allocated=None):
    """The candidate transactions and the vouchers each reconciles against: {bexio_id: (pass, vouchers, reason)}.

    A voucher allocated in ERPNext to another transaction is no candidate (allocated: {(doctype, name): bexio_id}); the
    transaction it is allocated to keeps it. The passes, each only on a unique match, in this order:
    one voucher of the transaction's GL account, date and amount; then the only voucher of the account and amount within
    WINDOW_DAYS of its value or book date, and near no other open transaction of that account and amount; then, among
    several of the same day, the only one whose name appears in the bank line's title; then the only combination of the
    vouchers in that window (Payment Entries and Journal Entries) whose sum is its amount, a transfer that pays several
    bills, and whose vouchers are near no other open transaction of that account and direction. A transaction with no
    such match keeps pass None and the reason. A voucher that two transactions claim takes neither of them.
    """
    allocated = allocated or {}

    def free(voucher, bexio_id):
        return allocated.get(_key(voucher), bexio_id) == bexio_id

    by_key = collections.defaultdict(list)
    for v in vouchers:
        by_key[(v["account"], v["date"], v["amount"])].append(v)
    result, single, pending = {}, set(), []
    for t in transactions:
        if not t["candidate"]:
            continue
        found = [v for v in by_key.get((t["account"], t["date"], t["amount"]), []) if free(v, t["bexio_id"])]
        if len(found) == 1:
            result[t["bexio_id"]] = (PASS_ONE, found, "")
            single.add(_key(found[0]))
        else:
            pending.append(t)

    def rivals(voucher, same):
        """The open transactions that same selects and the voucher lies near, the one asking included; none for a voucher
        ERPNext already holds for its line, since no other line can take it."""
        if _key(voucher) in allocated:
            return []
        return [s for s in pending if same(s) and _near(voucher, s)]

    for t in pending:
        pool = [v for v in vouchers if _key(v) not in single and free(v, t["bexio_id"])]
        window = [v for v in pool if v["account"] == t["account"] and v["amount"] == t["amount"] and _near(v, t)]
        same_day = [v for v in window if v["date"] == t["date"]]
        day = [v for v in pool if v["account"] == t["account"] and _near(v, t)
               and (v["amount"] > ZERO) == (t["amount"] > ZERO)]
        combos = []
        if len(day) <= COMBINE_LIMIT:
            combos = [c for n in range(2, len(day) + 1) for c in itertools.combinations(day, n)
                      if sum((v["amount"] for v in c), ZERO) == t["amount"]]
        named = [v for v in same_day if _named(v, t["text"])]
        # a voucher that lies within the window of another open line of its account and amount is not this line's alone
        window_rivals = len(rivals(window[0], lambda s: s["account"] == t["account"] and s["amount"] == t["amount"])) if len(window) == 1 else 0
        # the same for the payments of a combination: no other open line of their account and direction lies near them
        combo_shared = len(combos) == 1 and any(
            len(rivals(v, lambda s: s["account"] == t["account"] and (s["amount"] > ZERO) == (v["amount"] > ZERO))) > 1
            for v in combos[0])
        if len(window) == 1 and window_rivals > 1:
            result[t["bexio_id"]] = (None, [], "{} lines within the window of that voucher".format(window_rivals))
        elif len(window) == 1:
            result[t["bexio_id"]] = (PASS_WINDOW, window, "")
        elif len(same_day) > 1 and len(named) == 1:
            result[t["bexio_id"]] = (PASS_TEXT, named, "")
        elif len(combos) == 1 and combo_shared:
            result[t["bexio_id"]] = (None, [], "a payment of its only combination lies within the window of another open line")
        elif len(combos) == 1:
            result[t["bexio_id"]] = (PASS_COMBINED, list(combos[0]), "")
        elif len(combos) > 1:
            result[t["bexio_id"]] = (None, [], "{} combinations of the payments near its date".format(len(combos)))
        elif len(day) > COMBINE_LIMIT:
            result[t["bexio_id"]] = (None, [], "{} payments near its date: too many to combine".format(len(day)))
        elif len(same_day) > 1:
            result[t["bexio_id"]] = (None, [], "{} candidates, none or several named in its text".format(len(same_day)))
        elif len(window) > 1:
            result[t["bexio_id"]] = (None, [], "{} candidates within {} days".format(len(window), WINDOW_DAYS))
        elif any(v["account"] == t["account"] and v["amount"] == t["amount"] for v in pool):
            result[t["bexio_id"]] = (None, [], "amount only on other dates, none within {} days".format(WINDOW_DAYS))
        else:
            result[t["bexio_id"]] = (None, [], "no voucher of that amount on the account")

    claims = collections.Counter(_key(v) for pass_, found, _ in result.values() if pass_ for v in found)
    for bexio_id, (pass_, found, _) in list(result.items()):
        if pass_ and any(claims[_key(v)] > 1 for v in found):
            result[bexio_id] = (None, [], "a voucher that another transaction also matches")
    return result


def kept_allocations(matches, allocated):
    """The Bank Transactions already reconciled in ERPNext whose match does not name the same vouchers, by bexio id."""
    owned = collections.defaultdict(set)
    for key, bexio_id in allocated.items():
        owned[bexio_id].add(key)
    return sorted(bexio_id for bexio_id, keys in owned.items()
                  if not (matches.get(bexio_id, (None,))[0] and {_key(v) for v in matches[bexio_id][1]} == keys))


def reconcile_lines(matches):
    """What the loader reconciles: each matched transaction with its vouchers, by doctype and name."""
    return [{"bexio_id": bexio_id, "vouchers": [{"doctype": v["doctype"], "name": v["name"]} for v in found]}
            for bexio_id, (pass_, found, _) in sorted(matches.items()) if pass_]


def unmatched_lines(results, matches):
    """The candidate transactions that stay Unreconciled, by bexio id and reason."""
    return ["Bank Transaction {}: {}".format(r["bexio_id"], matches[r["bexio_id"]][2])
            for r in results if r["doc"] and r["candidate"] and not matches[r["bexio_id"]][0]]


def match_summary(results, matches, done=()):
    """Counts per bank account, year and bexio status: the transactions already reconciled in ERPNext (done), each pass, and open."""
    columns = (PASS_ONE, PASS_WINDOW, PASS_TEXT, PASS_COMBINED)
    per = collections.OrderedDict()
    for r in sorted((r for r in results if r["doc"] and r["candidate"]), key=lambda r: (r["account"], r["year"], not r["booked"])):
        group = "bexio booked" if r["booked"] else "bexio unreconciled"
        count = per.setdefault((r["account"], r["year"], group), collections.Counter())
        if r["bexio_id"] in done:
            count["already"] += 1
        else:
            count[matches[r["bexio_id"]][0] or "open"] += 1
    lines = ["{:<12}{:<6}{:<20}{:>9}{:>12}{:>13}{:>8}{:>10}{:>8}".format(
        "bank account", "year", "bexio status", "already", "one voucher", "date window", "text", "combined", "open")]
    for (account, year, group), count in per.items():
        lines.append("{:<12}{:<6}{:<20}{:>9}{:>12}{:>13}{:>8}{:>10}{:>8}".format(
            account, year, group, count["already"], *[count[c] for c in columns], count["open"]))
    reasons = collections.Counter(line.split(": ", 1)[1] for line in unmatched_lines(results, matches))
    for reason, n in sorted(reasons.items()):
        lines.append("open, {}: {}".format(reason, n))
    return "\n".join(lines)


def summary(results):
    """The totals of a dry run, per bank account, year and currency; no names, no amounts converted."""
    per = collections.OrderedDict()
    for r in sorted((r for r in results if r["doc"]), key=lambda r: (r["account"], r["year"], r["currency"])):
        acc = per.setdefault((r["account"], r["year"], r["currency"]), [0, ZERO, ZERO])
        acc[0] += 1
        acc[1] += r["in"]
        acc[2] += r["out"]

    lines = ["{:<12}{:<6}{:<6}{:>8}{:>16}{:>16}".format("bank account", "year", "curr", "count", "sum in", "sum out")]
    for (account, year, currency), (count, sum_in, sum_out) in per.items():
        lines.append("{:<12}{:<6}{:<6}{:>8}{:>16,.2f}{:>16,.2f}".format(
            account, year, currency, count, sum_in, sum_out))
    unmapped = sum(1 for r in results if r["error"])
    lines.append("")
    lines.append("records: {}, mapped: {}, unmapped: {}".format(len(results), len(results) - unmapped, unmapped))
    booked = sum(1 for r in results if r["doc"] and r["booked"])
    lines.append("booked in bexio (reconciled or auto_reconciled): {}, not booked: {}".format(
        booked, sum(1 for r in results if r["doc"]) - booked))
    missing = collections.Counter(field for r in results for field in r["unknown"])
    for field, count in sorted(missing.items()):
        lines.append("ERPNext has no field {} ({} records): the posting plan decides it".format(field, count))
    return "\n".join(lines)


def detail_lines(results):
    """The unmapped records, by bexio id, for the private report file."""
    return ["Bank Transaction {}: unmapped: {}".format(r["bexio_id"], r["error"]) for r in results if r["error"]]


def account_number(gl_account):
    """The number of a GL account from its name: '1020 - UBS Kontokorrent' gives 1020."""
    return gl_account.split(" ", 1)[0]


def year_end_balances(lines, years):
    """The balance on each account at the end of each year: {(number, year): Decimal}, from (number, date, debit minus credit)."""
    totals = collections.defaultdict(lambda: ZERO)
    for number, date, amount in lines:
        totals[(number, date[:4])] += amount
    balances = {}
    for number in {number for number, _ in totals}:
        running = ZERO
        for year in years:
            running += totals.get((number, year), ZERO)
            balances[(number, year)] = running
    return balances


def journal_bank_lines(journal, accounts, numbers):
    """bexio's journal lines on the bank accounts, in CHF, as (number, date, debit minus credit).

    Carry-forward lines are left out: they copy balances the postings already make (posting_plan, decision D1).
    """
    ids = {str(a["id"]): str(a["account_no"]) for a in accounts}
    lines = []
    for line in journal:
        if pp._is_carry_forward(line):
            continue
        amount = cents(line.get("base_currency_amount") or 0)
        for side, sign in (("debit_account_id", 1), ("credit_account_id", -1)):
            number = ids.get(str(line[side]))
            if number in numbers:
                lines.append((number, line["date"][:10], sign * amount))
    return lines


def balance_lines(erp, lookups, export_dir):
    """Per bank account and year end: the ERPNext ledger balance (GL), bexio's journal balance, and the difference, in CHF."""
    gl_accounts = sorted({a["account"] for a in lookups["bank_account"].values()})
    gl = erp.list("GL Entry", [["account", "in", gl_accounts], ["is_cancelled", "=", 0]],
                  ["account", "posting_date", "debit", "credit"])
    erp_lines = [(account_number(r["account"]), r["posting_date"], cents(r["debit"]) - cents(r["credit"])) for r in gl]
    with open(os.path.join(export_dir, "journal.json"), encoding="utf-8") as f:
        journal = json.load(f)
    with open(os.path.join(export_dir, "accounts.json"), encoding="utf-8") as f:
        accounts = json.load(f)
    numbers = {account_number(a) for a in gl_accounts}
    journal_part = journal_bank_lines(journal, accounts, numbers)
    years = sorted({date[:4] for _, date, _ in erp_lines + journal_part})
    ledger = year_end_balances(erp_lines, years)
    bexio = year_end_balances(journal_part, years)
    lines = ["{:<10}{:<6}{:>18}{:>18}{:>14}".format("account", "year", "ERPNext GL", "bexio journal", "difference")]
    for number, year in sorted(ledger):
        lines.append("{:<10}{:<6}{:>18,.2f}{:>18,.2f}{:>14,.2f}".format(
            number, year, ledger[(number, year)], bexio.get((number, year), ZERO),
            ledger[(number, year)] - bexio.get((number, year), ZERO)))
    return "\n".join(lines)


def _add(totals, key, sum_in, sum_out, reconciled):
    row = totals.setdefault(key, [0, ZERO, ZERO, 0])
    row[0] += 1
    row[1] += sum_in
    row[2] += sum_out
    row[3] += int(reconciled)


def live_check(erp_rows, results, matches, lookups):
    """The Bank Transactions in ERPNext against the dry run, per bank account and year: (table lines, difference lines).

    A document is right when it is submitted, with the account, date, deposit and withdrawal of its mapping, and status
    Reconciled when the match found its vouchers, Unreconciled otherwise. The table shows each figure as dry run / ERPNext;
    the differences name bexio ids only.
    """
    names = {v["name"]: k for k, v in lookups["bank_account"].items()}
    live = {row["bexio_id"]: row for row in erp_rows}
    dry, found = collections.OrderedDict(), collections.OrderedDict()
    differences = []
    for r in sorted((r for r in results if r["doc"]), key=lambda r: (r["account"], r["year"])):
        doc = r["doc"]
        reconciled = bool(r["candidate"] and matches[r["bexio_id"]][0])
        _add(dry, (r["account"], r["year"]), r["in"], r["out"], reconciled)
        row = live.get(r["bexio_id"])
        if row is None:
            differences.append("Bank Transaction {}: not in ERPNext".format(r["bexio_id"]))
            continue
        _add(found, (names.get(row["bank_account"], "?"), row["date"][:4]), cents(row["deposit"]), cents(row["withdrawal"]),
             row["status"] == "Reconciled")
        same = (row["docstatus"] == 1 and row["bank_account"] == doc["bank_account"] and row["date"] == doc["date"]
                and cents(row["deposit"]) == cents(doc["deposit"]) and cents(row["withdrawal"]) == cents(doc["withdrawal"])
                and row["status"] == ("Reconciled" if reconciled else "Unreconciled"))
        if not same:
            differences.append("Bank Transaction {}: differs (status {}, submitted {})".format(
                r["bexio_id"], row["status"], row["docstatus"] == 1))
    seen = {r["bexio_id"] for r in results if r["doc"]}
    differences.extend("Bank Transaction {}: in ERPNext, not in the export".format(bexio_id)
                       for bexio_id in sorted(set(live) - seen))

    lines = ["{:<10}{:<6}{:>14}{:>16}{:>34}{:>34}".format("account", "year", "count", "reconciled", "sum in", "sum out")]
    for key in sorted(set(dry) | set(found)):
        d = dry.get(key, [0, ZERO, ZERO, 0])
        e = found.get(key, [0, ZERO, ZERO, 0])
        lines.append("{:<10}{:<6}{:>14}{:>16}{:>34}{:>34}".format(
            key[0], key[1], "{} / {}".format(d[0], e[0]), "{} / {}".format(d[3], e[3]),
            "{:,.2f} / {:,.2f}".format(d[1], e[1]), "{:,.2f} / {:,.2f}".format(d[2], e[2])))
    lines.append("differences by bexio id: {}".format(len(differences)))
    return lines, differences


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text + "\n")


def write_documents(results, path, reconcile=()):
    """The mapped Bank Transactions and their reconciliations, as the loader takes them, into the private file; returns the number of documents.

    Each document is keyed by bexio_id; the loader inserts and submits it, then reconciles the ones in reconcile.
    """
    documents = [{"doctype": r["doc"]["doctype"], "bexio_id": r["bexio_id"], "booked": r["booked"],
                  "values": {k: v for k, v in r["doc"].items() if k != "doctype"}}
                 for r in results if r["doc"]]
    write_private(path, json.dumps({"documents": documents, "reconcile": list(reconcile)}, indent=1))
    return len(documents)


def main(argv):
    parser = argparse.ArgumentParser(description="Map the exported bexio bank transactions to ERPNext and match them to their bookings (nothing is written to ERPNext).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals and the match counts")
    parser.add_argument("--write", metavar="FILE", help="also write the mapped documents and their reconciliations to a private file for the loader")
    parser.add_argument("--balances", action="store_true", help="read ERPNext and the export, print each bank account's year-end balance against bexio's journal")
    parser.add_argument("--check", action="store_true", help="read the Bank Transactions in ERPNext and compare them with the dry run (after the write)")
    parser.add_argument("--report", default=os.path.join(im.PRIVATE, "bexio-bank-differences.txt"),
                        help="private file for the unmapped records, by bexio id")
    parser.add_argument("--unmatched", default=os.path.join(im.PRIVATE, "bexio-bank-unmatched.txt"),
                        help="private file for the booked transactions that stay unreconciled, by bexio id and reason")
    parser.add_argument("--differences", default=os.path.join(im.PRIVATE, "bexio-bank-live-differences.txt"),
                        help="private file for the --check differences, by bexio id")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if not (args.dry_run or args.write or args.balances or args.check):
        parser.error("give --dry-run, --write, --balances or --check: the write itself is the loader's (bexio-drafts.sh)")

    export_dir = args.export or im.newest_export()
    file = os.path.join(export_dir, TRANSACTIONS + ".json")
    if not os.path.exists(file):
        print("bank transactions: not exported yet (no {}.json in the export)".format(TRANSACTIONS))
        return 0
    with open(file, encoding="utf-8") as f:
        records = json.load(f)
    data = load_export(export_dir)
    erp = im.Erp.from_file(args.token_file)
    try:
        lookups = lookups_from_erp(erp, data)
        meta = doctype_fields(erp, "Bank Transaction")
        gl_accounts = sorted({a["account"] for a in lookups["bank_account"].values()})
        vouchers = read_vouchers(erp, gl_accounts)
        allocated = read_allocated(erp)
        balances = balance_lines(erp, lookups, export_dir) if args.balances else None
        live_rows = erp.list("Bank Transaction", [["bexio_id", "is", "set"]],
                             ["name", "bexio_id", "bank_account", "date", "deposit", "withdrawal", "status", "docstatus"]) if args.check else None
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2

    results = plan(records, lookups, meta)
    matches = match(match_transactions(results, lookups), vouchers, allocated)
    reconcile = reconcile_lines(matches)
    done = set(allocated.values())
    kept = kept_allocations(matches, allocated)
    print("bank transactions from {}".format(export_dir))
    print(summary(results))
    print("")
    print("vouchers with a bexio_id on the bank accounts: {}".format(len(vouchers)))
    print("vouchers already allocated to a Bank Transaction in ERPNext: {}".format(len(allocated)))
    print(match_summary(results, matches, done))
    print("already reconciled: {}, whose match names other vouchers: {}".format(len(done), len(kept)))
    if balances is not None:
        print("")
        print(balances)
    if live_rows is not None:
        table, differences = live_check(live_rows, results, matches, lookups)
        print("")
        print("\n".join(table))
        write_private(args.differences, "\n".join(differences))
        print("{} differences in {}".format(len(differences), args.differences))
    if args.write:
        print("wrote {} documents and {} reconciliations to the private file for the loader; nothing was written to ERPNext".format(
            write_documents(results, args.write, reconcile), len(reconcile)))
    else:
        print("dry run: nothing was written to ERPNext")
    lines = detail_lines(results)
    print("{} unmapped records in {}".format(len(lines), args.report))
    write_private(args.report, "\n".join(lines))
    unmatched = unmatched_lines(results, matches) + [
        "Bank Transaction {}: already reconciled, but the match names other vouchers".format(bexio_id)
        for bexio_id in kept if matches.get(bexio_id, (None,))[0]]
    print("{} transactions left unreconciled in {}".format(len(unmatched), args.unmatched))
    write_private(args.unmatched, "\n".join(unmatched))
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
