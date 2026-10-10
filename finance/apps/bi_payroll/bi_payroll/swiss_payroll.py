"""The frappe side of the Swiss payroll: the rate copies on each slip, and the structure per Swiss company.

The formulas (fixtures/salary_component.json) are evaluated by HRMS with the slip and the assignment as their
names. HRMS's formula context has no way to read a settings doctype, so the rates are copied here, on
validate, and the slip keeps the rates it was computed with.
"""

from decimal import Decimal

import frappe
from frappe.utils import flt, getdate

from bi_payroll.quellensteuer import parse_tariff_file, qst_amount
from bi_payroll.swiss_rates import RATE_FIELDS, STRUCTURE_DEDUCTIONS, STRUCTURE_EARNINGS, missing_components

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
    # a structure made before the Quellensteuer row existed has none: the tax would be computed and not deducted, so the
    # slip stops here, naming the manual step (README, Salary Structure). It is the structure that is checked, not the
    # slip's rows: a row whose amount was 0 on the first calculation is not on the slip yet. A slip without a tax is
    # not affected.
    has_row = frappe.db.exists("Salary Detail", {
        "parent": doc.salary_structure, "parenttype": "Salary Structure", "parentfield": "deductions",
        "salary_component": "Quellensteuer Employee",
    })
    if amount and not has_row:
        frappe.throw(
            f"Salary Structure {doc.salary_structure} has no Quellensteuer Employee row, so the tax of {amount} "
            "would not be deducted. Make the structure again with that row (README, Salary Structure) and assign it."
        )
    if flt(doc.get("swiss_qst_amount")) != flt(amount):
        doc.swiss_qst_amount = flt(amount)
        doc.calculate_net_pay()


def ensure_structure():
    # One Salary Structure per Swiss company, made once (after install and after each migrate, so a new company is
    # covered). Submitted, so Salary Structure Assignments can name it. A structure that no slip or assignment names
    # is replaced when it is defective: nothing depends on it yet, so the new one is made as a fresh structure would
    # be. One in use is not changed here: its gap is logged, so it is in the Error Log and not silent.
    for company in frappe.get_all("Company", filters={"country": "Switzerland"}, pluck="name"):
        existing = frappe.db.get_value("Salary Structure", {"company": company, "docstatus": ["<", 2]}, "name")
        if existing:
            structure = frappe.get_doc("Salary Structure", existing)
            missing = missing_components(
                [row.salary_component for row in structure.earnings],
                [row.salary_component for row in structure.deductions],
            )
            bare = formula_gaps(structure.earnings + structure.deductions)
            if not missing and not bare:
                continue
            if structure_in_use(existing):
                gaps = [f"has no {', '.join(missing)}"] if missing else []
                if bare:
                    gaps.append(f"has {len(bare)} rows without their component's formula")
                frappe.log_error(
                    title="Swiss Salary Structure lacks components",
                    message=f"Salary Structure {existing} for {company} {' and '.join(gaps)}. It is not changed by "
                    "bench migrate: make it again by hand (README, Salary Structure).",
                )
                continue
            if structure.docstatus == 1:
                structure.cancel()
            frappe.delete_doc("Salary Structure", existing)
            frappe.log_error(
                title="Swiss Salary Structure replaced",
                message=f"Salary Structure {existing} for {company} was not used and had {len(missing)} missing "
                f"components and {len(bare)} rows without a formula. It is made again from swiss_rates.py.",
            )
        doc = frappe.get_doc({
            "doctype": "Salary Structure",
            "name": f"Swiss Monthly {company}",
            "company": company,
            "currency": frappe.get_cached_value("Company", company, "default_currency"),
            "payroll_frequency": "Monthly",
            "is_active": "Yes",
            "earnings": structure_rows(STRUCTURE_EARNINGS),
            "deductions": structure_rows(STRUCTURE_DEDUCTIONS),
        })
        doc.insert()
        doc.submit()


def structure_in_use(name):
    # a slip or an assignment that names the structure, whatever its docstatus: then the structure is not replaced
    return bool(
        frappe.db.exists("Salary Slip", {"salary_structure": name})
        or frappe.db.exists("Salary Structure Assignment", {"salary_structure": name})
    )


def formula_gaps(rows):
    # the rows whose component has a formula but which have none of their own: HRMS computes such a row as 0
    return [
        row.salary_component for row in rows
        if not row.formula and frappe.db.get_value("Salary Component", row.salary_component, "formula")
    ]


def structure_rows(components):
    # A structure row keeps its formula only as it was typed: HRMS resets it on save, to the value it had before
    # validate. A row made with no formula therefore computes 0, so each row takes its component's formula here.
    # The statistical flag is copied too: a row that is statistical is kept off the slip (the employer shares).
    rows = []
    for name in components:
        component = frappe.db.get_value(
            "Salary Component", name, ["formula", "amount_based_on_formula", "statistical_component"], as_dict=True
        )
        rows.append({
            "salary_component": name,
            "formula": component.formula or "",
            "amount_based_on_formula": component.amount_based_on_formula,
            "statistical_component": component.statistical_component,
        })
    return rows


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
