# Swiss setup of company BI Concepts

What the finance site (`frontend`, ERPNext 16.50.0) looks like after the Swiss
setup, how it was done, and how to redo it on a fresh site. Decision for the
app itself: `swiss.md`.

## State

| Item | Count |
| --- | --- |
| Image | `frappe-finance-custom:v16.50.0-swiss-bi6` (all eight ERPNext services; previous images `v16.50.0-swiss-bi5` and `v16.50.0-swiss-bi4`, `v16.50.0-swiss-bi3`, and `v16.50.0-swiss-bi1` and `v16.50.0-swiss`, kept for rollback) |
| Apps | frappe 16.50.0, erpnext 16.50.0, erpnextswiss 1.34.1, bi_finance 0.0.1 |
| Accounts, company BI Concepts | 179 (KMU chart, 9 roots, numbers in the names) |
| Sales Taxes and Charges Templates | 15 (bexio codes) |
| Purchase Taxes and Charges Templates | 27 (bexio codes) |
| Item Tax Templates | 42 (bexio codes) |
| Fiscal years | 12 (2015 to 2026), each linked to BI Concepts |
| Custom field `bexio_id` | 21 doctypes |

The 3+3 stock templates (`Switzerland normal/reduced/lodging VAT`) are gone.
The QR-bill print format of erpnextswiss is not enabled (it sends invoice
data to data.libracore.ch; separate decision, see `swiss.md`, Risks).

## What was done, in order

1. Checked that the chart could still be replaced: `GL Entry` count 0.
2. Backup: `bench --site frontend backup --with-files`. Files, in
   `sites/frontend/private/backups/` inside the `sites` volume:
   `20261009_203105-frontend-database.sql.gz`,
   `20261009_203105-frontend-site_config_backup.json`,
   `20261009_203105-frontend-files.tar`,
   `20261009_203105-frontend-private-files.tar`.
3. Image: `finance-local.yml` now sets `image: frappe-finance-custom:v16.50.0-swiss`
   on the eight services that ran `frappe/erpnext` (the override keeps
   `pwd.yml` as upstream has it, so merging upstream stays clean).
4. `docker compose -p frappe-finance -f pwd.yml -f finance-local.yml up -d`,
   then `bench --site frontend install-app erpnextswiss` and
   `bench --site frontend migrate`.
5. `finance/scripts/swiss-setup.sh` (steps `coa`, `vat`, `fiscal`, `fields`).

## The setup script

`finance/scripts/swiss-setup.sh [coa] [vat] [fiscal] [fields] [currencies] [banks] [qrbill] [host]`
pipes `swiss-setup.py` into bench's Python in the backend container. No argument
runs all steps. Every step skips what exists, so a second run changes nothing.

