"""Map the bexio outgoing payments to ERPNext Payment Entries (pay), salary to Journal Entries.

Step four of the bexio pipeline, after import_purchase.py has put the bills into
ERPNext. This module maps one payment order at a time: map_payment() returns the
ERPNext document dict. A supplier payment becomes a Payment Entry against the
Purchase Invoice it settles; a salary payment becomes a Journal Entry, since it is
no supplier payment and no bill stands behind it. The live run belongs to
erp-a2ma; the command line here only runs the dry run, which reads ERPNext and
writes nothing.

Run it as:

    python3 finance/bexio/import_payments_out.py --dry-run [--export DIR]

--export defaults to the newest directory under <private>/bexio-export/. The
export holds payments.json, the bexio payment orders. A supplier payment is linked
to its bill by purchase_reference.bill_id: the bexio id of the bill, which is the
bexio_id of its Purchase Invoice. Amount, currency and document number of the
paid, linked payments agree with their bill. Only executed payments are mapped,
those with status "paid"; the other statuses are skipped and named in the report.
The dry run prints totals per year, status and currency, and the records that are
skipped or unmapped by reason. The bexio ids of those go to
<private>/bexio-payments-out-dry-run.txt, never to the screen or the repository.

Standard library only, apart from import_master.py and import_purchase.py.
"""

import argparse
import datetime
import json
import os
import sys
from decimal import Decimal, ROUND_HALF_UP

import import_master as im
import import_purchase as ip

EXECUTED = "paid"                 # bexio's status of an order the bank has executed
SALARY_ACCOUNT = "91"             # bexio account id of Lohndurchlaufkonto, the account salary is paid out of
CURRENCY = "CHF"                  # the company currency; the bank accounts and the bills are in it
CENT = Decimal("0.01")
ZERO = Decimal("0")
PAYMENTS_FILE = "payments.json"
PROBLEMS_FILE = "bexio-payments-out-dry-run.txt"


class Lookups:
    """What the mapping reads from ERPNext: the bank GL accounts, the Accounts and the Purchase Invoices by bexio_id."""

    def __init__(self, banks, accounts, invoices):
        self.banks = banks        # bexio bank account id -> GL account of the Bank Account
        self.accounts = accounts  # bexio account id -> Account name
        self.invoices = invoices  # bexio bill id -> the Purchase Invoice row (name, supplier, credit_to, currency, grand_total)

    @classmethod
    def from_erp(cls, erp):
        banks = erp.list("Bank Account", [["company", "=", im.COMPANY], ["bexio_id", "is", "set"]], ["name", "bexio_id", "account"])
        accounts = erp.list("Account", [["company", "=", im.COMPANY], ["bexio_id", "is", "set"]], ["name", "bexio_id"])
        invoices = erp.list("Purchase Invoice", [["company", "=", im.COMPANY], ["bexio_id", "is", "set"]],
                            ["name", "bexio_id", "supplier", "credit_to", "currency", "grand_total"])
        return cls(
            banks={r["bexio_id"]: r["account"] for r in banks},
            accounts={r["bexio_id"]: r["name"] for r in accounts},
            invoices={r["bexio_id"]: r for r in invoices},
        )


def _money(value):
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _bill_of(payment):
    return (payment.get("purchase_reference") or {}).get("bill_id")


def map_payment(payment, lookups):
    """The ERPNext document dict of one bexio payment order; raises Skipped or MappingError when it has no home as it is."""
    if payment["status"] != EXECUTED:
        raise ip.Skipped("not executed: status {}".format(payment["status"]))
    if payment["currency"] != CURRENCY:
        raise ip.MappingError("currency {}: the export has no exchange rate for it".format(payment["currency"]))
    bank = lookups.banks.get(str(payment["sender"]["id"]))
    if not bank:
        raise ip.MappingError("no Bank Account with bexio_id {} in ERPNext".format(payment["sender"]["id"]))
    amount = _money(payment["amount"])
    if payment["is_salary"]:
        return salary_entry(payment, amount, bank, lookups)
    return supplier_payment(payment, amount, bank, lookups)


def supplier_payment(payment, amount, bank, lookups):
    """A Payment Entry (pay) against the Purchase Invoice of the bill the payment settles."""
    bill_id = _bill_of(payment)
    if not bill_id:
        raise ip.MappingError("paid, not salary, and no bill link in the export")
    invoice = lookups.invoices.get(str(bill_id))
    if not invoice:
        raise ip.MappingError("no Purchase Invoice with the bill's bexio_id in ERPNext")
    if invoice["currency"] != CURRENCY:
        raise ip.MappingError("Purchase Invoice in a currency other than {}".format(CURRENCY))
    if _money(invoice["grand_total"]) != amount:
        raise ip.MappingError("amount differs from its Purchase Invoice")
    return {
        "doctype": "Payment Entry", "company": im.COMPANY, "payment_type": "Pay",
        "party_type": "Supplier", "party": invoice["supplier"],
        "posting_date": payment["execution_date"], "reference_date": payment["execution_date"],
        "reference_no": payment.get("document_no") or None,
        "paid_from": bank, "paid_from_account_currency": CURRENCY,
        "paid_to": invoice["credit_to"], "paid_to_account_currency": CURRENCY,
        "paid_amount": float(amount), "received_amount": float(amount),
        "references": [{
            "reference_doctype": "Purchase Invoice", "reference_name": invoice["name"],
            "allocated_amount": float(amount),
        }],
        "bexio_id": str(payment["id"]),
    }


