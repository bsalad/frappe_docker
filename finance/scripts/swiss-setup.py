"""Swiss setup of company BI Concepts, run inside the backend container by
swiss-setup.sh. Steps are named on the command line (coa, vat, fiscal,
fields, currencies, banks, gebuev); each one is re-runnable and skips what
already exists. `freeze <date> [--apply]` is a separate command: it closes the
books up to a date and is a dry run unless --apply is given."""
import csv
import datetime
import os
import sys

import frappe
from frappe.utils import get_bench_path

COMPANY = "BI Concepts"

# erpnextswiss ships the KMU chart as a CSV. Its own importer (coa_importer)
# needs pandas, which the image does not have, so the CSV is read here.
COA_CSV = "apps/erpnextswiss/erpnextswiss/erpnextswiss/coa_import/accounts_template.csv"

# (rate, kind, period label). Rates change on 2018-01-01 and 2024-01-01; a
# template per rate keeps the period in its name because ERPNext tax
# templates have no validity dates. 3.8 % applied both before 2018 and from
# 2024, so one template serves both.
VAT_RATES = [
    (8.1, "Normal", "ab 2024"),
    (2.6, "Reduziert", "ab 2024"),
    (3.8, "Beherbergung", "bis 2017, ab 2024"),
    (7.7, "Normal", "2018-2023"),
    (2.5, "Reduziert", "bis 2023"),
    (3.7, "Beherbergung", "2018-2023"),
    (8.0, "Normal", "bis 2017"),
]
# KMU accounts, by number: USt payable, Vorsteuer on material/services and
# on investments/operating expense.
ACC_USt, ACC_VSt_MAT, ACC_VSt_INV = "2200", "1170", "1171"
DEFAULT_RATE = 8.1

FISCAL_YEARS = range(2015, 2027)

BEXIO_DOCTYPES = [
    # (doctype, field to put bexio_id after)
    ("Customer", "customer_name"),
    ("Supplier", "supplier_name"),
    ("Contact", "last_name"),
    ("Address", "address_title"),
    ("Item", "item_name"),
    ("Account", "account_number"),
    ("Sales Invoice", "title"),
    ("Purchase Invoice", "title"),
    ("Journal Entry", "title"),
    ("Payment Entry", "payment_type"),
    # master data of the bexio import (finance-sg76)
    ("Customer Group", "customer_group_name"),
    ("Supplier Group", "supplier_group_name"),
    ("Item Price", "item_code"),
    ("Bank Account", "account_name"),
    # keys for the document import (finance-3qsp)
    ("Sales Order", "title"),
    ("Quotation", "title"),
    ("Bank Transaction", "date"),
]

# Currencies bexio books in, enabled by the `currencies` step. CHF is paid in
# 0.05 steps (Swiss cash rounding); the others keep the cent.
CURRENCIES = {"CHF": 0.05, "EUR": 0.01, "USD": 0.01, "GBP": 0.01,
              "BRL": 0.01, "JPY": 0.01, "CNY": 0.01, "PLN": 0.01}


# GeBüV (OR 958f, GeBüV art. 9-10): a posted record may not change or vanish
# without a trace. ERPNext v16 already refuses to delete a submitted document
# and versions changes to tracked doctypes; the gebuev step sets what is left.
GEBUEV_TRACKED = ["Account", "Customer", "Supplier", "Item", "Bank Account", "Company",
                  "Sales Invoice", "Purchase Invoice", "Journal Entry", "Payment Entry",
                  "Bank Transaction"]
# The one role that may delete on these doctypes; Administrator is not listed
# because it bypasses the role check.
GEBUEV_DELETE_ROLE = "Accounts Manager"


def say(msg):
    print(msg, flush=True)


def delete_revokes(perms, keep):
    """perms: (role, permlevel, delete) rows, as the effective permissions of a
    doctype. Returns the (role, permlevel) pairs that may delete but are not in keep."""
    return [(role, level) for role, level, delete in perms if delete and role not in keep]