**qrbill.** Creates or updates the print format "BI Sales Invoice QR" from
`finance/apps/bi_finance/bi_finance/bi_sales_invoice_qr.html`, the file the
image installs. The QR code comes from the app's `qrbill.py`, drawn on the
server with pyqrcode (Frappe's own package), so no invoice data leaves it.
The format is enabled but not the default print format: Benchi chooses that
after seeing a sample. The slip is 105 mm at the foot of the last page, whatever the
number of pages: a short invoice keeps it on page 1, a long one gets it at the foot of
its last page, or on a page of its own when the body leaves less than 105 mm. The page
cannot measure its body (wkhtmltopdf runs with JavaScript off), so the format renders
twice. The first render has the body alone with a marker after it; pypdf reads the
marker's height on the last page (`qrbill.py` `sales_invoice_slip_spacer`), and the
second has a gap of that height before the slip. That is one more wkhtmltopdf run per
print, also for the HTML preview. The gap assumes A4 with the format's 15 mm top and
1 mm bottom margins. The margins are set in the format's CSS: wkhtmltopdf reads
`.print-format` margins, not the format's margin fields. The IBAN prints in groups of
four; the QR text does not.

**host.** Sets the site's `host_name` from the `HOST_NAME` environment variable, the
URL the containers can reach (`https://<machine>.<tailnet>.ts.net:8448`, the tailnet
name of the host). Without it, `wkhtmltopdf` fetches `/assets` from `frontend` port 80,
where nothing listens, and every PDF on the site fails with `ConnectionRefusedError`.
The name stays out of the repo, so it is passed on the command line:

```sh
HOST_NAME=https://<machine>.<tailnet>.ts.net:8448 finance/scripts/swiss-setup.sh host
```

The URL is also what mails and prints link to. Check it from the backend and the
queue containers (`curl .../api/method/ping` gives 200) before relying on it.

**coa.** Stops with exit 3 if the company has GL entries. Otherwise it deletes
the 3+3 templates, clears the company's account links and the Mode of Payment
accounts, deletes the company's accounts, and creates the accounts of
erpnextswiss's `coa_import/accounts_template.csv` (Kontenrahmen KMU, 179
rows). The company defaults named in the CSV are set (receivable 1100,
payable 2000, income 3200, expense 4200, cash 1000, round-off 6940, ...);
five defaults have no field in v16 and are skipped with a message. Account
2200 Umsatzsteuer gets account type `Tax`, which the CSV leaves empty and the
tax templates need. Mode of Payment `Cash` points at 1000 Kasse.
erpnextswiss's own importer is not used: it imports `pandas`, which the image
does not have (bead erp-p1xb).

**vat.** bexio's 42 VAT codes (`/3.0/taxes`), 1:1, replace the templates
ERPNext had. Each code is one template per kind, named after the code:
`UN81 8.1% Normalsatz`, `VM81 8.1% Normalsatz Material/DL`,
`BZB81 8.1% Bezugsteuer Invest./Aufwand`. The bexio id is in `bexio_id`
(unique) on all three template doctypes, and the importers map a document's tax
by that id, never by its rate. The template's description keeps bexio's period
(`bexio 28, gültig ab 2023-07`); a code that is inactive in bexio is disabled.

- Sales (15): `UN77`, `UN81`, `UR25`, `UR26`, `US37`, `US38`, `UEX`, `ULA`,
  `MEL`, `UNO`, `SUB`, `SPE`, `UO77`, `UO81`, `U00`. Booking to 2202, bexio's
  transitory account: the VAT is moved to 2200 when the invoice is paid.
- Purchase (27): `VM…`, `VIM`, `ZOLLM`, `BZM…` and `VB…`, `V00`, `VSF`, `ZOLLB`,
  `BZB…` book to the transitory Vorsteuer 1172 at the bill date (bexio's account;
  moved to 1170 or 1171 when the bill is paid); corrections `VES`, `VEV`, `VKÜ`
  (1172, 1173, 1174), one per bexio id, for 7.7 and 8.1.
- Item Tax (42): one per code, one row on the code's account.
- Defaults: `UN81` (sales) and `VM81` (purchase, Material/DL).

How the special codes are modelled:

- **Bezugsteuer** (`BZ…`, reverse charge): one purchase template with two rows,
  +rate on 1172 (Vorsteuer) and -rate on 2202 (the liability bexio books the
  reverse charge on). The net is 0.
- **Einfuhrsteuer** (`ZOLLM`, `ZOLLB`): rate 0. The import VAT is on the customs
  document; ERPNext does not deduct it from a percentage. Booking it is part of
  the import (open question in `bexio-mapping.md`).
- **Corrections** (`VES`, `VEV`, `VKÜ`): inactive in bexio. Booked as journal
  entries, so the templates exist for reference only. Their accounts (1172 to
  1174, by the order of the mapping) are not verified.
- Accounts of the other codes follow the kind (sales 2202, purchase 1172). The
  bexio journal books them so; `vat --check` compares the export's own accounts.
- Account types: 1172 to 1174, 2202 and 2203 are set to `Tax`, as 2200 is in `coa`:
  an Item Tax row takes only accounts of that type.

