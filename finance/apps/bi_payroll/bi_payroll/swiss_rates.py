"""The Swiss rate fields, as one list: the settings doctype, the copies on the Salary Slip and the formulas agree on
these names. Pure Python, so the offline tests run without frappe (test_swiss_payroll.py)."""

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
