app_name = "bi_payroll"
app_title = "BI Payroll"
app_publisher = "BI Concepts"
app_description = "Swiss payroll components for HRMS (AHV, ALV, BVG, UVG/NBU, KTG, FAK)"
app_license = "Proprietary"

# HRMS is needed for the payroll doctypes. This app is installed on the live site and the HRMS copy
# (Decision 2026-10-10). Keep it out of bi_finance: bi_finance must migrate where HRMS is not installed.
required_apps = ["erpnext", "hrms", "bi_finance"]

# The rates are settings, not constants: each slip takes a copy when it is validated, because the
# formulas read the slip and the assignment only (see swiss_payroll.py).
doc_events = {
    "Salary Slip": {
        "before_validate": "bi_payroll.swiss_payroll.copy_rates_to_slip",
        # after HRMS's validate, which computes the gross the withholding tax is worked out from
        "validate": "bi_payroll.swiss_payroll.withhold_qst",
    },
}

after_install = "bi_payroll.swiss_payroll.ensure_structure"
after_migrate = "bi_payroll.swiss_payroll.ensure_structure"

# The components and the slip fields are fixtures/ (exported with bench export-fixtures); the structure
# is made by ensure_structure, because it names a company.
fixtures = [
    # or_filters: two "like" filters in "filters" are ANDed and would export nothing
    {"dt": "Salary Component", "or_filters": [
        ["name", "like", "%Employee"], ["name", "like", "%Employer"], ["name", "in", ["Basic Salary", "BVG Age"]]]},
    {"dt": "Custom Field", "filters": [["name", "like", "Salary Slip-swiss_%"]]},
    {"dt": "Custom Field", "filters": [["name", "=", "Salary Structure Assignment-swiss_bvg_insured_salary"]]},
    {"dt": "Custom Field", "filters": [["name", "like", "Employee-swiss_qst_%"]]},
]