def salary_entry(payment, amount, bank, lookups):
    """A Journal Entry for a salary payment: Lohndurchlaufkonto debited, the bank credited. No bill stands behind it."""
    if _bill_of(payment):
        raise ip.MappingError("salary payment with a bill link: not a salary payment as the export has it")
    account = lookups.accounts.get(SALARY_ACCOUNT)
    if not account:
        raise ip.MappingError("no Account with bexio_id {} (Lohndurchlaufkonto) in ERPNext".format(SALARY_ACCOUNT))
    return {
        "doctype": "Journal Entry", "company": im.COMPANY, "voucher_type": "Bank Entry",
        "posting_date": payment["execution_date"], "user_remark": "bexio Lohnzahlung",
        "accounts": [
            {"account": account, "debit_in_account_currency": float(amount), "credit_in_account_currency": 0},
            {"account": bank, "debit_in_account_currency": 0, "credit_in_account_currency": float(amount)},
        ],
        "bexio_id": str(payment["id"]),
    }


class Totals:
    """Per year, status and currency: the counts and the amounts; per reason: what is skipped or unmapped and why."""

    def __init__(self):
        self.rows = {}
        self.reasons = {}     # (reason, currency) -> {"records", "amount"}
        self.problems = []

    def row(self, year, status, currency):
        return self.rows.setdefault((year, status, currency), {
            "records": 0, "mapped": 0, "skipped": 0, "unmapped": 0,
            "amount": ZERO, "mapped_amount": ZERO,
        })

    def reason(self, err, currency, amount):
        entry = self.reasons.setdefault((str(err), currency), {"records": 0, "amount": ZERO})
        entry["records"] += 1
        entry["amount"] += amount


def dry_run(payments, lookups):
    """Map every payment and count the results; writes nothing. Returns the Totals."""
    totals = Totals()
    for pay in payments:
        day = datetime.date.fromisoformat(pay["execution_date"])
        row = totals.row(day.year, pay["status"], pay["currency"])
        amount = _money(pay["amount"])
        row["records"] += 1
        row["amount"] += amount
        # the bill's id is named in the problems file only: the reasons on screen stay generic
        ref = "payment {}".format(pay["id"]) + (" (bill {})".format(_bill_of(pay)) if _bill_of(pay) else "")
        try:
            map_payment(pay, lookups)
        except ip.Skipped as err:
            row["skipped"] += 1
            totals.reason(err, pay["currency"], amount)
            totals.problems.append("{}: skipped, {}".format(ref, err))
        except ip.MappingError as err:
            row["unmapped"] += 1
            totals.reason(err, pay["currency"], amount)
            totals.problems.append("{}: unmapped, {}".format(ref, err))
        else:
            row["mapped"] += 1
            row["mapped_amount"] += amount
    return totals


def report(totals, export_dir):
    lines = ["payment dry run from {}".format(export_dir),
             "{:<6}{:<13}{:<5}{:>8}{:>8}{:>9}{:>9}{:>15}{:>15}".format(
                 "year", "status", "cur", "records", "mapped", "skipped", "unmapped", "amount", "mapped amount")]
    for (year, status, currency), r in sorted(totals.rows.items()):
        lines.append("{:<6}{:<13}{:<5}{:>8}{:>8}{:>9}{:>9}{:>15}{:>15}".format(
            year, status, currency, r["records"], r["mapped"], r["skipped"], r["unmapped"],
            r["amount"], r["mapped_amount"]))
    lines.append("")
    lines.append("skipped and unmapped, by reason")
    for (reason, currency), r in sorted(totals.reasons.items()):
        lines.append("{:>6}  {:<5}{:>15}  {}".format(r["records"], currency, r["amount"], reason))
    lines.append("dry run: nothing was written")
    return "\n".join(lines)


def load_payments(export_dir):
    with open(os.path.join(export_dir, PAYMENTS_FILE), encoding="utf-8") as f:
        return json.load(f)


def main(argv):
    parser = argparse.ArgumentParser(description="Map the bexio outgoing payments to ERPNext Payment Entries (dry run).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if not args.dry_run:
        print("the live run is erp-a2ma's; this command takes --dry-run only", file=sys.stderr)
        return 2
    export_dir = args.export or im.newest_export()
    payments = load_payments(export_dir)
    try:
        lookups = Lookups.from_erp(im.Erp.from_file(args.token_file))
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    totals = dry_run(payments, lookups)
    print(report(totals, export_dir))
    if totals.problems:
        ip.write_private(os.path.join(im.PRIVATE, PROBLEMS_FILE), totals.problems)
        print("{} record(s) listed by bexio id in {}".format(len(totals.problems), os.path.join(im.PRIVATE, PROBLEMS_FILE)), file=sys.stderr)
    unmapped = sum(r["unmapped"] for r in totals.rows.values())
    return 1 if unmapped else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
