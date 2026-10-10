"""The frappe side of the Swiss payroll: the rate copies on each slip, and the structure per Swiss company.

The formulas (fixtures/salary_component.json) are evaluated by HRMS with the slip and the assignment as their
names. HRMS's formula context has no way to read a settings doctype, so the rates are copied here, on
validate, and the slip keeps the rates it was computed with.
"""

from decimal import Decimal

import frappe
from frappe.utils import flt, getdate

from bi_payroll.quellensteuer import parse_tariff_file, qst_amount
from bi_payroll.swiss_rates import RATE_FIELDS, STRUCTURE_DEDUCTIONS, STRUCTURE_EARNINGS

QST_DECIMAL_FIELDS = ("income_from", "step", "minimum_tax", "rate")


def copy_rates_to_slip(doc, method=None):
    settings = frappe.get_cached_doc("Payroll Swiss Settings")
    for field in RATE_FIELDS:
        # an empty rate (an insurer not given yet) is 0: its component is then left out of the slip
        doc.set(field, settings.get(field) or 0)


def withhold_qst(doc, method=None):
    # Runs on validate, after HRMS has computed gross_pay (before_validate is too early for it). The formulas cannot
    # read the tariff table, so the tax is worked out here from the gross and set on the slip; the deductions are then
    # computed again, because the QST component's formula reads the field. Only when the amount changed, so a slip
    # saved again with the same gross is not computed twice.
    canton, code = frappe.db.get_value("Employee", doc.employee, ["swiss_qst_canton", "swiss_qst_code"]) or (None, None)
    amount = 0
    if code:
        rows = frappe.get_all(
            "QST Tariff", filters={"canton": canton, "tariff_code": code},
            fields=["canton", "tariff_code", "valid_from", *QST_DECIMAL_FIELDS],
        )
        # a Decimal of the float the database gives, so the tariff arithmetic stays exact
        rows = [{**row, **{f: Decimal(str(row[f])) for f in QST_DECIMAL_FIELDS}} for row in rows]
        try:
            amount = qst_amount(rows, canton, code, doc.gross_pay, getdate(doc.end_date))
        except LookupError as error:
            frappe.throw(str(error))
    if flt(doc.get("swiss_qst_amount")) != flt(amount):
        doc.swiss_qst_amount = flt(amount)
        doc.calculate_net_pay()


def ensure_structure():
    # One Salary Structure per Swiss company, made once (after install and after each migrate, so a new company is
    # covered). Submitted, so Salary Structure Assignments can name it.
    for company in frappe.get_all("Company", filters={"country": "Switzerland"}, pluck="name"):
        if frappe.db.exists("Salary Structure", {"company": company, "docstatus": ["<", 2]}):
            continue
        doc = frappe.get_doc({
            "doctype": "Salary Structure",
            "name": f"Swiss Monthly {company}",
            "company": company,
            "currency": frappe.get_cached_value("Company", company, "default_currency"),
            "payroll_frequency": "Monthly",
            "is_active": "Yes",
            "earnings": [{"salary_component": c} for c in STRUCTURE_EARNINGS],
            "deductions": [{"salary_component": c} for c in STRUCTURE_DEDUCTIONS],
        })
        doc.insert()
        doc.submit()


def load_tariff(path):
    # A year's update is a reload: the rows of the file's canton and valid-from dates are replaced, the rest kept.
    # The ESTV files are ASCII; run as `bench --site <copy> execute bi_payroll.swiss_payroll.load_tariff --args '["<path>"]'`.
    with open(path, encoding="latin-1") as f:
        rows = parse_tariff_file(f.read())
    if not rows:
        frappe.throw("The tariff file has no tariff records (Recordart 06 or 11).")
    frappe.db.delete("QST Tariff", {
        "canton": rows[0]["canton"],
        "valid_from": ["in", sorted({row["valid_from"] for row in rows})],
    })
    for row in rows:
        frappe.get_doc({"doctype": "QST Tariff", **row}).insert(ignore_permissions=True)
    frappe.db.commit()
    return len(rows)
