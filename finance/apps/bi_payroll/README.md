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
- `bi_payroll/hand_over.py` and `bi_payroll/bi_payroll/report/payroll_hand_over`: the report `Payroll hand-over`, per
  employee and year or per employee and month, for the trustee's filing (Excel or CSV). The rows are summed from the
  submitted slips and their Salary Detail lines; the logic is pure Python, so `bi_payroll/test_hand_over.py` tests it
  with invented employees.
- `bi_payroll/test_swiss_payroll.py`: offline tests with invented employees and rates:
  `python3 -m unittest bi_payroll.test_swiss_payroll` from this directory (no frappe needed).
- `bi_payroll/quellensteuer.py`: the ESTV withholding tariffs (Quellensteuer, Löhne, 2025 record format; source and
  version in the `QST Tariff` doctype's description). It parses a canton's text file (records 06 and 11), finds the
  bracket for a monthly gross and gives the tax (rate on the whole income, at least the minimum tax). Pure Python.
  Monthly method only: the gross of the slip, the tariff as published (it already holds the AHV/ALV/BVG share); annual,
  variable and 13th-salary cases are not handled. The 2026 file's record format is not checked yet.
- `swiss_payroll.withhold_qst` (a `validate` on Salary Slip, after HRMS has the gross): sets `swiss_qst_amount` and
  computes the deductions again; the `Quellensteuer Employee` component reads it. HRMS is not in this repo: the order
  against HRMS's own validate (the gross is known when the tax is set), the one row after the second calculation, and
  net = gross minus deductions were checked on a throwaway site with HRMS v16.50.0 (erp-5t6q, 2026-10-10, invented data).
- `bi_payroll/bi_payroll/doctype/qst_tariff`: the tariff rows (data, one per record). `swiss_payroll.load_tariff(path)`
  reloads a canton's file: the rows of its valid-from dates are replaced. Employees carry `swiss_qst_canton` and
  `swiss_qst_code` (Employee custom fields, fixtures).
- `bi_payroll/test_quellensteuer.py`: offline tests with invented rows in the ESTV format:
  `python3 -m unittest bi_payroll.test_quellensteuer`.
- Salary Structure, when a company's structure predates a component: `ensure_structure` makes the "Swiss Monthly"
  structure only when the company has none, so a submitted one is never amended by `bench migrate`. The gap is logged
  to the Error Log ("Swiss Salary Structure lacks components"), and a slip whose Quellensteuer amount is not zero stops
  on validate ("has no Quellensteuer Employee row") until the structure has it. The manual step: make the structure
  again with every row of `swiss_rates.py` (STRUCTURE_EARNINGS, STRUCTURE_DEDUCTIONS, the Quellensteuer Employee last),
  each row with its component's formula (`structure_rows` does this), submit it, cancel the old one, and move the
  employees' assignments to the new one. A Duplicate in Desk is not enough: it copies the old rows, and HRMS keeps a row
  without its formula at 0. Employees with no tariff code are not affected.
- Formulas: a structure row computes only with its formula stored (HRMS resets a row's formula on save to the value it
  had before validate). `ensure_structure` copies each component's formula onto its rows. A structure made before that
  change has empty formulas and computes 0 for every component; the live structure (erp-2s3c) was one of them.
- `bi_payroll/test_layout.py`: the layout and stamp guard, copied from `bi_finance`:
  `python3 -m unittest bi_payroll.test_layout`.
- `finance/images/bi_payroll.Containerfile`: the layer on the HRMS copy image.
