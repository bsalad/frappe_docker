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
EMPLOYER_SHARES = [key for key in COMPONENTS if key.endswith("_employer")]
AMOUNTS = ["gross", "net", *COMPONENTS, "employer_total"]


def hand_over(slips, lines, by_month):
    """slips: dicts with name, employee, employee_name, ahv_number, month, gross_pay, net_pay (submitted slips).
    lines: {slip name: {component name: amount}}, the Salary Detail rows of each slip summed by component.
    by_month: one row per employee and month, else one row per employee for the whole period."""
    key_of = {name: key for key, name in COMPONENTS.items()}
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
