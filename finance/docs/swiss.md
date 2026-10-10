# Swiss compliance: erpnextswiss on ERPNext v16

Decision on whether libracore's erpnextswiss can carry the Swiss compliance of
the finance site (ERPNext v16.50.0), and the build path for a custom image that
includes it. The running stack is unchanged; the app is not installed on site
`frontend`. That is a later bead, after a backup.

## Verdict

**Fit, with one condition.** erpnextswiss covers the scope we need. Before any
real invoice carries a QR-bill, the QR rendering must move off libracore's
server (see Risks). Until then the app can be built and installed on a copy,
not used for live invoices.

Pinned in `finance/apps.json`:

- ERPNext `v16.50.0` (tag, commit `7474d9e`, the same tag the running stack uses)
- Frappe `v16.50.0` (tag, commit `f20f92d`, passed as `FRAPPE_BRANCH`)
- erpnextswiss branch `v16` at commit `5d85c45b48f4774cda6ba84ab75f138ad45a55c3`
  (2026-10-07)

## Source and licence

- Repo: https://github.com/libracore/erpnextswiss (description: "ERPNext
  application for Switzerland-specific use cases").
- Licence: AGPL-3.0 (`LICENSE`; `app_license = "AGPL"` in `hooks.py`).
- Branches relevant to v16:
  - `v16`: the v16 fork. Its `README_V16.md` says it is based on upstream
    `v2025`, adds `frappe`/`erpnext` `>=16.0.0,<17.0.0` as bench dependencies,
    and fixes the workspace structure for v16. Last commit 2026-10-07. **Used.**
  - `v16_compatibility`: an older v16 attempt (last commit 2026-09-30). Not used.
  - `master` and `v15`: v15-era code, active (2026-10-07). Not used.
- Tags: none for v16. The pin is therefore a branch plus a commit (see Build).
- Open issues: 6 on the repo. Issue #143, "Will there be Support for erpnext
  v16?", is open since 2026-04-30 with no comments. Upstream has not announced
  v16 support; the `v16` branch is libracore's own adaptation.
- Dependencies (`pyproject.toml` on `v16`): `bs4`, `factur-x>=2.3,<5`,
  `unidecode`, `opencv-python`, `pymupdf`, `fintech`, `icalendar`, `lxml`,
  `paramiko<4`. Frappe itself is not a pip dependency.
- Hooks: `after_install` and `after_migrate` create the workspaces and the app
  tile; `before_migrate` retires old workspace route pages. Scheduler: a daily
  calibration check and EBICS sync, an hourly EDI pickup. A `doc_events` hook
  on `Contact` pushes to Nextcloud if configured. Custom fields are installed as
  fixtures (Sales Invoice, Purchase Invoice, Payment Entry, Account, Customer, ...).
- Patches (`patches.txt`) are old and small: five entries, the latest for v1.29.5.

## Scope against our list

| Need | erpnextswiss v16 | Evidence |
| --- | --- | --- |
| QR-bill on sales invoices | Yes, with a caveat | Print format `qr_sales_invoice`, and `templates/qrr_invoice`. The QR image is rendered by an external server (see Risks). |
| MWST declaration, effective method | Partly | `doctype/vat_declaration` with `vat_type` `effective` or `flat`, and effective-method rates on the net amount (not tested here). The VAT accounts per quarter agree with bexio, see below; the form's rows per rate do not yet. |
| MWST figures per period (`mwst_report.py`) | Partly | `--basis posting` (default) counts invoices by posting date. `--basis payment` (vereinnahmte Entgelte) counts each Payment Entry by its posting date, and splits it over the invoices it pays by allocated / grand total: their net and each tax row times that share, so a receipt is split over the rates of its invoices; a payment with no invoice reference splits no tax. Journal Entries' VAT rows are read on both bases by posting date (see "What the figures read" below). |
| MWST declaration, received | Partly | Report `kontrolle_mwst` and the Swiss MWST page (`kt_swiss_route_schweizer_mwst`). Not tested here. |
| Swiss chart of accounts | Yes | `erpnextswiss/coa_import/accounts_template.csv`, 180 rows. Root groups follow the KMU numbering: 1 Aktiven, 2 Passiven, 3 Betriebsertrag, 4 Aufwand Material/Waren/Dienstleistungen, 5 Personalaufwand, 6 Sonstiger Betriebsaufwand, 7 Nebenerfolg, 9 Abschluss. Not tested against our data. |
| camt.053 import | Yes | Bank import page, CAMT.053 format; a profile for Aargauische Kantonalbank. |
| camt.054 import | Yes | Bank import page, `read_camt054` in `bankimport.py`. |
| pain.001 payments | Yes | `payment_proposal` pain-001 templates; EBICS connection doctype. |
| ESR/QR reference matching | Yes | `scripts/esr_qr_tools.py`; ESR/QR fields on Payment Entry and Purchase Invoice. |
| Incoming QR and ZUGFeRD invoices | Yes | `zugferd/`, `qr_reader.py`, `factur-x`. |
| Missing or not checked | - | QR-bill layout against the current Swiss standard (not checked); EBICS against a real bank (not possible here); MWST received path (not tested); data migration from `swiss_accounting_software` (the README says not to uninstall it until the flows are validated). |

