# Swiss setup of company BI Concepts

What the finance site (`frontend`, ERPNext 16.50.0) looks like after the Swiss
setup, how it was done, and how to redo it on a fresh site. Decision for the
app itself: `swiss.md`.

## State

| Item | Count |
| --- | --- |
| Image | `frappe-finance-custom:v16.50.0-swiss` (all eight ERPNext services) |
| Apps | frappe 16.50.0, erpnext 16.50.0, erpnextswiss 1.34.1 |
| Accounts, company BI Concepts | 179 (KMU chart, 9 roots, numbers in the names) |
| Sales Taxes and Charges Templates | 7 |
| Purchase Taxes and Charges Templates | 14 |
| Item Tax Templates | 7 |
| Fiscal years | 12 (2015 to 2026), each linked to BI Concepts |
| Custom field `bexio_id` | 10 doctypes |

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

`finance/scripts/swiss-setup.sh [coa] [vat] [fiscal] [fields]` pipes
`swiss-setup.py` into bench's Python in the backend container. No argument
runs all four steps. Every step skips what exists, so a second run changes
nothing.

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

**vat.** One template per rate, since ERPNext tax templates have no validity
dates; the period is in the name.

| Rate | Kind | Period |
| --- | --- | --- |
| 8.1 % | Normal | from 2024-01-01 |
| 2.6 % | Reduziert | from 2024-01-01 |
| 3.8 % | Beherbergung | until 2017-12-31 and from 2024-01-01 |
| 7.7 % | Normal | 2018-01-01 to 2023-12-31 |
| 2.5 % | Reduziert | until 2023-12-31 |
| 3.7 % | Beherbergung | 2018-01-01 to 2023-12-31 |
| 8.0 % | Normal | until 2017-12-31 |

- Sales: `USt <rate>% <kind> (<period>)`, booking to 2200 Umsatzsteuer. 7 templates.
- Purchase: `VSt <rate>% <kind> (<period>) Material/DL` (1170) and
  `... Invest./Aufwand` (1171). 14 templates.
- Item Tax: `MWST <rate>% <kind> (<period>)`, with rows for 2200, 1170 and
  1171, so one template serves sales and purchase.
- Defaults: the 8.1 % sales template and the 8.1 % Material/DL purchase
  template.

Checked on a draft Sales Invoice (not saved): 100.00 net with the 8.1 %
template gives 8.10 tax on 2200 and 108.10 total.

Method: ERPNext books VAT on the invoice date. The effective method on
payments received (vereinnahmte Entgelte) is therefore a matter of the VAT
statement, not of these templates; it is not built or tested yet (`swiss.md`
lists erpnextswiss's MWST declaration as untested).

**fiscal.** Calendar years 2015 to 2026. 2026 existed; the others were added,
and each gets a row for BI Concepts.

**fields.** Custom field `bexio_id` (label "bexio ID", Data, unique, read
only, in standard filter, not copied with a document) on Customer, Supplier,
Contact, Address, Item, Account, Sales Invoice, Purchase Invoice, Journal Entry
and Payment Entry, placed after the name or title field where the doctype has
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

## Redo on a fresh site

With the swiss image built (`finance/scripts/build-image.sh`), the stack
running on it and company BI Concepts (CH, CHF) created:

```sh
docker compose -p frappe-finance exec -T backend bench --site frontend install-app erpnextswiss
docker compose -p frappe-finance exec -T backend bench --site frontend migrate
finance/scripts/swiss-setup.sh
```

On a site with transactions the `coa` step refuses; run `vat fiscal fields`
only.

## Left for the import

`finance/docs/bexio-mapping.md` (finance-b5kj) did not exist when this was
written. Once it does:

- Match bexio's account numbers to the 179 KMU accounts. The template is
  sparse (one bank account, 1020 UBS, and only a few revenue and expense
  accounts) and has quirks to settle first: 2202 Abrechnungskonto MWST carries
  tax rate 7.7, 6950 and 6990 (Finanzertrag, Währungsgewinne) sit under the
  Expense root 6, and root 8 is typed Income although it holds expense. Add
  missing accounts with numbers from bexio rather than renumbering.
- Match bexio VAT codes to the seven rates and the two Vorsteuer accounts
  (1170 against 1171). A bexio code for exempt, export or zero-rated sales has
  no template yet; add one when the mapping names it.
- Fill `bexio_id` on every imported record; use it as the key for re-runs.
- Currency: the template has EUR accounts (1020, 1101, 2001); check against
  bexio's foreign-currency accounts.
- Company field `chart_of_accounts` still reads `Standard`; it is not used
  after company creation.
