"""Map the exported bexio bank transactions to ERPNext Bank Transaction, offline.

Step four of the bexio pipeline, after import_master.py has made the Bank Accounts
(keyed by bexio_id). Each function takes one export record and the lookups and
returns the ERPNext Bank Transaction as a dict, not yet inserted: the live write and
its order are erp-a2ma's, after the posting plan (finance-3qsp). So nothing is
written here: --dry-run reads ERPNext and prints totals only.

The export has no bank_transactions.json yet: the banking endpoint answers 403 (the
manifest records it), and finance-3qsp's export login adds the scope. Until the file
exists the dry run says "not exported yet". The field names below are ASSUMED: the bexio
docs could not be read for the banking transactions, so they are kept in one place and
the first thing to check when the file exists.

A record that cannot be mapped (an unknown bank account, a currency other than its
account's, a zero amount) raises Unmapped, and the report names it by bexio id only.
Amounts stay in the account's currency: no conversion, so no CHF totals here.

Run it as:

    python3 finance/bexio/import_bank.py --dry-run [--export DIR]

--export defaults to the newest directory under <private>/bexio-export/. The output
is totals only; the unmapped records go to a file under <private>, never to the
repository. Standard library only, plus import_master.
"""

import argparse
import collections
import json
import os
import sys
from decimal import Decimal

import import_master as im

COMPANY = im.COMPANY
TRANSACTIONS = "bank_transactions"

# ASSUMED field names of a bexio banking transaction. amount is signed: negative when
# money leaves the account (ASSUMED, to be confirmed against the export).
BOOKED_WITH = "booked_with"          # ASSUMED: what the transaction is booked against, if booked
ERP_BOOKED_WITH = "bexio_booked_with"  # the ERPNext field it would land in; the posting plan decides

ZERO = Decimal("0")


class Unmapped(Exception):
    """A record the importer does not map. The message names the reason, never a company name."""


def _dec(value):
    return Decimal(str(value))


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

    # ERPNext keeps the direction in two columns: money in is a deposit, money out a withdrawal
    doc = {
        "doctype": "Bank Transaction", "company": COMPANY, "bexio_id": str(record["id"]),
        "date": record["value_date"], "bank_account": account["name"], "currency": currency,
        "deposit": float(amount) if amount > 0 else 0.0,
        "withdrawal": float(-amount) if amount < 0 else 0.0,
        "description": record.get("text") or "",
        "reference_number": record.get("reference") or "",
    }
    # the link to what the transaction is booked against, kept for reconciliation later
    if record.get(BOOKED_WITH):
        doc[ERP_BOOKED_WITH] = str(record[BOOKED_WITH])
    return doc


def plan(records, lookups, meta=None):
    """Map every record of the export. Nothing is written; returns one result per record."""
    results = []
    for record in records:
        result = {"bexio_id": str(record.get("id")), "account": str(record.get("bank_account_id")),
                  "year": str(record.get("value_date") or "????")[:4], "doc": None, "error": None,
                  "unknown": [], "currency": None, "in": ZERO, "out": ZERO}
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
    """The Bank Accounts by bexio_id, read from ERPNext, with the currency the export gives each one."""
    names = {r["bexio_id"]: r["name"] for r in erp.list("Bank Account", [["bexio_id", "is", "set"]], ["name", "bexio_id"])}
    currency = {str(c["id"]): c["name"] for c in data["currencies"]}
    accounts = {}
    for b in data["bank_accounts"]:
        name = names.get(str(b["id"]))
        if name is not None:
            accounts[str(b["id"])] = {"name": name, "currency": currency.get(str(b["currency_id"]))}
    return {"currency": currency, "bank_account": accounts}


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
    missing = collections.Counter(field for r in results for field in r["unknown"])
    for field, count in sorted(missing.items()):
        lines.append("ERPNext has no field {} ({} records): the posting plan decides it".format(field, count))
    return "\n".join(lines)


def detail_lines(results):
    """The unmapped records, by bexio id, for the private report file."""
    return ["Bank Transaction {}: unmapped: {}".format(r["bexio_id"], r["error"]) for r in results if r["error"]]


def main(argv):
    parser = argparse.ArgumentParser(description="Map the exported bexio bank transactions to ERPNext (dry run only for now).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals")
    parser.add_argument("--report", default=os.path.join(im.PRIVATE, "bexio-bank-differences.txt"),
                        help="private file for the unmapped records, by bexio id")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if not args.dry_run:
        parser.error("dry run only for now: the live run is erp-a2ma's, after the posting plan (finance-3qsp)")

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
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2

    results = plan(records, lookups, meta)
    print("bank transactions from {}".format(export_dir))
    print(summary(results))
    lines = detail_lines(results)
    print("dry run: nothing was written; {} unmapped records in {}".format(len(lines), args.report))
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
