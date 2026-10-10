"""The frappe side of the Swiss payroll: the rate copies on each slip, and the structure per Swiss company.

The formulas (fixtures/salary_component.json) are evaluated by HRMS with the slip and the assignment as their
names. HRMS's formula context has no way to read a settings doctype, so the rates are copied here, on
validate, and the slip keeps the rates it was computed with.
"""

import frappe

from bi_payroll.swiss_rates import RATE_FIELDS

# Order matters: a formula can read only the components above it (BVG Age before the BVG components).
STRUCTURE_EARNINGS = ["Basic Salary"]
STRUCTURE_DEDUCTIONS = [
    "BVG Age",
    "AHV/IV/EO Employee", "AHV/IV/EO Employer",
    "ALV Employee", "ALV Employer",
    "BVG Employee", "BVG Employer",
    "NBU Employee", "UVG Employer",
    "KTG Employee", "KTG Employer",
    "FAK Employer",
]


def copy_rates_to_slip(doc, method=None):
    settings = frappe.get_cached_doc("Payroll Swiss Settings")
    for field in RATE_FIELDS:
        # an empty rate (an insurer not given yet) is 0: its component is then left out of the slip
        doc.set(field, settings.get(field) or 0)


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
