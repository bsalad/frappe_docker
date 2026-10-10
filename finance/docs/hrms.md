# Swiss payroll on ERPNext v16: HRMS fit study

Decision document for running Swiss payroll in ERPNext v16 (ERPNext `v16.50.0`),
the way `swiss.md` decides the Swiss accounting. Benchi decided on 2026-10-10: full
HRMS in ERPNext, with the payroll history from bexio included. This document says how,
and what is still open. **Nothing is installed.** The running stack is unchanged, and
the HRMS app is not in `finance/apps.json`.

Public facts only: no company data, no employee data, no amounts from the company.
Those stay in `private/`.

## Verdict

**Fit for the payroll core; four Swiss gaps, two of them need a decision before go-live.**

- Frappe HRMS `v16.50.0` gives what the payroll needs underneath: Salary Components with
  formulas, Salary Structures, Salary Slips, Payroll Entry for accruals and bank entries,
  and the leave and expense-claim doctypes.
- erpnextswiss `v16` (already on the site) has a Lohnausweis doctype (Salary Certificate),
  a salary payment step for the pain.001 run, and the AHV number on Employee. These need
  HRMS to work: they read `Salary Slip` and `Salary Detail`.
- HRMS has no Swiss rules. AHV/IV/EO, ALV, BVG, UVG/NBU, KTG and FAK can be written as
  Salary Components with formulas, with no code. Quellensteuer and ELM are not covered by
  either app (see Gaps).

## Versions

| Item | Pin | Evidence |
| --- | --- | --- |
| ERPNext | `v16.50.0`, commit `7474d9e` (live) | tag, as in `apps.json` and `swiss.md` |
| Frappe | `v16.50.0`, commit `f20f92d` | as in `swiss.md` |
| HRMS | tag `v16.50.0`, commit `7c03769` (branch `version-16`, head 2026-10-07) | `gh api repos/frappe/hrms/git/refs/tags/v16.50.0` |
| HRMS requires | `frappe/erpnext` (`required_apps` in `hrms/hooks.py`); Python `>=3.10` | `pyproject.toml` on `version-16` |

`v16.50.0` is the HRMS tag that matches ERPNext `v16.50.0`. Pin it the way `apps.json`
pins erpnext: a tag, with the commit noted here. The branch `version-16` moves; the tag does not.

## What HRMS covers (v16)

Source: `hrms/hr/doctype` (119 doctypes) and `hrms/payroll/doctype` on `version-16`.

- **Payroll:** `salary_component`, `salary_structure`, `salary_structure_assignment`,
  `salary_slip` (with `salary_detail` rows: `abbr`, `amount`, `year_to_date`),
  `payroll_entry`, `payroll_period`, `additional_salary`, `employee_other_income`,
  `arrear`, `retention_bonus`, `gratuity`, `payroll_correction`, `salary_withholding`.
- **Leave and time:** `leave_application`, `leave_allocation`, `leave_policy`,
  `leave_ledger_entry`, `attendance`, `shift_*`.
- **Expenses:** `expense_claim` and its details and taxes (the table that `payment-runs.md`
  says the site lacks until HRMS is in).
- **Accounting:** a Salary Slip submit makes no GL entry (`salary_slip.py`, `on_submit`).
  The GL comes from `Payroll Entry`: the accrual Journal Entry (`make_accrual_jv_entry`),
  which takes only the submitted slips of that Payroll Entry (`payroll_entry.py`,
  `get_sal_slip_list`, lines 311 to 327: `payroll_entry` set to it, and an empty
  `journal_entry`), and the bank entry for the net pay (`make_bank_entry`).
- **Employee:** not in HRMS. It is in ERPNext core (`erpnext/setup/doctype/employee`).

Regional code: `hrms/regional` has `india` and `united_arab_emirates` only. There is no
Swiss module.

## What HRMS lacks for Switzerland

- No AHV, ALV, BVG, UVG, KTG or FAK components. Their rates and ceilings are not in the app.
- No Quellensteuer tariff. Its `income_tax_slab` is a generic progressive table, not the
  cantonal withholding tariffs.
- No Lohnausweis (Form 11) and no ELM transmission.
- No pain.001 for salaries. Payroll Entry makes Journal and Bank Entries; the bank file is
  erpnextswiss's (see the pay step below).

