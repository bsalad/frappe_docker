"""Imports a UBS camt.053 or camt.054 file as Bank Transactions: the ERPNext side of camt.py.

dry_run shows what an import would do and writes nothing; import_statement does the same and
then creates the Bank Transactions (submitted, Unreconciled) and stamps the transaction_id on
the bexio lines they match. Both refuse a file whose IBAN is not the Bank Account's.
The balance check is reported, never blocking.
"""

from decimal import Decimal

import frappe
from frappe.utils import today

from bi_finance import camt


@frappe.whitelist()
def dry_run(file_url, bank_account=None):
    frappe.has_permission("Bank Transaction", "read", throw=True)
    return _run(file_url, bank_account, write=False)


@frappe.whitelist(methods=["POST"])
def import_statement(file_url, bank_account=None, upload=None):
    """upload: the Bank Statement Upload record the import is made from; named in the comment of each stamp."""
    frappe.has_permission("Bank Transaction", "create", throw=True)
    return _run(file_url, bank_account, write=True, upload=upload)


def _run(file_url, bank_account, write, upload=None):
    try:
        statement = camt.parse(_file_content(file_url))
    except camt.CamtError as err:
        frappe.throw(str(err))

    account = _account(statement, bank_account)
    decisions = camt.plan(statement, _existing_ids(account, statement), _bexio_lines(account))
    ledger = {code: _ledger(account["account"], balance["date"], code == "CLBD")
              for code, balance in statement["balances"].items()}
    checks = camt.check_balances(statement["balances"], ledger)

    imported = _write(account, decisions, upload) if write else 0
    return {
        "bank_account": account["name"], "iban": statement["iban"], "version": statement["version"],
        "currency": account["currency"], "imported": imported,
        "counts": camt.summary(decisions, statement["skipped"]),
        "balances": {code: {"date": check["date"], "file": float(check["file"]), "ledger": float(check["ledger"]), "diff": float(check["diff"])}
                     for code, check in checks.items()},
    }


def _file_content(file_url):
    name = frappe.db.get_value("File", {"file_url": file_url}, "name")
    if not name:
        frappe.throw("No File with the URL {}".format(file_url))
    content = frappe.get_doc("File", name).get_content()
    return content.encode("utf-8") if isinstance(content, str) else content


def _account(statement, bank_account):
    """The Bank Account to import into: the one named, else the one whose IBAN is the file's. Refuses a different IBAN."""
    rows = _accounts_with_iban()
    if bank_account:
        rows = [row for row in rows if row["name"] == bank_account]
        if not rows:
            frappe.throw("Bank Account {} does not exist or has no IBAN".format(bank_account))
    rows = [row for row in rows if _normal_iban(row["iban"]) == statement["iban"]]
    if not rows:
        frappe.throw("The file's IBAN {} is not the IBAN of any Bank Account{}".format(
            statement["iban"], " named {}".format(bank_account) if bank_account else ""))
    if len(rows) > 1:
        frappe.throw("The file's IBAN {} is the IBAN of {} Bank Accounts".format(statement["iban"], len(rows)))
    account = rows[0]
    account["currency"] = frappe.db.get_value("Account", account["account"], "account_currency")
    if statement["currency"] != account["currency"]:
        frappe.throw("The file is in {}, Bank Account {} in {}".format(statement["currency"], account["name"], account["currency"]))
    return account


def _accounts_with_iban():
    return frappe.get_all("Bank Account", filters={"iban": ["is", "set"]}, fields=["name", "iban", "account", "company"])


@frappe.whitelist()
def account_for_file(file_url):
    """The name of the one Bank Account whose IBAN is the file's, else None: the upload form's default."""
    frappe.has_permission("Bank Transaction", "read", throw=True)
    try:
        iban = camt.parse(_file_content(file_url))["iban"]
    except camt.CamtError:
        return None
    names = [row["name"] for row in _accounts_with_iban() if _normal_iban(row["iban"]) == iban]
    return names[0] if len(names) == 1 else None


def _normal_iban(value):
    return "".join(value.split()).upper()


def _existing_ids(account, statement):
    ids = [tx["reference"] for tx in statement["transactions"]]
    if not ids:
        return set()
    return set(frappe.get_all("Bank Transaction", pluck="transaction_id", filters={
        "bank_account": account["name"], "transaction_id": ["in", ids], "docstatus": ["<", 2]}))


def _bexio_lines(account):
    """The bexio lines of the account that have no transaction_id yet: rule (b) matches against them."""
    rows = frappe.get_all("Bank Transaction", fields=["name", "date", "deposit", "withdrawal"], filters={
        "bank_account": account["name"], "bexio_id": ["is", "set"], "transaction_id": ["is", "not set"], "docstatus": ["<", 2]})
    return [{"name": row["name"], "date": str(row["date"]), "deposit": Decimal(str(row["deposit"])),
             "withdrawal": Decimal(str(row["withdrawal"]))} for row in rows]


def _ledger(account, date, inclusive):
    """The balance of the bank's GL account: up to and including the date (CLBD), or before it (OPBD)."""
    operator = "<=" if inclusive else "<"
    total = frappe.db.sql(
        "select coalesce(sum(debit - credit), 0) from `tabGL Entry` where account = %s and is_cancelled = 0 and posting_date {} %s".format(operator),
        (account, date))[0][0]
    return Decimal(str(total))


def _write(account, decisions, upload=None):
    written = 0
    for tx in decisions:
        if tx["rule"] == "bexio":
            frappe.get_doc("Bank Transaction", tx["bexio"]).db_set("transaction_id", tx["reference"], update_modified=False)
            # db_set leaves no Version entry; the comment is what shows the stamp on the document.
            frappe.get_doc({
                "doctype": "Comment", "comment_type": "Info",
                "reference_doctype": "Bank Transaction", "reference_name": tx["bexio"],
                "content": "transaction_id {} stamped by {} on {}".format(
                    tx["reference"], "Bank Statement Upload " + upload if upload else "a camt import", today()),
            }).insert(ignore_permissions=True)
        elif tx["rule"] == "new":
            doc = frappe.get_doc({
                "doctype": "Bank Transaction", "status": "Unreconciled", "company": account["company"],
                "bank_account": account["name"], "currency": account["currency"], "date": tx["booking_date"],
                "booking_date": tx["booking_date"],
                "deposit": float(tx["deposit"]), "withdrawal": float(tx["withdrawal"]),
                "transaction_id": tx["reference"], "reference_number": tx["reference_number"] or "",
                "description": tx["description"] or "", "bank_party_name": tx["bank_party_name"] or "",
                "bank_party_iban": tx["bank_party_iban"] or "",
            })
            doc.insert()
            doc.submit()
            written += 1
    return written
