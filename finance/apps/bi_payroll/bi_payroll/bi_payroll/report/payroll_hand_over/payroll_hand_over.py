"""Payroll hand-over: the Swiss amounts per employee for a year, or for one month, for the trustee's filing.

The rows are read from the submitted Salary Slips of the company and their Salary Detail rows, so they are the
payslips' own amounts. Export them from the report (Excel or CSV) and send them to the trustee; the payroll data
stays out of the repository (finance/docs/hrms.md).

This is a list of amounts, not a Lohnausweis: the form's line numbers are not mapped here. That mapping is the
Salary Certificate's, and it is checked against the 2026 form before use (finance/docs/hrms.md, step 6).
"""

import calendar
import datetime

import frappe
from frappe import _
from frappe.utils import flt

from bi_payroll.hand_over import COMPONENTS, EMPLOYER_SHARES, hand_over


def execute(filters=None):
    filters = frappe._dict(filters or {})
    year = int(filters.year)
    by_month = bool(filters.month)
    if by_month:
        month = int(filters.month)
        start = datetime.date(year, month, 1)
        end = datetime.date(year, month, calendar.monthrange(year, month)[1])
    else:
        start, end = datetime.date(year, 1, 1), datetime.date(year, 12, 31)

    slips = frappe.get_all(
        "Salary Slip",
        filters={"company": filters.company, "docstatus": 1, "start_date": ["between", [start, end]]},
        fields=["name", "employee", "employee_name", "start_date", "gross_pay", "net_pay"],
        order_by="start_date asc",
    )
    if not slips:
        return columns(by_month), []

    ahv_numbers = {
        e.name: e.social_security_number or ""
        for e in frappe.get_all(
            "Employee",
            filters={"name": ["in", list({s.employee for s in slips})]},
            fields=["name", "social_security_number"],
        )
    }
    lines = {}
    details = frappe.get_all(
        "Salary Detail",
        filters={"parenttype": "Salary Slip", "parent": ["in", [s.name for s in slips]]},
        fields=["parent", "salary_component", "amount"],
    )
    for d in details:
        slip_lines = lines.setdefault(d.parent, {})
        slip_lines[d.salary_component] = slip_lines.get(d.salary_component, 0) + flt(d.amount)

    rows = hand_over(
        [
            {
                "name": s.name, "employee": s.employee, "employee_name": s.employee_name,
                "ahv_number": ahv_numbers.get(s.employee, ""), "month": s.start_date.month,
                "gross_pay": flt(s.gross_pay), "net_pay": flt(s.net_pay),
            }
            for s in slips
        ],
        lines,
        by_month,
    )
    return columns(by_month), rows


def columns(by_month):
    cols = [
        {"label": _("Employee"), "fieldname": "employee", "fieldtype": "Link", "options": "Employee", "width": 130},
        {"label": _("Name"), "fieldname": "employee_name", "fieldtype": "Data", "width": 160},
        {"label": _("AHV number"), "fieldname": "ahv_number", "fieldtype": "Data", "width": 130},
    ]
    if by_month:
        cols.append({"label": _("Month"), "fieldname": "month", "fieldtype": "Int", "width": 70})
    cols += [
        {"label": _("Slips"), "fieldname": "slips", "fieldtype": "Int", "width": 60},
        {"label": _("Gross"), "fieldname": "gross", "fieldtype": "Currency", "width": 120},
    ]
    cols += [
        {"label": _(name), "fieldname": key, "fieldtype": "Currency", "width": 120}
        for key, name in COMPONENTS.items() if key not in EMPLOYER_SHARES
    ]
    cols += [{"label": _("Net"), "fieldname": "net", "fieldtype": "Currency", "width": 120}]
    cols += [
        {"label": _(name), "fieldname": key, "fieldtype": "Currency", "width": 120}
        for key, name in COMPONENTS.items() if key in EMPLOYER_SHARES
    ]
    cols += [{"label": _("Employer total"), "fieldname": "employer_total", "fieldtype": "Currency", "width": 120}]
    return cols
