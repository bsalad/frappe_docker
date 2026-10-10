"""The payroll hand-over: the Swiss amounts per employee, for the trustee's 2026 filing.

One row per employee for the period, or per employee and month. The amounts are the slips' own (gross, net) and the
Salary Detail rows of each slip, summed by component, so the rows match the payslips.

Pure Python, so the offline tests run without frappe (test_hand_over.py). The report
(bi_payroll/bi_payroll/report/payroll_hand_over) reads the submitted Salary Slips and passes them here.
"""

# Column key -> the Salary Component's name in fixtures/salary_component.json. Quellensteuer is not a component yet
# (the cantonal tariff is open): its column stays 0 until a component with this name exists.
COMPONENTS = {
    "ahv_employee": "AHV/IV/EO Employee",
    "alv_employee": "ALV Employee",
    "bvg_employee": "BVG Employee",
    "nbu_employee": "NBU Employee",
    "ktg_employee": "KTG Employee",
    "quellensteuer": "Quellensteuer",
    "ahv_employer": "AHV/IV/EO Employer",
    "alv_employer": "ALV Employer",
    "bvg_employer": "BVG Employer",
    "uvg_employer": "UVG Employer",
    "ktg_employer": "KTG Employer",
    "fak_employer": "FAK Employer",
}
# The earnings the rows carry besides gross: Lohnausweis line 1 is the basic salary, the only earning there is.
EARNINGS = {"basic": "Basic Salary"}
EMPLOYER_SHARES = [key for key in COMPONENTS if key.endswith("_employer")]
AMOUNTS = ["gross", "net", *EARNINGS, *COMPONENTS, "employer_total"]

# The Lohnausweis (Form 11) lines that the rows fill, numbered as erpnextswiss's Salary Certificate numbers them
# (its fields 1 to 15, the same numbers as the form). A line not listed is not in payroll yet: docs/hrms.md has the
# full table. KTG is not on a line here; its own column keeps it visible until the trustee says where it goes.
# (column key, the form's line, the amounts summed into it)
LOHNAUSWEIS = [
    ("line_1", "1. Lohn", ["basic"]),
    ("line_8", "8. Bruttolohn total", ["gross"]),
    ("line_9", "9. Beiträge AHV/IV/EO/ALV/NBUV", ["ahv_employee", "alv_employee", "nbu_employee"]),
    ("line_10_1", "10.1 Ordentliche Beiträge berufliche Vorsorge", ["bvg_employee"]),
    ("line_11", "11. Nettolohn", ["net"]),
    ("line_12", "12. Quellensteuerabzug", ["quellensteuer"]),
    ("ktg_employee", "KTG Employee (not on a line: to check)", ["ktg_employee"]),
]


def hand_over(slips, lines, by_month):
    """slips: dicts with name, employee, employee_name, ahv_number, month, gross_pay, net_pay (submitted slips).
    lines: {slip name: {component name: amount}}, the Salary Detail rows of each slip summed by component.
    by_month: one row per employee and month, else one row per employee for the whole period."""
    key_of = {name: key for key, name in {**EARNINGS, **COMPONENTS}.items()}
    rows = {}
    for slip in slips:
        month = slip["month"] if by_month else None
        row = rows.get((slip["employee"], month))
        if row is None:
            row = rows[(slip["employee"], month)] = dict(
                employee=slip["employee"], employee_name=slip["employee_name"], ahv_number=slip["ahv_number"],
                month=month, slips=0, **{key: 0 for key in AMOUNTS})
        row["slips"] += 1
        row["gross"] += slip["gross_pay"]
        row["net"] += slip["net_pay"]
        for component, amount in lines.get(slip["name"], {}).items():
            if component in key_of:
                row[key_of[component]] += amount
    for row in rows.values():
        row["employer_total"] = sum(row[key] for key in EMPLOYER_SHARES)
        for key in AMOUNTS:
            row[key] = round(row[key], 2)
    return sorted(rows.values(), key=lambda row: (row["employee"], row["month"] or 0))


def lohnausweis(rows):
    """The hand-over rows, with the amounts summed by the Lohnausweis lines instead of the components: one row per row."""
    return [
        {
            **{key: row[key] for key in ("employee", "employee_name", "ahv_number", "month", "slips")},
            **{column: round(sum(row[key] for key in keys), 2) for column, _, keys in LOHNAUSWEIS},
        }
        for row in rows
    ]
