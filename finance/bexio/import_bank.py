"""Map the exported bexio bank transactions to ERPNext Bank Transaction, offline.

Step four of the bexio pipeline, after import_master.py has made the Bank Accounts
(keyed by bexio_id). Each function takes one export record and the lookups and
returns the ERPNext Bank Transaction as a dict, not yet inserted: the live write and
its order are erp-7avs's, after the posting plan's open decisions (finance-3qsp, D7).
So nothing is written to ERPNext here: --dry-run reads ERPNext and prints totals only,
--write also keeps the documents in a private file for the loader.

The export's bank_transactions.json has amount unsigned (always positive) and type
CREDIT (money in) or DEBIT (money out). status says whether bexio has booked the
transaction: reconciled and auto_reconciled are booked (against a payment or a banking
entry, which the export does not say), unreconciled and ignored are not. The export
has no field that names the booking, so the link is not mapped here (D7).

A record that cannot be mapped (an unknown bank account, a currency other than its
account's, a zero amount) raises Unmapped, and the report names it by bexio id only.
Amounts stay in the transaction's currency: no conversion, so no CHF totals here.

Run it as:

    python3 finance/bexio/import_bank.py --dry-run [--export DIR]
    python3 finance/bexio/import_bank.py --write <private>/bexio-bank-docs.json [--export DIR]

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

# bexio's status values: these two mean bexio has booked the transaction
BOOKED = ("reconciled", "auto_reconciled")

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
    if record.get("type") not in ("CREDIT", "DEBIT"):
        raise Unmapped("type {} is neither CREDIT nor DEBIT".format(record.get("type")))

    # the export's amount is unsigned; its type gives the direction, which ERPNext keeps in two columns
    deposit = amount if record["type"] == "CREDIT" else ZERO
    withdrawal = amount if record["type"] == "DEBIT" else ZERO
    return {
        "doctype": "Bank Transaction", "company": COMPANY, "bexio_id": str(record["id"]),
        "date": record["value_date"], "bank_account": account["name"], "currency": currency,
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
                  "booked": record.get("status") in BOOKED}
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


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text + "\n")


def write_documents(results, path):
    """The mapped Bank Transactions as the loader takes them, into the private file; returns their number.

    Each is keyed by bexio_id; the loader inserts it and submits it, and reconciles it only once the posting
    plan decides the booking link (D7), so nothing here says what a transaction is booked against.
    """
    documents = [{"doctype": r["doc"]["doctype"], "bexio_id": r["bexio_id"], "booked": r["booked"],
                  "values": {k: v for k, v in r["doc"].items() if k != "doctype"}}
                 for r in results if r["doc"]]
    write_private(path, json.dumps({"documents": documents}, indent=1))
    return len(documents)


def main(argv):
    parser = argparse.ArgumentParser(description="Map the exported bexio bank transactions to ERPNext (nothing is written to ERPNext).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals")
    parser.add_argument("--write", metavar="FILE", help="also write the mapped documents to a private file for the loader")
    parser.add_argument("--report", default=os.path.join(im.PRIVATE, "bexio-bank-differences.txt"),
                        help="private file for the unmapped records, by bexio id")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if not (args.dry_run or args.write):
        parser.error("give --dry-run or --write: the live run waits for the posting plan's open decisions (D7)")

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
    if args.write:
        print("wrote {} documents to the private file for the loader; nothing was written to ERPNext".format(
            write_documents(results, args.write)))
    else:
        print("dry run: nothing was written to ERPNext")
    lines = detail_lines(results)
    print("{} unmapped records in {}".format(len(lines), args.report))
    write_private(args.report, "\n".join(lines))
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
