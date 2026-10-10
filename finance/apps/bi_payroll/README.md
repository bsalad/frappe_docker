# bi_payroll

Frappe app for BI Concepts' Swiss payroll on the HRMS copy (`finance/docs/hrms.md`). It is installed on the
copy only: it requires `hrms`, which the live site does not have, so nothing here goes into `bi_finance`.

- `bi_payroll/fixtures/salary_component.json`: the Salary Components with their formulas (AHV/IV/EO, ALV with
  the ceiling, BVG on a fixed yearly insured salary with the age bands, NBU, UVG, KTG, FAK). The abbreviations
  are the ones erpnextswiss's Salary Certificate reads (B, AHV, ALV, NBUV, PK). No 13th salary component.
- `bi_payroll/fixtures/custom_field.json`: the rate copies on Salary Slip (one read-only section), and the
  BVG insured salary on Salary Structure Assignment (fixed per year).
- `bi_payroll/swiss_rates.py`: the list of rate names, shared by the settings, the slip copies and the tests.
- `bi_payroll/swiss_payroll.py`: `copy_rates_to_slip` (a `before_validate` on Salary Slip: the formulas see only the
  slip and the assignment, so each slip takes the rates from Payroll Swiss Settings when it is validated) and
  `ensure_structure` (one "Swiss Monthly" Salary Structure per Swiss company).
- `bi_payroll/bi_payroll/doctype/payroll_swiss_settings`: the settings (single). The insurers' rates are empty until
  Benchi gives them. The BVG age-band rates are placeholders (half the statutory minimum), until the fund's plan
  sheet is in `private/payroll-insurance/`.
- `bi_payroll/test_swiss_payroll.py`: offline tests with invented employees and rates:
  `python3 -m unittest bi_payroll.test_swiss_payroll` from this directory (no frappe needed).
- `bi_payroll/test_layout.py`: the layout and stamp guard, copied from `bi_finance`:
  `python3 -m unittest bi_payroll.test_layout`.
- `finance/images/bi_payroll.Containerfile`: the layer on the HRMS copy image.
