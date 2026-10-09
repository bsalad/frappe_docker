"""Swiss setup of company BI Concepts, run inside the backend container by
swiss-setup.sh. Steps are named on the command line (coa, vat, fiscal,
fields); each one is re-runnable and skips what already exists."""
import csv
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
]


def say(msg):
    print(msg, flush=True)


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


STEPS = {"coa": coa, "vat": vat, "fiscal": fiscal, "fields": fields}

if __name__ == "__main__":
    steps = sys.argv[1:] or list(STEPS)
    frappe.init(site="frontend")
    frappe.connect()
    frappe.set_user("Administrator")
    try:
        for s in steps:
            STEPS[s]()
    finally:
        frappe.destroy()