## VAT per quarter against bexio

bexio declares on payments received (vereinnahmte Entgelte), so the VAT of a
sale is due in the quarter of the receipt, and the input VAT of a bill in the
quarter of its payment. ERPNext books the same moves on the same dates
(`erpnext-setup.md`, "Method"). `finance/bexio/mwst_compare.py` compares the
payment-basis accounts per quarter, bexio's journal against the ERPNext General
Ledger, read only: 2200 (Ziffer 399), 1170 and 1171 (Vorsteuer, Ziffer 400 to 420)
and 2203 (Bezugsteuer), net per quarter; and the flows of 2200, 1170, 1171, 2202 and
1172 per quarter. The flow is the net of both sides of the lines on those accounts,
without the settlement (the lines against 2201) and the 1 January carry-forward, so a
debit and a credit of one account in one voucher count as one movement; the gross
debits and credits are printed as information and are not counted. On ERPNext's side
the gross, and so the flow, keeps the lines whose voucher
carries bexio's key (`bexio_keyed()`): bexio's journal lines and manual entries, the
direct card and bank entries the import made from bexio, the credit notes, and the
vatfix-manual lines that book manual VAT on 1171 and 2203. The other lines of those
accounts are ERPNext-only: the invoices with their invoice-date reversals, and the
vatfix-invoice and vatfix-bill mirrors on 2200, 1170 and 1171. They must net to zero
per quarter and account, which the check prints as its own measure. Result for 27
quarters (2020 Q2 to 2026 Q4): 338 comparisons; net 0 differences; flow 0; ERPNext-only
0. The gross, which is information, differs in 5 quarters (2023 Q4, 2024 Q2, 2025 Q1,
2026 Q1, 2026 Q2), all on 1171; see the paragraph on the gross below.

bexio settles in three steps, and the two GL checks of `mwst_report.py` measure its
moves, not the net. On receipt, the sales VAT moves 2202 -> 2200; on the bill's payment,
the input VAT moves 1172 -> 1170 or 1171. At the settlement, bexio books one entry per
quarter 2200 -> 2201 for the declared sales tax, and 2201 -> 1170 and 1171 for the
declared input tax; the payment of the declared total is 2201 -> bank. So 2200 and
1170 and 1171 net to zero in a settled quarter, and a net check proves nothing there.
The gross check sums the credits of 2200 from vouchers other than Sales Invoice (the
moves and manual sales tax) and the debits of 1170 and 1171 from vouchers other than
Purchase Invoice (the moves, and the direct bank, card and manual input tax), and reads
the settlement entry of the quarter (its 2200 debit and its 1170 and 1171 credits)
against both sums. A settlement that differs from the sums is a correction bexio made
in the settlement. 21 of 27 quarters are settled; 2026 Q3 and later are open. The
per-quarter comparison is in the private file `bexio-mwst-compare.csv`.

Explained, not a difference in the books:

