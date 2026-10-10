"""The bank feed shared by the bank APIs (Wise now, PayPal later): feed rows written as submitted Bank Transactions.

A row is the dict wise_client.statement_rows gives: transaction_id, date, deposit, withdrawal, currency, description,
reference_number. Its transaction_id is the external id. A Bank Transaction with that id is left alone on every later
run, so a second run writes nothing.

A period already imported by bexio has no transaction_id, so a row is matched to it by account, date and amount instead.
Each such Bank Transaction takes one feed row, so the feed does not write a movement twice. The count matters: two
movements of the same amount on the same day are both kept. The reference is not compared, because bexio's lines have
none.

The matching is in new_rows, which reads nothing, so it is tested without a database. plan and write are the frappe side.
"""

import collections

import frappe


def new_rows(rows, fed_ids, imported):
    """The rows that are not yet in ERPNext: (create, summary). Pure: fed_ids and imported are read by the caller.

    fed_ids: the transaction_ids already on the account. imported: a Counter of (date, deposit, withdrawal) of the
    account's Bank Transactions that no feed wrote (bexio's). It is consumed here, so the caller's copy is not reused.
    """
    create = []
    summary = {"rows": len(rows), "already_fed": 0, "already_imported": 0}
    for row in rows:
        if row["transaction_id"] in fed_ids:
            summary["already_fed"] += 1
            continue
        key = _key(row)
        if imported[key] > 0:
            imported[key] -= 1
            summary["already_imported"] += 1
            continue
        create.append(row)
    summary["create"] = len(create)
    return create, summary


def plan(bank_account, rows):
    """What a run would write to bank_account for these rows: (create, summary). Reads ERPNext, writes nothing."""
    existing = frappe.get_all(
        "Bank Transaction",
        filters={"bank_account": bank_account, "docstatus": ["<", 2]},
        fields=["transaction_id", "date", "deposit", "withdrawal"],
    )
    fed_ids = {r["transaction_id"] for r in existing if r["transaction_id"]}
    imported = collections.Counter(_key(r) for r in existing if not r["transaction_id"])
    return new_rows(rows, fed_ids, imported)


def write(bank_account, rows):
    """Insert and submit the rows as Bank Transactions, as the bexio loader does (a submitted one posts no GL). Returns the count."""
    company = frappe.db.get_value("Bank Account", bank_account, "company")
    written = 0
    for row in rows:
        # checked again per row: the hourly job and a Sync now can run at once
        if frappe.db.exists("Bank Transaction", {"transaction_id": row["transaction_id"]}):
            continue
        doc = frappe.get_doc(
            {
                "doctype": "Bank Transaction",
                "company": company,
                "bank_account": bank_account,
                "date": row["date"],
                "deposit": row["deposit"],
                "withdrawal": row["withdrawal"],
                "currency": row["currency"],
                "description": row["description"],
                "reference_number": row["reference_number"],
                "transaction_id": row["transaction_id"],
            }
        )
        doc.insert()
        doc.submit()
        written += 1
    return written


def _key(row):
    return (str(row["date"]), round(float(row["deposit"] or 0), 2), round(float(row["withdrawal"] or 0), 2))
