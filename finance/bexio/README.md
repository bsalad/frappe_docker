# bexio login

The bexio scripts log in through Benchi's bexio OAuth app, with read-only scopes.
bexio enforces the scopes, so the token cannot change the account, whatever the code does.

## Log in once

On the Mac mini's desktop Terminal:

```sh
varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/oauth.py login
```

The command prints the bexio authorize URL and opens it in the browser. Approve the
login there. The browser then returns to `http://localhost:8765/callback`, the local
listener on 127.0.0.1:8765 takes the code, and the refresh token is saved in the
macOS keychain (service `varlock`, account `finance:local:BEXIO_REFRESH_TOKEN`).
The token itself is never printed.

## What the scopes allow

Every scope is read-only: `openid offline_access` for the login and the refresh,
and `*_show` for contacts, notes, articles, invoices, offers, orders, deliveries,
bills, expenses, bank accounts and bank payments. There is no
`accounting` scope and no `*_edit` scope. The list is the `SCOPE` constant in
`oauth.py`; a later import adds `accounting` there, behind its own consent.

## How the scripts use it

Each run refreshes the access token from the keychain. bexio rotates the refresh token
on every refresh, so the new one is saved back into the same keychain item. The access
token is kept in memory only.

## Log in again

If the refresh token expires or is revoked, the scripts stop with
an error that names bexio's answer (for example `invalid_grant`) and says to run
`... oauth.py login` again. Run the login
command above again. A new login replaces the stored token.

Revoking the old personal access token (`BEXIO_TOKEN`) is a separate step, done in
bexio once the scripts run on this login.

## Master-data pipeline (export, then import)

1. Export, read-only, to private JSON files (needs the bexio login, so under varlock):

       varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/export.py

2. One-off per site, as Administrator in the backend container (the API user may not
   write these): `finance/scripts/swiss-setup.sh fields currencies`, and the banks
   that the Bank Accounts need:

       BANKS="$(python3 finance/bexio/import_master.py --print-banks)" finance/scripts/swiss-setup.sh banks

3. Import into ERPNext as `api-agent@finance.local` (token file `~/ws_yardr_finance/.erpnext-api`):

       python3 finance/bexio/import_master.py --dry-run   # totals only
       python3 finance/bexio/import_master.py

Each record is keyed by `bexio_id`; a second run changes nothing. Take a backup
first (`bench --site frontend backup`). The export directory is company data and
stays under `~/ws_yardr_finance/private/`, never in the repository.

## Sales documents (invoices, credit notes, orders, offers)

`import_sales.py` maps the exported sales documents to ERPNext: `invoices.json`
to Sales Invoice, `credit_vouchers.json` to Sales Invoice with `is_return`
against the original, `orders.json` to Sales Order, `offers.json` to Quotation.
Each record is read with its positions (the single-document call). The
functions return the ERPNext document as a dict; nothing is inserted or
submitted yet. The live run is erp-a2ma's, after the posting plan (finance-3qsp).

    python3 finance/bexio/import_sales.py --dry-run [--export DIR]

The dry run reads ERPNext and prints totals only: counts per export file, the
CHF net, tax and gross per year, and the invoice status counts (8 open, 9
paid). The differences and unmapped records, by bexio id, go to
`<private>/bexio-sales-differences.txt`.

- VAT: each position's bexio tax id is looked up in the Sales Taxes and Charges
  Templates by their `bexio_id`. A tax id without a template is unmapped; the
  rate never picks a template. The tax rows are computed from the positions,
  and bexio's own tax per rate is compared, not copied.
- Free-text positions and text lines go to the item `bexio Position`; the
  description keeps the bexio text. Discounts and unknown position types are
  unmapped, not guessed.
- Credit notes get `bexio_id` `credit-<id>`, since a credit note and an invoice
  can share an id. Their link to the invoice is the field `invoice_id`, not yet
  confirmed against a real export.
- Document number: kept in `remarks` ("bexio Nr. ..."); the dry run lists any
  field the ERPNext doctype does not have.

## Attachments (bexio files to Purchase Invoices)

`import_files.py` plans the attachment of the bexio files (receipts, PDFs) to the
Purchase Invoices they belong to, so the books keep their vouchers (GeBüV, see
`finance/docs/archiving.md`). A bill or an expense lists its files by bexio id
(`attachment_ids`); the file becomes a private ERPNext File on the Purchase Invoice with
the same `bexio_id`. The live run is erp-a2ma's. The dry run reads ERPNext and writes nothing:

    python3 finance/bexio/import_files.py --dry-run [--export DIR]

It prints totals per doctype only. The bexio ids of the files it cannot place go to
`<private>/bexio-files-dry-run.txt`. Rows: `no metadata` (no `files.json` entry), `no content`
(no file under `files/`), `size differs`, `no document` (the Purchase Invoice is not in ERPNext
yet), `unlinked` (no record lists it), `shared` (two records list it). A file already attached
is recognised by the description `bexio file <id>` or by name and size, so a second run skips it.