- **1 January carry-forward.** bexio's journal repeats the closing balance of
  the year on 1 January as a line "provisorischer Saldovortrag" (1170, 1171 against
  273). ERPNext holds that balance from the year before and does not book it
  again. The comparison leaves those lines out of bexio's side, so the balance
  is counted once.
- **Rappen.** bexio's journal amounts carry more than two decimals in some
  lines; each line is rounded to the rappen, as ERPNext posts it.

Explained since the first run: the transit entries of 2026 Q2 and Q3 were the ERPNext
credit notes, which post their transit VAT to 2202 directly, as bexio does; their key
(`credit-<id>`) now counts them as bexio's, and the transit accounts net to zero in
every quarter. The invoice-date mirrors (vatfix-invoice, vatfix-bill) are ERPNext-only
on 2200, 1170 and 1171 and net against the invoices.

Explained: the gross differences (5 in 5 quarters, all on 1171) are netting inside a
voucher, not a difference in the books. bexio's direct card and bank input tax is in
ERPNext and matches bexio per day in every quarter. The differences sit on 12 days (2023
Q4 1, 2024 Q2 6, 2025 Q1 2, 2026 Q1 2, 2026 Q2 1), and on each of them the ERPNext line
is a `vatfix-manual-<id>` entry. `import_manual_fix` books the correction of a bexio
manual entry as one row per account of the difference, so a manual entry that bexio
posts as a debit and a credit on 1171 becomes one net debit in ERPNext. On the three 2026
Q1 days, bexio's 1171 debits minus its credits of the day, the settlement left aside,
equal the `vatfix-manual` debits of that day to the rappen. The nets agree in every
quarter; only the gross debits differ, which is why the difference count goes by the
flow.

Not explained yet: the settlement's input leg differs from the ERPNext input sum in 13 of 21
settled quarters, which the first run attributed to the direct input tax; that is not
proven (the payment-mode report reads the direct input tax since erp-9fkd). The settlement's
sales leg differs in 3, two of them by the same amount in adjacent quarters (2022 Q2 and Q3).

