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
| MWST declaration, effective method | Yes | `doctype/vat_declaration` with `vat_type` `effective` or `flat`, and effective-method rates on the net amount. |
| MWST declaration, received | Partly | Report `kontrolle_mwst` and the Swiss MWST page (`kt_swiss_route_schweizer_mwst`). Not tested here. |
| Swiss chart of accounts | Yes | `erpnextswiss/coa_import/accounts_template.csv`, 180 rows. Root groups follow the KMU numbering: 1 Aktiven, 2 Passiven, 3 Betriebsertrag, 4 Aufwand Material/Waren/Dienstleistungen, 5 Personalaufwand, 6 Sonstiger Betriebsaufwand, 7 Nebenerfolg, 9 Abschluss. Not tested against our data. |
| camt.053 import | Yes | Bank import page, CAMT.053 format; a profile for Aargauische Kantonalbank. |
| camt.054 import | Yes | Bank import page, `read_camt054` in `bankimport.py`. |
| pain.001 payments | Yes | `payment_proposal` pain-001 templates; EBICS connection doctype. |
| ESR/QR reference matching | Yes | `scripts/esr_qr_tools.py`; ESR/QR fields on Payment Entry and Purchase Invoice. |
| Incoming QR and ZUGFeRD invoices | Yes | `zugferd/`, `qr_reader.py`, `factur-x`. |
| Missing or not checked | - | QR-bill layout against the current Swiss standard (not checked); EBICS against a real bank (not possible here); MWST received path (not tested); data migration from `swiss_accounting_software` (the README says not to uninstall it until the flows are validated). |

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

`finance/apps.json` lists the two apps. The build uses `images/custom/Containerfile`,
which is the upstream full-image build. It takes the apps through `bench init
--apps_path`, with the file as a BuildKit secret.

```sh
finance/scripts/build-image.sh            # tag v16.50.0-swiss
```

The script:

1. Checks that the `v16` branch of erpnextswiss still points at the commit in
   `apps.json` (`git ls-remote`). It stops if the branch has moved.
2. Runs `docker build` with `--secret id=apps_json`, `FRAPPE_BRANCH=v16.50.0`,
   and `--no-cache`. The cache is off on purpose: a secret is not part of the
   layer cache key, so a cached `bench init` layer would keep the old apps.

Result: `frappe-finance-custom:v16.50.0-swiss`. Not run against site `frontend`.

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