Retired: the ERPNext templates `USt…`, `VSt…` and `MWST…` (the 8 %-period
names of the first setup). `vat` deletes them once nothing refers to them; a
template still linked by an item or a document stops the step.

`swiss-setup.sh vat --check <export dir>` compares the export's `taxes.json`
and `accounts.json` with the templates (code, rate, account, active) and prints
each difference; it exits 1 on any. Needs the full export with `/3.0/taxes`.

Checked on a draft Sales Invoice (not saved) with the former 8.1 % template:
100.00 net gives 8.10 tax on 2200 and 108.10 total. The same numbers apply to
`UN81`.

Method: ERPNext books VAT on the invoice date. The effective method on
payments received (vereinnahmte Entgelte) is therefore a matter of the VAT
statement, not of these templates; it is not built or tested yet (`swiss.md`
lists erpnextswiss's MWST declaration as untested).

**fiscal.** Calendar years 2015 to 2026. 2026 existed; the others were added,
and each gets a row for BI Concepts.

**fields.** Custom field `bexio_id` (label "bexio ID", Data, unique, read
only, in standard filter, not copied with a document) on Customer, Supplier,
Contact, Address, Item, Account, Sales Invoice, Purchase Invoice, Journal Entry
and Payment Entry, and the master, document and tax-template doctypes after
them, File included (the bexio uuid of an attached file, see the Attachments
section of `finance/bexio/README.md`), placed after the name or title field where the doctype has
one. Read only blocks the form, not API or Data Import writes. Unique means two
documents with an empty id are fine; a second document with the same id is
rejected (checked, `UniqueValidationError`).

**gebuev.** The GeBüV safeguards (OR 958f): posted records cannot change or
disappear silently. Each item checks the current value, sets it if needed, and
prints what changed. A second run changes nothing.

- Accounts Settings "Enable Immutable Ledger" on. Cancelling a document then
  posts reversal entries and leaves the original GL rows unchanged. Without it,
  cancelling sets `is_cancelled` on the original rows in place.
- Accounts Settings "Delete Accounting and Stock Ledger entries on deletion of
  transaction" off (already off). Deleting a document keeps its ledger entries.
- Delete right: on Account, Customer, Supplier, Item, Bank Account, Company,
  Sales Invoice, Purchase Invoice, Journal Entry, Payment Entry and Bank
  Transaction only Accounts Manager keeps it. Other roles lose it, so
  api-agent loses delete on all of them. Frappe already refuses to delete a
  submitted document, for everyone, so this covers drafts and cancelled ones.
  Gap: Accounts Manager can still delete a cancelled document; closing that
  needs a server hook or removing the right from Accounts Manager.
- Track Changes (Version) on the same doctypes (a Property Setter, so a
  migrate keeps it). They were already on.

Not in the step: v16 has no separate audit-trail setting; Track Changes is the
version history. Freezing is a separate command, below.

**freeze.** `swiss-setup.sh freeze <YYYY-MM-DD> [--apply]` sets the company's
"Accounts Frozen Till Date": no posting dated on or before the date. It is a dry
run unless `--apply` is given. It shows the current freeze, the GL entries that
the date would cover, and the role allowed to post into frozen periods (set by
hand; the command does not set it). It refuses to move a freeze back. Not run
yet: the history import comes first.

**Treasury.** The workspace "Treasury" is a file of bi_finance, as are its number cards
(`number_card/`: the total in CHF, and one card per Bank or Cash account by its chart
number) and the month-end chart (`dashboard_chart/`). The image's migrate syncs them, so
nothing is made by hand on the site. A new bank account gets its card as a new file. The
cards read the report "Cash Position" (`report/cash_position`), which reads the GL, not Bank
Transactions. A foreign-currency balance is turned into CHF at the Currency Exchange rate of
the as-of date, or the latest one before it. ERPNext fills that table from its rate source
(Currency Exchange Settings, frankfurter.dev); the report never fetches a rate. A missing
rate leaves that account's CHF value out, and the report says so. The hooks of bi_finance
list the two doctypes (`importable_doctypes`), since Frappe syncs only its own cards and
charts from the module folders.

## Switching the stack to a new image

Recreating `backend` or the queue containers gives them new IPs. nginx in
`frontend` keeps the old backend IP, so ERPNext answers 502 until it is
restarted (down about 10 minutes on 2026-10-09 at 22:51). After every
recreate, restart `frontend` and `websocket` and check the ping:

```sh
docker compose -p frappe-finance -f pwd.yml -f finance-local.yml up -d
docker compose -p frappe-finance restart frontend websocket
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/api/method/ping   # must print 200
```

Back up first (`bench --site frontend backup --with-files`), and switch only
when no other session is writing to ERPNext.

To change only the `bi_finance` layer, build that layer on the current base image
under a new tag, then point `finance-local.yml` at it (the base is not rebuilt):

```sh
docker build --build-arg BASE=frappe-finance-custom:v16.50.0-swiss-bi1-base \
    --tag frappe-finance-custom:v16.50.0-swiss-bi6 --file finance/images/bi_finance.Containerfile .
```

## Redo on a fresh site

With the swiss image built (`finance/scripts/build-image.sh`), the stack
running on it and company BI Concepts (CH, CHF) created:

```sh
docker compose -p frappe-finance exec -T backend bench --site frontend install-app erpnextswiss
docker compose -p frappe-finance exec -T backend bench --site frontend install-app bi_finance
docker compose -p frappe-finance exec -T backend bench --site frontend migrate
finance/scripts/swiss-setup.sh
```

`bi_finance` (finance/apps/bi_finance) is this repo's app for the QR-bill. It is
not in `finance/apps.json` (bench clones apps from a git URL, and this one is
not pushed): `finance/scripts/build-image.sh` builds it as a layer on top of
the image. Its offline tests run in the image:

```sh
docker run --rm -v "$PWD/finance/apps/bi_finance:/home/frappe/bi_finance_src:ro" \
    frappe-finance-custom:v16.50.0-swiss-bi6 \
    sh -c 'cd /home/frappe/bi_finance_src && ../frappe-bench/env/bin/python -m unittest bi_finance.test_qrbill'
```

The Cash Position report has its own offline tests, in the same way (`bi_finance.test_cash_position`).
The setup script's pure parts are tested without a container:
`python3 -m unittest discover -s finance/scripts -p 'test_*.py'`.

On a site with transactions the `coa` step refuses; run `vat fiscal fields`
only.

## Bexio history

The bexio accounting history is in ERPNext, from the first business year to the
current one, loaded in the order of the import chain (`finance/bexio/README.md`,
"Import chain and the rerun"). Each record carries its bexio id in `bexio_id`,
the key that makes a rerun skip it.

How it was checked: `finance/bexio/check_trial_balance.py` compares, per business
year and account, bexio's journal with ERPNext's GL Entry rows (read only, nothing
is written). Balance-sheet accounts compare the closing balance, profit-and-loss
accounts the net movement of the year. The rows are kept outside the repository.

Result of the check on 2026-10-10: 8 business years, 88 accounts, 308
year-account rows compared. 24 rows differ, in 12 accounts:

- 20 rows are rounding of up to 3 rappen from ERPNext's 0.05 rounding of the
  imported invoices. Accepted, not fixed.
- 4 rows come from one open item: a 2024 sale that bexio booked without a
  customer. It stays open until the customer is named; then one Journal Entry
  with the customer as party posts it, and the check runs again.

The rerun of the chain changes nothing: each loader's dry run reports nothing to
write, and the counts of Sales Invoice, Purchase Invoice, Payment Entry, Journal
Entry, Bank Transaction and File are the same before and after. Orders and offers
are not part of the history and are not loaded.

Not reconciled, and not in the GL: 34 bank transactions in a foreign currency
(no GL entry is made for them), and 73 bank transactions that are not reconciled
with a voucher. Both wait for confirmation.

Not done here: no period is locked or frozen. The MWST per period against bexio's
declarations is a separate check.
