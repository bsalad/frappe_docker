# Swiss payroll on ERPNext v16: HRMS fit study

Decision document for running Swiss payroll in ERPNext v16 (ERPNext `v16.50.0`),
the way `swiss.md` decides the Swiss accounting. Benchi decided on 2026-10-10: full
HRMS in ERPNext, with the payroll history from bexio included. This document says how,
and what is still open. HRMS is in `finance/apps-copy.json` and installed on a copy site only
(see [Copy site](#copy-site)). The live stack is unchanged: it runs the tag in
`finance-local.yml`, without HRMS.

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

## Decision (2026-10-10)

Benchi decided on 2026-10-10:

- **Calculator:** ERPNext with HRMS, as above. Live payroll in ERPNext as soon as the HRMS
  copy passes, still in 2026.
- **ELM:** no own Swissdec certification. The transmission is bought from a certified vendor,
  if one takes our data (see `elm.md`). Vendor inquiry: not now.
- **2026 wage reports:** done by the company's trustee / payroll service. The payroll
  hand-over export is their input: the report `Payroll hand-over` in `bi_payroll`, per employee
  and year, or per employee and month. It lists gross, each Swiss deduction by component
  (AHV/IV/EO, ALV, BVG, UVG/NBU, KTG, FAK, Quellensteuer), net, the employer shares and the
  AHV number, and exports as Excel or CSV. It runs on the HRMS copy only.

**Open:** certified ELM transmitter: inquiry later.

The hand-over rows are checked against invented employees (`python3 -m unittest
bi_payroll.test_hand_over`). They are not yet run on the copy site with real slips. Two parts
are open and stay so: Quellensteuer has no component yet (its column is 0), and the Lohnausweis
line mapping is not checked against the 2026 form (step 6).

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
| ELM / Swissdec transmission | Not in erpnextswiss `v16` (no ELM file in its tree), not in HRMS | ELM 4.0 switched off; ELM 5.0 required for the 2026 payroll year. Sources disagree on the 4.0 end date (30.06.2026 per BFS; end 2025 per one vendor). Transmission needs Swissdec-certified payroll software | **Gap.** See Questions and `elm.md` | [BFS](https://www.bfs.admin.ch/bfs/de/home/grundlagen/elm.html), [Swissdec](https://www.swissdec.ch/abschaltung-elm-4-0), [AXA](https://www.axa.ch/de/unternehmenskunden/melden-und-mutieren/melden/elektronische-lohnmeldung.html) |
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
2. **HRMS image on a copy.** Add `hrms` (tag `v16.50.0`) to `finance/apps-copy.json` (the live
   list stays without it) and build the copy image as `swiss.md` does. Install on a copy of the site after a backup. Check that
   erpnextswiss and bi_finance still migrate and their tests pass.
3. **Swiss salary components and structure.** Components with the formulas above, as
   fixtures of the new app `bi_payroll` (`finance/apps/bi_payroll/`), not `bi_finance`: HRMS is not
   on the live site, and a fixture that names an HRMS doctype would break the live migrate. The app
   requires `hrms` and is installed on the copy only. The rates are the doctype Payroll Swiss
   Settings, not constants in code, so a 2027 change is a settings edit; each Salary Slip keeps
   the rates it was computed with. Invented-data tests for each formula, with the ALV ceiling and
   the BVG threshold (`python3 -m unittest bi_payroll.test_swiss_payroll` in the app folder).
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
with formulas (AHV, ALV, BVG, UVG/NBU, KTG, FAK) as settings, in the `bi_payroll` app (its fixtures and Payroll Swiss Settings).
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

## Lohnausweis map (erp-fs9c)

The report `Payroll hand-over` (bi_payroll) has a view `Lohnausweis` that sums the same rows into the
Form 11 lines. The line numbers and labels are the fields of erpnextswiss's Salary Certificate. Not yet
checked against the 2026 form (step 6 of the plan).

| Form line | Salary Component(s) on the slip | Column in view `Lohnausweis` | Status |
|---|---|---|---|
| 1. Lohn / Rente | Basic Salary | `line_1` | in payroll |
| 2.1 Verpflegung und Unterkunft | none | — | not in payroll |
| 2.2 Privatanteil Geschäftsfahrzeug | none | — | not in payroll |
| 2.3 Weitere Gehaltsnebenleistungen | none | — | not in payroll |
| 3. Unregelmässige Leistungen | none | — | not in payroll |
| 4. Kapitalleistungen | none | — | not in payroll |
| 5. Beteiligungsrechte | none | — | not in payroll |
| 6. Verwaltungsratsentschädigungen | none | — | not in payroll |
| 7. Andere Leistungen | none | — | not in payroll |
| 8. Bruttolohn total | gross pay of the slip | `line_8` | in payroll |
| 9. Beiträge AHV/IV/EO/ALV/NBUV | AHV/IV/EO Employee, ALV Employee, NBU Employee | `line_9` | in payroll |
| 10.1 Ordentliche Beiträge berufliche Vorsorge | BVG Employee | `line_10_1` | in payroll |
| 10.2 Beiträge Einkauf berufliche Vorsorge | none | — | not in payroll |
| 11. Nettolohn / Rente | net pay of the slip | `line_11` | in payroll |
| 12. Quellensteuerabzug | Quellensteuer | `line_12` | 0 until erp-6rrh lands (no component yet) |
| 13.1.1 Effektive Spesen Reise / Verpflegung / Übernachtung | none | — | not in payroll |
| 13.1.2 Effektive Spesen übrige | none | — | not in payroll |
| 13.2.1 Pauschalspesen Repräsentation | none | — | not in payroll |
| 13.2.2 Pauschalspesen Auto | none | — | not in payroll |
| 13.2.3 Pauschalspesen übrige | none | — | not in payroll |
| 13.3 Beiträge an die Weiterbildung | none | — | not in payroll |
| 14. Weitere Gehaltsnebenleistungen | none | — | not in payroll |
| 15. Bemerkungen | none (free text) | — | not in payroll |
| (no line) | KTG Employee | `ktg_employee` | open: the trustee says where it goes |

Unentgeltliche Beförderung and Kantinenverpflegung (checkboxes on the form) are not in payroll either.
The employer shares (AHV, ALV, BVG, UVG, KTG, FAK) are not on the form's lines and stay in the
hand-over view only.

## Copy site

A copy of the live books with HRMS installed, to try the payroll work on before live. It is
a separate compose project, `frappe-finance-copy`, with its own volumes and network. It
listens on `127.0.0.1:8081` and on the tailnet only, on port 8462
(`https://<machine>.<tailnet>.ts.net:8462`), never Funnel. It holds the full books, so it
stays on this machine and the tailnet. The live stack is not touched by any of this.

- Image: `frappe-finance-custom:v16.50.0-swiss-hrms1` (ERPNext and HRMS 16.50.0, erpnextswiss
  1.34.1, bi_finance), built on `v16.50.0-swiss-hrms1-base`.
  Live stays on the tag in `finance-local.yml`. A later app (bi_payroll) gets its own layer on top.
- Build: from `finance/apps-copy.json`, under a tag that does not exist yet (the script
  refuses an existing one, so the live tags cannot be overwritten):

  ```sh
  finance/scripts/build-image.sh copy <new-tag>
  ```

  The live image is built the other way round, from `finance/apps.json` (no HRMS):
  `finance/scripts/build-image.sh live <new-tag>`. See `swiss.md`, Build.
- Safety settings on the copy's site, set after every restore, do not turn them off:
  `pause_scheduler` 1 and `mute_emails` 1 (no scheduled jobs, no mail), no enabled Webhook,
  no outgoing Email Account, `host_name` the copy's own URL, and the app name starts with
  `COPY`, so it cannot be mistaken for live.
- The copy's Administrator password is in `private/.erpnext-admin-copy` (mode 600), never in
  the repo. The compose override `finance-copy.yml` holds no secrets.

Every command below names the project `frappe-finance-copy` and the override file. Never
`-p frappe-finance` here: that is the live stack.

**Start**

```sh
docker compose -p frappe-finance-copy -f pwd.yml -f finance-copy.yml up -d --scale scheduler=0
/opt/homebrew/bin/tailscale serve --bg --https=8462 http://127.0.0.1:8081
```

`--scale scheduler=0` keeps the scheduler off. The first start creates a new site named
`frontend` (a few minutes). It is replaced by the restore below.

**Stop** (keeps the volumes)

```sh
/opt/homebrew/bin/tailscale serve --https=8462 off
docker compose -p frappe-finance-copy -f pwd.yml -f finance-copy.yml down
```

**Refresh from a new backup**

1. A fresh backup of the live site. It only reads the books:
   `docker exec frappe-finance-backend-1 bench --site frontend backup --with-files`.
   Note the timestamp prefix of the four files it writes.
2. Copy the four files (`*-database.sql.gz`, `*-site_config_backup.json`, `*-files.tar`,
   `*-private-files.tar`) out of `frappe-finance-backend-1:/home/frappe/frappe-bench/sites/frontend/private/backups/`
   with `docker cp` into `private/copy-restore/` (mode 700), then into the same folder in
   `frappe-finance-copy-backend-1`.
3. Restore into the copy, with the container paths:
   `docker exec frappe-finance-copy-backend-1 bench --site frontend restore <database.sql.gz> --with-public-files <files.tar> --with-private-files <private-files.tar> --db-root-password admin --force`
4. The restore replaces the copy's database with the live one, including the live password
   hash. Check `bench --site frontend list-apps`: if `hrms` is missing, run
   `bench --site frontend install-app hrms`, then `bench --site frontend migrate`. Then set the
   safety settings again, and the copy's own password:
   `bench --site frontend set-admin-password "$(cat private/.erpnext-admin-copy)"`.

The `admin` database root password is the upstream default in `pwd.yml`, for the copy's own
MariaDB, which only this stack reaches.

**Remove** (deletes the copy's books on this machine)

```sh
/opt/homebrew/bin/tailscale serve --https=8462 off
docker compose -p frappe-finance-copy -f pwd.yml -f finance-copy.yml down -v
```

Then delete `private/copy-restore/` and `private/.erpnext-admin-copy`.

## Sources

- HRMS: `frappe/hrms` branch `version-16`, tag `v16.50.0`, read through the GitHub API.
- ERPNext: `frappe/erpnext` tag `v16.50.0` (`7474d9e`).
- erpnextswiss: `libracore/erpnextswiss` branch `v16` (`README_V16.md`, `custom/employee.json`,
  `custom/salary_slip.json`, `doctype/salary_certificate*`, `doctype/payment_proposal_salary_slip`,
  `templates/print_formats/kt_salary_slip.html`).
- Swiss public values: the links in the table, from a web search on 2026-10-10. Those are
  secondary sources, except the BFS and Swissdec pages. The Form 11 Wegleitung was not opened.