## Swiss needs and how each is met

Rates are the 2026 public values from the sources in the last column. They are inputs to
the Salary Components, not hard-coded in the app. Each must be checked against the
insurer's or the Ausgleichskasse's own sheet before a live run.

| Need | How it is met | 2026 public values | Status | Source |
| --- | --- | --- | --- | --- |
| AHV/IV/EO | Salary Components with a formula on the gross AHV wage: 5.30% employee, 5.30% employer | 10.60% total, 5.30% each side. The sources do not agree on the split between AHV, IV and EO; the formula does not need it | Config, no code. Check the wage base (family allowances excluded) with the Ausgleichskasse | [weka](https://www.weka.ch/themen/personal/lohn-und-gehalt/lohnabrechnung/article/lohnabzuege-die-aktuellen-sozialversicherungsbeitraege/), [onlinetreuhand 2026](https://www.onlinetreuhand.ch/fileadmin/user_upload/Onlinetreuhand/Dokumente/Sozialversicherungen_2026.pdf) |
| ALV (incl. ceiling) | Component with `min(base, ceiling)`: 1.10% employee, 1.10% employer | 2.20% total up to CHF 148,200 a year (unchanged from 2025). No contribution above the ceiling: the 1% solidarity contribution (ALV 2) lapsed on 1 Jan 2023 | Config, no code. Nothing above the ceiling | same sources; [law.ch](https://law.ch/lawnews/2022/10/arbeitslosenversicherung-solidaritaetsprozent-entfaellt-per-01-01-2023/) |
| BVG (coordinated salary) | Component on `salary - coordination deduction`, with an entry threshold, and the age-based rates of the Pensionskasse. The split and the rates are the fund's plan, not the law | Entry threshold CHF 22,680; coordination deduction CHF 26,460 | Config is possible (the formula reads the employee's date of birth). The plan rates are open | [convit](https://convit.ch/wissen/bvg-koordinationsabzug), [auditrium](https://www.auditrium.ch/blog/articles/koordinationsabzug-2026-wichtige-aenderungen-fuer-unternehmen) |
| UVG / NBU | Components with the insurer's percentage on the insured salary. BU/UVG employer share, NBU employee share | Set by the insurer per policy | Config. Needs the policy sheets | none; insurer documents |
| KTG | Component with the insurer's rate on the insured salary | Set by the insurer | Config. Needs the policy sheet | none |
| FAK | Employer component on the AHV wage, rate by canton and Ausgleichskasse | Set by the fund | Config. Needs the fund's rate | none |
| Quellensteuer | Not covered. erpnextswiss's Salary Certificate settings have a field "12 Quellensteuerabzug" that maps a component; it does not compute the tax | Cantonal tariffs, per year | **Gap.** Build (bi_finance) or outsource. See Questions | `erpnextswiss` `salary_certificate_settings.json` |
| 13th salary | A Salary Component paid in December, or an accrual over the year (`accrual_component`) | Contract | Config. Choose the method (Question) | HRMS `salary_component` fields |
| Lohnausweis (Form 11), per employee per year | erpnextswiss `Salary Certificate`: sums `Salary Slip` gross and net over the year, and the `Salary Detail` rows by `abbr` (B, AHV, ALV, NBUV, PK) into the form's fields. Fields are set in `Salary Certificate Settings` | ESTV Wegleitung for Form 11 applies from 1 Jan 2026 | Needs HRMS. The field layout is **not checked** against the 2026 form | [ESTV Wegleitung 2026](https://www.estv.admin.ch/dam/de/sd-web/afP1GDFr8gE3/dbst-form-lohna-wegleitung-2026-de.pdf) (not opened in this study; from the search result) |
| ELM / Swissdec transmission | Not in erpnextswiss `v16` (no ELM file in its tree), not in HRMS | ELM 4.0 switched off; ELM 5.0 required for the 2026 payroll year. Sources disagree on the 4.0 end date (30.06.2026 per BFS; end 2025 per one vendor). Transmission needs Swissdec-certified payroll software | **Gap.** See Questions | [BFS](https://www.bfs.admin.ch/bfs/de/home/grundlagen/elm.html), [Swissdec](https://www.swissdec.ch/abschaltung-elm-4-0), [AXA](https://www.axa.ch/de/unternehmenskunden/melden-und-mutieren/melden/elektronische-lohnmeldung.html) |
| Payslip layout | HRMS print format on Salary Slip, built for us (the no-company-data rule keeps the template generic). erpnextswiss ships `kt_salary_slip.html`, a print for one of its own sample companies; use it as a reference only | none | Build. Small | erpnextswiss `templates/print_formats/kt_salary_slip.html` |
| Net salary payment (pain.001) | erpnextswiss `Payment Proposal` with `Payment Proposal Salary Slip` (the salary side of the payment run). The run in `payment-runs.md` leaves salaries out while the site has no HRMS | pain.001.001.09 as in `payment-runs.md` | Test on a copy after HRMS (erp-xpmr) | `payment-runs.md`; erpnextswiss `payment_proposal_salary_slip` |
| Employee AHV number | erpnextswiss custom fields `social_security_number` and `old_social_security_number` on Employee | n/a | Present in `v16` (`custom/employee.json`) | erpnextswiss `v16` |

## Migration from bexio

The bexio payroll export (bead erp-5sbz) is not run yet, so this section maps the entities
the bead asks for. Counts come with that export. Nothing below has been read from `private/`.

| bexio payroll | ERPNext / HRMS doctype | Note |
| --- | --- | --- |
| Employees | Employee (ERPNext core) | AHV number to `social_security_number`. Pay data is not on Employee |
| Salary types and insurance settings | Salary Component, Salary Structure | Set `abbr` to the codes erpnextswiss reads: B, AHV, ALV, NBUV, PK. Without these codes the Lohnausweis sums come out as zero |
| Paystubs (PDF, per employee, year, month) | Salary Slip, the PDF attached to it | The slip history is the data for the Lohnausweis of past years |
| Absences per employee and business year | Leave Application (or a Leave Allocation balance) | Only for the open year, or as a balance. Per-day history is not needed for the Lohnausweis |
| Payroll journal lines (booked) | Journal Entry (already loaded by `import_payroll.py`, bexio_id `journal-<id>`) | Stays as the GL |

### History: Salary Slips, not new GL

The GL already holds the history: `import_payroll.py` loads the payroll journal lines as
Journal Entries. Creating Salary Slips for those months does not create GL again, if the
slips are never picked up by a Payroll Entry accrual. The accrual picks only slips that
name that Payroll Entry (`payroll_entry`) and have an empty `journal_entry`
(`payroll_entry.py`, `get_sal_slip_list`). The rule for history: leave `payroll_entry`
empty. That alone excludes them. Setting `journal_entry` as well is optional: it is one Link,
and `import_payroll.py` loads one Journal Entry per bexio journal line (`journal-<id>`), so a
month can have several. Link it only when a month has a single entry; otherwise leave it empty. This is the design to test. It is not yet run.

The check after the load is the one `import_payroll.py --check` already does: the GL per
account and year against bexio's lines, with counts and totals only in the notes.

## Plan

Ordered beads. Each is one change, offline first; the live site is only changed in a deploy
slot that ledgerdemain gives (`erpnext-setup.md`).

1. **Benchi's decisions** (the Questions below). No code.
2. **HRMS image on a copy.** Add `hrms` (tag `v16.50.0`) to `finance/apps.json` and build a
   layer as `swiss.md` does. Install on a copy of the site after a backup. Check that
   erpnextswiss and bi_finance still migrate and their tests pass.
3. **Swiss salary components and structure.** Components with the formulas above, as
   fixtures of the `bi_finance` app (its files go under `finance/apps/bi_finance/bi_finance/bi_finance/`).
   The rates are settings, not constants in code, so a 2027 change is a settings edit.
   Invented-data tests for each formula, with the ALV ceiling and the BVG threshold.
4. **Employee load from the bexio export.** Offline importer, the same shape as
   `import_master.py`, writing only Employee and the AHV number. Mocked data in tests.
5. **History loader.** One submitted Salary Slip per employee and month, `payroll_entry`
   empty, PDF attached. Check: no new GL (`make_accrual_jv_entry` is not
   called on these), and the GL per account and year equals before the load.
6. **Lohnausweis check.** Compare the erpnextswiss Salary Certificate layout with the 2026
   Form 11. Compare one year's output with the bexio paystub totals (counts and totals in
   the note).
7. **Net salary pay.** erpnextswiss salary payment proposal with HRMS, on a copy. Test the
   pain.001 file against the schema check in `bi_finance/payment_run.py`. Link to erp-xpmr.
8. **Cash forecast.** The forecast reads the salary accounts 5000 to 5099 (`cash_forecast.py`).
   After HRMS, the Payroll Entry accruals post to the same accounts. Confirm the payroll line
   of the forecast does not count a salary twice once the net pay runs through the bank.
9. **Quellensteuer** (only if Question 2 says we build it). Its own bead, with the tariff
   tables as data.
10. **ELM** (only if Question 1 says we transmit ourselves). Its own bead, after a
    Swissdec-certification check.

Risks:

- **Privacy.** Payroll data is the most sensitive data in the company, and the repo is public.
  Every payroll file stays in `private/`. Tests use invented employees.
- **Install is a schema change.** HRMS adds many doctypes and custom fields on install and
  migrate. Install on a copy first; the rollback is the backup.
- **Image size and build time** grow with HRMS. Measure on the copy build, not from this study.
- **Lohnausweis is unchecked.** The erpnextswiss doctype was written for its own sample
  companies. Its field mapping is a config, not proof.
- **ELM and certification.** A payroll that is not Swissdec-certified cannot send ELM data.
  This is a decision, not a build step.
- **Double booking.** The history rule above depends on the accrual filter in
  `get_sal_slip_list`. The check in step 5 is the test of it, and must pass before any
  history is loaded live.

## Recommendation

Go with HRMS `v16.50.0`, pinned as a tag. Write the Swiss deductions as Salary Components
with formulas (AHV, ALV, BVG, UVG/NBU, KTG, FAK) as settings in the `bi_finance` fixtures.
Use erpnextswiss's Salary Certificate for the Lohnausweis, after its field layout is checked
against the 2026 Form 11. Load the bexio history as submitted Salary Slips with no
`payroll_entry`, so no GL is posted twice. Do not start the live site
until the two gaps have a decision: how ELM is transmitted, and how Quellensteuer is
calculated.

## Questions for Benchi

1. **ELM.** Transmit the Lohnmeldung ourselves, or through a Swissdec-certified payroll
   provider that takes our data? The second is the simpler route; the first needs a
   certification of the stack, and we do not know yet whether that is possible for ERPNext
   with HRMS. Who is the Ausgleichskasse and the tax office, and what do they accept?
2. **Quellensteuer.** Do any employees fall under source tax? If yes: build the tariff in
   `bi_finance`, or have the calculation done outside the system and enter the amount?
3. **BVG.** Which Pensionskasse, and its plan's rates by age band, employer and employee
   split? Is the insured salary fixed or does it follow the salary?
4. **Insurers and funds.** The names of the Ausgleichskasse, the UVG, NBU and KTG insurer,
   and the FAK fund, each with its current rate sheet. The rates are in these, not in the
   law.
5. **13th salary.** Paid in December, or accrued monthly? Either works in HRMS; it changes the
   components.
6. **History depth.** Which business years must the Lohnausweis cover from ERPNext? Each year
   back is one load of Salary Slips from bexio.
7. **Net salary payment.** Keep it on the UBS pain.001 run (erp-xpmr), once it works with
   HRMS? Or pay salaries by hand?
8. **Install order.** OK to install HRMS on a copy site first, and to deploy the live site
   only after the copy passes and in a slot ledgerdemain gives?

## Sources

- HRMS: `frappe/hrms` branch `version-16`, tag `v16.50.0`, read through the GitHub API.
- ERPNext: `frappe/erpnext` tag `v16.50.0` (`7474d9e`).
- erpnextswiss: `libracore/erpnextswiss` branch `v16` (`README_V16.md`, `custom/employee.json`,
  `custom/salary_slip.json`, `doctype/salary_certificate*`, `doctype/payment_proposal_salary_slip`,
  `templates/print_formats/kt_salary_slip.html`).
- Swiss public values: the links in the table, from a web search on 2026-10-10. Those are
  secondary sources, except the BFS and Swissdec pages. The Form 11 Wegleitung was not opened.