Not compared yet: the form's rows per rate (200, 302 to 343), the base of each
Ziffer, and the split of the Vorsteuer in 400 and 405. They need each receipt
split over the VAT rates of its invoices, from bexio's invoices and receipts.
The filed declarations (bexio's MWST-Abrechnung) are not read; they would show
any correction made in the form itself.

### What the figures read (`mwst_report.py`)

- **Invoices** (Sales and Purchase Invoices, submitted), by posting date, on the
  posting basis. The tax rows by their template (the bexio id).
- **Payments** (Payment Entries, submitted), by posting date, on the payment
  basis only: each invoice they pay, by the share allocated / grand total.
- **Journal Entries** (GL rows of submitted entries), by posting date, on both
  bases. A bank or card entry is dated by the payment, so both bases agree on it:
  - 1170 (Ziffer 400) and 1171 (Ziffer 405): the net debit of the voucher. A direct
    card or bank expense with input tax is here.
  - 2200 (sales tax, Ziffer 399): the net credit, in the row of its rate (302 to
    343). The base is the one non-VAT row of the voucher with the same sign (the
    net revenue). A sales tax with no such row, or at a rate not in the form, is
    counted apart and named in the report's stdout.
  - 2203 (Bezugsteuer, Ziffer 382 or 383 by the date): the net credit. Its base is
    not read, so the Ziffer's base stays zero.
  - Left out, because the same tax is already counted elsewhere or is not a
    Ziffer: a voucher with 2202 (a sales tax moved from 2202 to 2200), a voucher
    with 1172 (the bill payment moves between 1172 and 1170/1171, counted through
    the Payment Entries on the payment basis), and a voucher with 2201 (the
    settlement, whose 1171 row is the period's moves and direct lines in one).
- The CSV has, under each Ziffer that a Journal Entry touches, a line
  `journal entries (by account)`: the part of the Ziffer that came from entries.

Left out: the GL rows of other accounts, except the one net row that gives a sales
tax its base; the Umsatz rows 220 to 299 and the Abzüge (not checked); and
cancelled entries and other companies.

## Alternatives

**`swiss_accounting_software` (onfuseag, ONFUSE AG).** Repo:
https://github.com/onfuseag/swiss_accounting_software. Marketplace listing for
v15, v16 and nightly: https://cloud.frappe.io/marketplace/apps/swiss_accounting_software.

- Scope: Swiss QR-bill (generated on the server with `chqr`, spec v2.3),
  camt.054 auto-matching, pain.001, Swiss hours calculation, Abacus export.
- Strong point: the QR-bill is rendered locally, so no invoice data leaves the
  server.
- Gaps against our list: no MWST declaration, no chart of accounts template,
  no camt.053 import, no ZUGFeRD, no ESR/QR reference matching beyond the
  QRR/SCOR/NON settings.
- Activity: `main` last pushed 2026-09-06; a `version16-fix` branch last changed
  2026-01-29 and is already merged into `main`. No v16 pin in `pyproject.toml`.
- Licence conflict: GitHub and README say GPL-3.0; `hooks.py` says MIT; the
  `pyproject.toml` has no licence. Ask ONFUSE before relying on either.

**Commercial builds (ECOSIRE and similar).** Marketing pages claim QR-bill,
camt.054 and pain.001 for v15 and v16 at a fixed price. Unverified; no public
repo. Not considered further.

**`ateso-group/erpnext_swiss`.** A containerised ERPNext setup script, last
commit 2023-01-17. Not an app. Not considered.

**Pick.** erpnextswiss `v16`. It is the only candidate that covers the MWST
declaration, the chart of accounts and camt.053 as well as QR and pain.001. The
Swiss-local QR renderer of `swiss_accounting_software` is the better QR
design, so if the QR-server condition cannot be met, the fallback is to
combine it with erpnextswiss or to use it alone for QR and accept the gap in
MWST. This is a decision for Benchi.

## Build

Two app lists: `finance/apps.json` (ERPNext, erpnextswiss and HRMS at its pinned tag, the live
image) and `finance/apps-copy.json` (the same, the copy site's list, see `hrms.md`). The build uses
`images/custom/Containerfile`, which is the upstream full-image build. It takes the apps through
`bench init --apps_path`, with the list as a BuildKit secret.

```sh
finance/scripts/build-image.sh live <tag>     # finance/apps.json, with hrms
finance/scripts/build-image.sh copy <tag>     # finance/apps-copy.json, with hrms
```

The script:

1. Refuses a `copy` build if its list has no `hrms`.
2. Refuses if `frappe-finance-custom:<tag>` (or its `-base` or `-finance` layer) already
   exists, so a build never overwrites a tag in use. Pick a new tag.
3. Checks that the `v16` branch of erpnextswiss still points at the commit in the list
   (`git ls-remote`). It stops if the branch has moved.
4. Runs `docker build` with `--secret id=apps_json`, `FRAPPE_BRANCH=v16.50.0`,
   and `--no-cache`. The cache is off on purpose: a secret is not part of the
   layer cache key, so a cached `bench init` layer would keep the old apps.
5. Builds the `bi_finance` layer on top.

Result: `frappe-finance-custom:<tag>`. Not run against site `frontend`.

Why a commit in a JSON key and not in the ref: bench clones each app with
`git clone --depth 1 --branch <ref>`, so it accepts a branch or tag, not a
commit. The `commit` key is ignored by bench; the script enforces it. The
check happens before the build, so a branch move after the check and before
the clone is not caught.

Docker: `docker-buildx` was missing from the CLI. It was installed with
Homebrew (`brew install docker-buildx`) and linked into `~/.docker/cli-plugins`.

Build result: see the bead note.

## Risks

1. **Invoice data leaves the server.** The QR-bill print format
   (`qr_sales_invoice`), the `qrr_invoice` template and the Planzer label
   render the QR code through `https://data.libracore.ch/phpqrcode/...`. The
   URL carries the IBAN, the payer and receiver names and addresses, the amount
   and the reference. The upstream README (section on QR invoices) says so and
   recommends a personal server (`github.com/lasalesi/phpqrcode`). The host is
   hard-coded in the print format and template, so this needs a change before
   go-live: either a self-hosted renderer on the tailnet, or local QR generation
   in the app. This is the condition for the fit.
   **Resolved for our own format:** "BI Sales Invoice QR" (app `bi_finance`,
   `finance/apps/bi_finance`) draws the QR locally with pyqrcode and embeds it as
   a data URI; it makes no request to any host. erpnextswiss's QR formats stay
   off. Checked on a rendered sample (2026-10-10): the HTML has no http(s) URL;
   the QR is an inline data URI.
2. **Unverified QR-bill layout.** The slip is hand-built HTML, not a checked
   Swiss QR-bill. Compare a generated slip against a bank's validator before
   use. **Open for Benchi:** no validator run yet; the IBAN prints in groups of four.
   **Layout, fixed on the sample (2026-10-10):** the 105 mm payment part did not
   fit page 1. wkhtmltopdf reads the margins from `.print-format` rules, not from
   the format's margin fields, so the format sets them itself (top 15 mm, bottom
   1 mm, sides 0 with the body padded 15 mm). A short invoice now renders on one
   page with the slip at the foot (`private/qr-sample.pdf`, 1 page).
   **Layout, fixed in code (2026-10-10):** the slip is meant to sit at the foot of the
   last page for any page count. The format renders twice: the body alone with a marker,
   then the body, a gap computed from the marker's place in the PDF, and the slip
   (`qrbill.py` `sales_invoice_slip_spacer`). A body that leaves less than 105 mm moves
   the slip to a page of its own, at its foot. Offline, with wkhtmltopdf and invented
   rows, the foot lands 1.5 to 1.8 mm from the page edge for 1, 2 and 3 pages.
   **On the live site (bi5, 2026-10-10, erp-a6eq):** the short sample is 1 page with the
   slip at the foot (`private/qr-sample.pdf`). The long sample (40 rows) is 3 pages: the
   slip is at the foot of page 2, and page 3 is blank (`private/qr-sample-long.pdf`).
   **Cause of the blank page (2026-10-10, replayed offline on bi5 from the live print HTML):**
   the template's `.print-format` rule sets a 1 mm bottom margin. wkhtmltopdf reads it as the
   page margin, and the printview page also applies it as CSS to Frappe's `.print-format`
   wrapper, so the 1 mm follows the slip. The slip's bottom sits 0.4 to 0.65 mm above the
   content edge, so for some body heights the margin overflows and wkhtmltopdf adds a blank
   last page. **Fix:** the template override `.print-format-gutter .print-format
   { margin-bottom: 0; }` (bead erp-hp26; test `test_wrapper_margin_does_not_add_a_page`),
   which Frappe does not read as a page option. The page keeps its 1 mm margin.
   **Live on bi6 (2026-10-10, erp-6ycq):** the site runs `v16.50.0-swiss-bi6` (backup
   `20261010_023805-frontend`, taken before the swap). Samples from invented drafts, deleted
   afterwards, in `private/`: `qr-sample.pdf` (3 rows, 1 page, slip at the foot);
   `qr-sample-mid.pdf` (25 rows, 2 pages, slip at the foot of page 2); `qr-sample-long.pdf`
   (40 rows, 3 pages: 20 rows on each of pages 1 and 2, the slip alone at the foot of page 3).
   No page is blank in any of the three. The 40-row body does not fit two pages as the bead
   expected; the slip goes to a page of its own, as the layout rule above says it should.
   **Closed by design (2026-10-10, yardmaster decision on erp-6ycq):** a 40-row invoice
   takes three pages. The bead's "two pages" expectation was wrong: when less than 105 mm
   is left on the last body page, the slip goes alone to a page of its own, as SIX IG v2.3
   allows. No template change.
   **PDF host, fixed (2026-10-10):** `host_name` is the tailnet URL, set by the
   `host` step. The backend and queue containers reach it; a stock format renders.
   Mails and prints link to that URL.
3. **Overlap with `swiss_accounting_software`.** Both claim QR and camt/pain.
   The README warns against running both or uninstalling either without
   validation. Install one.
4. **Not released for v16.** No tag, a fork-style branch, an open issue with
   no reply. Treat each pin update as a full test, not a routine bump.
5. **Bench install with heavy dependencies.** `opencv-python` and `pymupdf` are
   large; the image build is long and the image is bigger.
6. **EBICS and bank files** have not been run against a real bank. Keep the
   EBICS connection disabled until a test on a bank test environment.
7. **Migration.** The app adds custom fields and workspaces on install and
   migrate. Install only on a copy of the site, after a backup (later bead).

## Known issues in pin 5d85c45 (2026-10-10)

The pin is the head of upstream's `v16` branch today (`git ls-remote`, 2026-10-10),
so there is no fixed commit to bump to. Found on the images `bi9` and `bi10` (the
live site ran `bi10` then) and on the HRMS copy image `hrms1`.

| # | Where (erpnextswiss 1.34.1) | Finding | Effect on us |
| --- | --- | --- | --- |
| 1 | `erpnextswiss/scripts/item_tools.py` line 43 | Stray comma after a SQL string: `SyntaxError: invalid syntax`. | None at runtime. The module is a maintenance script, not hooked, and nothing we use imports or calls it. Its two `@frappe.whitelist()` functions (`get_next_item_code`, `get_voucher_value`) cannot be called while it does not compile; no app in the image references them. It breaks erpnextswiss's own test discovery and `test_item_cleanup_native`. |
| 2 | `tests/test_ebics_automation` | 6 of 33 tests error, `ImportError: not properly registered`. | None. EBICS stays disabled (Risks 6). erpnextswiss's own tests are not part of our gate. |
| 3 | `tests/test_workspace_routes_native` | 2 of 7 tests error: `Schweizer Buchhaltung is an archive of the previous navigation and can no longer be edited`. | None. The archived workspace is not used. |

**Confirmed (read-only):** item 1 on `bi9`, `bi10` and `hrms1` with `py_compile`
(all three fail the same way). Items 2 and 3 were run on `hrms1` only, on a throwaway
site inside the copy stack (`erp-q38s` review, 2026-10-10: 64 of 76 test modules pass).
Not compared on `bi9`: no site without HRMS was built for it. The errors do not name
HRMS, so it is likely they also fail without it, but that is not shown.

**Does anything we use import item_tools?** No. Searched the image's apps for
`item_tools` and `get_voucher_value`: `hooks.py`, page and doctype code do not
reference them. Client Scripts stored in a site's database are not in the image and were not searched. The hits are `test_item_cleanup_native` (imports it), a
security test that reads the file, and a verifier script that names it as a string.
The `bi_finance` app does not import it.

**Repro** (image `frappe-finance-custom:v16.50.0-swiss-bi9`, read-only, `--rm`):

```sh
docker run --rm --entrypoint python3 frappe-finance-custom:v16.50.0-swiss-bi9 \
  -m py_compile /home/frappe/frappe-bench/apps/erpnextswiss/erpnextswiss/scripts/item_tools.py
```

Expected: `SyntaxError: invalid syntax` at line 43, exit 1. The test modules were run per
module on a throwaway site in the copy stack (`bench --site <throwaway> run-tests --module <m>`),
then the site was dropped.

**Policy.**

- Our own fixes to erpnextswiss are patches, kept in `finance/patches/erpnextswiss/` and
  applied right after the app is fetched in the image build. Nothing is edited by hand in
  the image, and nothing goes upstream (Benchi, 2026-10-10). The patches and the pin they
  apply to are listed in `finance/docs/erpnextswiss-patches.md`. A pin change may stop a
  patch from applying; the build then fails and names the patch.
- Bump the pin when upstream fixes it. At each image build, check the branch:
  `git ls-remote https://github.com/libracore/erpnextswiss refs/heads/v16`. The build
  script already stops when the branch has moved from the pin. A fix is a new commit,
  so treat the bump as a full test (Risks 4).
- Item 1 is fixed by patch `0001-item-tools-syntax.patch`; see
  `finance/docs/erpnextswiss-patches.md`.
