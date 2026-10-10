"""The Swiss rate fields and the salary structure's components, as lists: the settings doctype, the copies on the
Salary Slip, the formulas and the structure agree on these names. Pure Python, so the offline tests run without frappe
(test_swiss_payroll.py)."""

RATE_FIELDS = [
    "swiss_ahv_ee", "swiss_ahv_ag",
    "swiss_alv_ee", "swiss_alv_ag", "swiss_alv_ceiling",
    "swiss_bvg_threshold", "swiss_bvg_coordination",
    "swiss_bvg_ee_25", "swiss_bvg_ee_35", "swiss_bvg_ee_45", "swiss_bvg_ee_55",
    "swiss_bvg_ag_25", "swiss_bvg_ag_35", "swiss_bvg_ag_45", "swiss_bvg_ag_55",
    "swiss_uvg_max", "swiss_uvg_ag", "swiss_nbu_ee", "swiss_ktg_ee", "swiss_ktg_ag", "swiss_fak_ag",
]

# Per assignment (Salary Structure Assignment), not from the settings: the insured salary is fixed per year.
ASSIGNMENT_FIELDS = ["swiss_bvg_insured_salary"]

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
    # its amount is set on the slip from the gross, after HRMS's validate (swiss_payroll.withhold_qst)
    "Quellensteuer Employee",
]