def parse_freeze(args):
    """args after `freeze`: a date and optionally --apply. Returns (date, apply)."""
    apply = "--apply" in args
    dates = [a for a in args if a != "--apply"]
    if len(dates) != 1 or len(args) != len(dates) + apply:
        sys.exit("usage: swiss-setup.sh freeze <YYYY-MM-DD> [--apply]")
    try:
        return datetime.date.fromisoformat(dates[0]).isoformat(), apply
    except ValueError:
        sys.exit(f"freeze: not a date: {dates[0]}")


def account(number):
    name = frappe.db.get_value(
        "Account", {"company": COMPANY, "account_number": number}, "name"
    )
    if not name:
        frappe.throw(f"Account {number} not found in {COMPANY}; run the coa step first")
    return name


def coa():
    # Replacing the chart is only safe while nothing has been booked.
    gl = frappe.db.count("GL Entry", {"company": COMPANY})
    if gl:
        say(f"STOP: {gl} GL entries for {COMPANY}; the chart can no longer be replaced")
        sys.exit(3)

    abbr = frappe.db.get_value("Company", COMPANY, "abbr")
    if frappe.db.exists("Account", f"1000 - Kasse - {abbr}"):
        say("coa: KMU chart already in place")
        return

    # The stock 'Switzerland ... VAT' templates point at the old accounts.
    for dt in ("Sales Taxes and Charges Template", "Purchase Taxes and Charges Template", "Item Tax Template"):
        for name in frappe.get_all(dt, {"company": COMPANY, "title": ["like", "Switzerland %"]}, pluck="name"):
            frappe.delete_doc(dt, name)
            say(f"coa: deleted {dt} {name}")

    # Links from the company and the payment modes to the old accounts.
    for f in frappe.get_meta("Company").get_link_fields():
        if f.options == "Account":
            frappe.db.set_value("Company", COMPANY, f.fieldname, None)
    frappe.db.delete("Mode of Payment Account", {"company": COMPANY})
    frappe.db.commit()

    frappe.db.delete("Account", {"company": COMPANY})
    with open(f"{get_bench_path()}/{COA_CSV}", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row = {k: (v or "").strip() for k, v in row.items()}
        parent = f"{row['parent_account']} - {abbr}" if row["parent_account"] else ""
        acc = frappe.get_doc({
            "doctype": "Account",
            "account_name": row["account_name"],
            "company": COMPANY,
            "parent_account": parent,
            "account_number": row["account_number"],
            "is_group": int(row["is_group"]),
            "root_type": row["root_type"],
            "report_type": row["report_type"],
            "account_currency": row["account_currency"],
            "account_type": row["account_type"],
            "tax_rate": float(row["tax_rate"] or 0),
            "freeze_account": row["freeze_account"],
        })
        # roots have no parent, which the form marks mandatory
        acc.flags.ignore_mandatory = True
        acc.flags.ignore_root_company_validation = True
        acc.insert()
        default = row["company_default_account"]
        if default and frappe.get_meta("Company").has_field(default):
            frappe.db.set_value("Company", COMPANY, default, acc.name)
        elif default:
            say(f"coa: no Company field {default} (template default for {acc.name})")
    frappe.db.commit()

    # The template leaves 2200 without a type; tax templates need type Tax.
    frappe.db.set_value("Account", account(ACC_USt), "account_type", "Tax")
    for mop, number in (("Cash", "1000"),):
        if frappe.db.exists("Mode of Payment", mop):
            doc = frappe.get_doc("Mode of Payment", mop)
            doc.append("accounts", {"company": COMPANY, "default_account": account(number)})
            doc.save()
    frappe.db.commit()
    say(f"coa: {frappe.db.count('Account', {'company': COMPANY})} accounts")


def vat():
    abbr = frappe.db.get_value("Company", COMPANY, "abbr")
    ust, vst_mat, vst_inv = account(ACC_USt), account(ACC_VSt_MAT), account(ACC_VSt_INV)
    for rate, kind, period in VAT_RATES:
        label = f"{rate}% {kind} ({period})"
        default = 1 if rate == DEFAULT_RATE else 0

        title = f"USt {label}"
        if not frappe.db.exists("Sales Taxes and Charges Template", f"{title} - {abbr}"):
            frappe.get_doc({
                "doctype": "Sales Taxes and Charges Template",
                "title": title,
                "company": COMPANY,
                "is_default": default,
                "taxes": [{
                    "charge_type": "On Net Total", "account_head": ust,
                    "description": f"MWST {rate}%", "rate": rate,
                }],
            }).insert()

        for short, acc in (("Material/DL", vst_mat), ("Invest./Aufwand", vst_inv)):
            title = f"VSt {label} {short}"
            if frappe.db.exists("Purchase Taxes and Charges Template", f"{title} - {abbr}"):
                continue
            frappe.get_doc({
                "doctype": "Purchase Taxes and Charges Template",
                "title": title,
                "company": COMPANY,
                "is_default": default if acc == vst_mat else 0,
                "taxes": [{
                    "category": "Total", "add_deduct_tax": "Add",
                    "charge_type": "On Net Total", "account_head": acc,
                    "description": f"Vorsteuer {rate}%", "rate": rate,
                }],
            }).insert()

        # One item template serves sales and purchase: ERPNext applies the
        # rate of the row whose account matches the tax row.
        title = f"MWST {label}"
        if not frappe.db.exists("Item Tax Template", f"{title} - {abbr}"):
            frappe.get_doc({
                "doctype": "Item Tax Template",
                "title": title,
                "company": COMPANY,
                "taxes": [{"tax_type": a, "tax_rate": rate} for a in (ust, vst_mat, vst_inv)],
            }).insert()
    frappe.db.commit()
    for dt in ("Sales Taxes and Charges Template", "Purchase Taxes and Charges Template", "Item Tax Template"):
        say(f"vat: {frappe.db.count(dt, {'company': COMPANY})} {dt}")


def fiscal():
    for year in FISCAL_YEARS:
        if frappe.db.exists("Fiscal Year", str(year)):
            doc = frappe.get_doc("Fiscal Year", str(year))
        else:
            doc = frappe.get_doc({
                "doctype": "Fiscal Year", "year": str(year),
                "year_start_date": f"{year}-01-01", "year_end_date": f"{year}-12-31",
            })
        if COMPANY not in [c.company for c in doc.companies]:
            doc.append("companies", {"company": COMPANY})
            doc.save() if doc.get("creation") else doc.insert()
    frappe.db.commit()
    say(f"fiscal: {frappe.db.count('Fiscal Year')} fiscal years")


def fields():
    for dt, after in BEXIO_DOCTYPES:
        if frappe.db.exists("Custom Field", {"dt": dt, "fieldname": "bexio_id"}):
            continue
        if not frappe.get_meta(dt).has_field(after):
            after = None
        frappe.get_doc({
            "doctype": "Custom Field",
            "dt": dt,
            "fieldname": "bexio_id",
            "label": "bexio ID",
            "fieldtype": "Data",
            "unique": 1,
            "read_only": 1,
            "in_standard_filter": 1,
            # a copied document must not inherit the id of the original
            "no_copy": 1,
            "insert_after": after,
        }).insert()
    frappe.db.commit()
    say(f"fields: bexio_id on {frappe.db.count('Custom Field', {'fieldname': 'bexio_id'})} doctypes")


def currencies():
    # The API user may not write Currency, so this lives here, not in the importer.
    for code, step in CURRENCIES.items():
        doc = frappe.get_doc("Currency", code)
        want = {"enabled": 1}
        if code == "CHF":
            want["smallest_currency_fraction_value"] = step
        if all(doc.get(k) == v for k, v in want.items()):
            continue
        doc.update(want)
        doc.save()
    frappe.db.commit()
    say(f"currencies: {frappe.db.count('Currency', {'enabled': 1})} enabled")


def banks():
    # Bank needs System Manager, which the API user lacks. The names are company
    # data, so they come from the BANKS environment variable (one per line),
    # never from this file: import_master.py --print-banks lists them.
    for name in filter(None, (n.strip() for n in os.environ.get("BANKS", "").split("\n"))):
        if not frappe.db.exists("Bank", name):
            frappe.get_doc({"doctype": "Bank", "bank_name": name}).insert()
    frappe.db.commit()
    say(f"banks: {frappe.db.count('Bank')} banks")


def gebuev():
    # 1. Accounts Settings. Cancelling with the immutable ledger posts reversal
    # entries and leaves the original GL rows as they were; without it, the
    # original rows get is_cancelled=1 in place.
    s = frappe.get_single("Accounts Settings")
    want = {"enable_immutable_ledger": 1, "delete_linked_ledger_entries": 0}
    changed = [f for f, v in want.items() if s.get(f) != v]
    for f in changed:
        say(f"gebuev: Accounts Settings {f} {s.get(f)} -> {want[f]}")
        s.set(f, want[f])
    if changed:
        s.save()
    else:
        say("gebuev: Accounts Settings already set")

    # 2. Delete right: only GEBUEV_DELETE_ROLE keeps it on these doctypes.
    # frappe refuses to delete a submitted document for everyone, so this
    # covers drafts and cancelled documents.
    for dt in GEBUEV_TRACKED:
        perms = [(p.role, p.permlevel, p.delete) for p in frappe.get_meta(dt).permissions]
        for role, level in delete_revokes(perms, keep=[GEBUEV_DELETE_ROLE]):
            frappe.permissions.update_permission_property(dt, role, level, "delete", 0)
            say(f"gebuev: {dt}: delete revoked from {role} (permlevel {level})")
        frappe.clear_cache(doctype=dt)
    say("gebuev: delete rights checked on " + str(len(GEBUEV_TRACKED)) + " doctypes")

    # 3. Track Changes (Version) on the same doctypes. A Property Setter, so a
    # migrate does not reset it to the doctype's JSON.
    for dt in GEBUEV_TRACKED:
        if frappe.db.get_value("DocType", dt, "track_changes"):
            continue
        frappe.make_property_setter(dt, None, "track_changes", 1, "Check", for_doctype=True)
        say(f"gebuev: {dt}: track changes on")
    frappe.clear_cache()
    say("gebuev: track changes checked on " + str(len(GEBUEV_TRACKED)) + " doctypes")
    frappe.db.commit()


def freeze(date, apply):
    # Accounts frozen till a date: no posting dated on or before it. The role
    # that may still post there is set by hand, not here. Dry run unless
    # --apply. The history import comes first, so this is not run before it.
    company = frappe.get_doc("Company", COMPANY)
    current = company.accounts_frozen_till_date
    if current and str(current) > date:
        sys.exit(f"STOP: accounts are frozen till {current}; a freeze is not moved back to {date}")
    gl = frappe.db.count("GL Entry", {"company": COMPANY, "posting_date": ["<=", date], "is_cancelled": 0})
    say(f"freeze: accounts frozen till {current or '(none)'} -> {date}")
    say(f"freeze: {gl} GL entries dated on or before {date}")
    say(f"freeze: role allowed to post into frozen periods: {company.role_allowed_for_frozen_entries or '(none)'}")
    if not apply:
        say("freeze: dry run, nothing changed; add --apply to set it")
        return
    frappe.db.set_value("Company", COMPANY, "accounts_frozen_till_date", date)
    frappe.db.commit()
    say(f"freeze: set, accounts frozen till {date}")


STEPS = {"coa": coa, "vat": vat, "fiscal": fiscal, "fields": fields, "currencies": currencies, "banks": banks,
         "gebuev": gebuev}

if __name__ == "__main__":
    args = sys.argv[1:]
    frappe.init(site="frontend")
    frappe.connect()
    frappe.set_user("Administrator")
    try:
        if args[:1] == ["freeze"]:
            freeze(*parse_freeze(args[1:]))
        else:
            for s in args or list(STEPS):
                STEPS[s]()
    finally:
        frappe.destroy()
