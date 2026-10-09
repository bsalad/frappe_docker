# bexio login

The bexio scripts log in through Benchi's bexio OAuth app, with read-only scopes.
bexio enforces the scopes, so the token cannot change the account, whatever the code does.

## Log in once

On the Mac mini's desktop Terminal:

```sh
varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/oauth.py login
```

The command prints the bexio authorize URL and opens it in the browser. Approve the
login there (the export login, `login --export-scope`, is described above). The browser then returns to `http://localhost:8765/callback`, the local
listener on 127.0.0.1:8765 takes the code, and the refresh token is saved in the
macOS keychain (service `varlock`, account `finance:local:BEXIO_REFRESH_TOKEN`).
The token itself is never printed.

## What the scopes allow

Every scope of the read-only login is read-only: `openid offline_access` for the login and the refresh,
and `*_show` for contacts, notes, articles, invoices, offers, orders, deliveries,
bills, expenses, bank accounts and bank payments. There is no
`accounting` scope and no `*_edit` scope. The list is the `SCOPE` constant in
`oauth.py`.

## Broker (first choice for the export)

bexio grants the accounting journal, manual entries, bank transactions and files
only with the `accounting` and `file` scopes, and both are write scopes. The
Varlock broker (launchd `ch.bi-concepts.varlock-broker`, 127.0.0.1:18899) holds
the token and answers GET requests to api.bexio.com with it; every other method is
refused, and the caller only ever sees the placeholder `vlk_placeholder_bexio`.
With `BEXIO_BROKER=1` the client sends its requests through the broker as an HTTPS
proxy, trusting the broker's CA (`/private/var/vlbroker/ca/combined-ca.pem`). It reads
no keychain and runs no OAuth refresh, for any entity. No varlock and no person at
the screen are needed:

```sh
BEXIO_BROKER=1 python3 finance/bexio/export.py --out /Users/bsaladin/ws_yardr_finance/private/bexio-export/<date>-full --only manual_entries --only journal --only bank_transactions --only files
```

The export login below is only the fallback, for a machine without the broker. The
temporary write scope it needs is not used when the broker is.

## Export login (fallback, one run, then removed)

Without the broker, the export has a second login with `EXPORT_SCOPE` (the
read-only scopes plus `accounting` and `file`). It is kept apart from the
read-only one: its own keychain item (account `finance:local:BEXIO_EXPORT_REFRESH_TOKEN`),
and only `export.py` uses it, for `manual_entries`, `journal`, `bank_transactions`
and `files`. Every other entity still goes through the read-only login. The
client stays GET-only in both cases.

Run it on the Mac mini's desktop Terminal, before the export run:

```sh
varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/oauth.py login --export-scope
varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/export.py --out /Users/bsaladin/ws_yardr_finance/private/bexio-export/<date>-full --only manual_entries --only journal --only bank_transactions --only files
varlock run -p /Users/bsaladin/ws_yardr_finance/secrets -- python3 finance/bexio/oauth.py logout --export-scope
```

Afterwards, `logout --export-scope` deletes the export keychain item. It does not
revoke anything at bexio: revoke the app's access in bexio as well. If that revoke
also kills the read-only refresh token, log in again with `oauth.py login` (the read-only one).
A login that is never removed keeps the write scopes in the keychain, so the logout is not optional.

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
functions return the ERPNext document as a dict. Nothing is submitted, and no
GL posts: the documents are drafts (docstatus 0). The posting plan
(finance-3qsp) decides how they post; the submit is erp-a2ma's.

    python3 finance/bexio/import_sales.py --dry-run [--export DIR]
    python3 finance/bexio/import_sales.py --apply [--export DIR]

`--apply` writes the invoices and credit notes as drafts. The plan goes to
`<private>/bexio-sales-drafts.json`, and the loader
(`finance/scripts/bexio-drafts.sh`, `bexio-drafts.py`) inserts it inside the
backend container as Administrator. Each draft is named by bexio's
`document_nr` (set on insert, so the ACC-SINV series does not move) and keyed by
`bexio_id`: a rerun updates a draft in place, skips a submitted one, and changes
nothing that is already right. A foreign-currency invoice is booked in CHF, as
bexio books it (see Currencies below).

`finance/scripts/bexio-drafts.sh <plan.json> submit` also submits each draft
after it is loaded, in posting date order: that is the step that writes the GL.
A plan's `keep_draft` lists the bexio ids that stay drafts. A document already
submitted is skipped, so a second run submits nothing; a draft that fails
validation on submit is rolled back and listed by bexio id.

The dry run reads ERPNext and prints totals only: counts per export file, the
CHF net, tax and gross per year, and the invoice status counts (8 open, 9
paid). The differences and unmapped records, by bexio id, go to
`<private>/bexio-sales-differences.txt`.

- VAT: each position's bexio tax id is looked up in the Sales Taxes and Charges
  Templates by their `bexio_id`. A tax id without a template is unmapped; the
  rate never picks a template. The tax rows are computed from the positions,
  and bexio's own tax per rate is compared, not copied.
- Free-text positions and text lines go to the item `bexio Position`; the
  description keeps the bexio text. A position discount goes to ERPNext's
  discount fields. A bexio discount row becomes the document's discount (the
  lines less bexio's net); subtotal rows are left out. Prices that include the
  VAT are mapped with the tax row marked as included. An unknown position type
  is unmapped, not guessed.
- The total is bexio's `total` (the VAT included, after the discounts). A
  difference of up to 5 rappen goes into the last tax row; a larger one is
  unmapped and listed by bexio id.
- Where no tax row can take a difference of up to 5 rappen (prices that include
  the VAT, or a document bexio charged no VAT on), an invoice takes it as bexio
  booked it: a positive difference as one more item line `Rundung (bexio Total)`
  on the first line's income account, without a VAT code; a negative one as the
  grand-total discount (`apply_discount_on` "Grand Total"). The tax rows stay as
  ERPNext computes them, so the tax can be a rappen off bexio's; the differences
  line names the rappen and how they were taken. Orders, offers and credit notes
  keep the refusal, and so does a negative difference on an invoice that also
  has a document discount, since ERPNext has one discount field per document.
- The free-text items `bexio Position` (sales) and `bexio Aufwand` (purchases)
  are created by `import_master.py`, not by bexio.
- Credit notes get `bexio_id` `credit-<id>`, since a credit note and an invoice
  can share an id. Their link to the invoice is the field `invoice_id`, not yet
  confirmed against a real export.
- Document number: the name of the draft, and kept in `remarks` ("bexio Nr. ...").
  The dry run lists any field the ERPNext doctype does not have.
- Rounding: ERPNext cannot carry cents in its rounding adjustment (it recomputes
  it from the grand total), so a difference of up to 5 rappen goes into the last
  tax row, or where there is none, into the `Rundung` line or the grand-total
  discount described above.
- Quantities: the free-text item (`bexio Position`) takes whole quantities, and
  ERPNext keeps a quantity to three places. A line of a fraction, of more than
  three places, or with a discount on the free-text item is one unit at its
  amount, and its quantity and discount stay in its text ("1.58 x 1000.00 less
  10%: ..."). A zero-rate row keeps a zero price list rate, so ERPNext does not
  fill the free-text item's selling price into it.
- Not written by `--apply`: an invoice whose total differs from bexio's by more
  than 5 rappen, or by a difference no line or discount can take. It is listed by
  bexio id in `<private>/bexio-sales-differences.txt` and in the loader's output.

## Currencies

ERPNext 16 requires the party account's currency to equal the document's, and the
receivable and payable accounts are CHF. So a foreign-currency document is booked in
CHF, as bexio books it on 1100 and 2000: its conversion rate is 1, and each line rate
and tax is bexio's amount times the rate, to the cent. The original currency and
amount, with the rate, go into the remarks (`bexio: USD 1234.00 @ 0.90199`; a rate
from the ECB adds its day).

The rate is the one bexio booked the document at: the bill's `exchange_rate`, or for
an invoice the `currency_factor` of its receivable line in the journal. The ECB's
rate of the invoice date is used only where neither is given (and listed as a
difference).

A foreign document is checked against bexio's own CHF booking before it is written:
the CHF total of an invoice must be what the journal books on 1100 for it, and of a
bill what it books on 2000, within 5 rappen. Otherwise it is left out, with the
difference, and the journal's lines (`journal.json`, `accounts.json`) are the source.
The chart's EUR and USD accounts (1101, 2001) stay unused, as bexio did not use them.

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

## Purchase bills

`import_purchase.py --dry-run` maps the bills (`bills.json`, with the lines of
`line_items`) to Purchase Invoices. `--apply` writes them as drafts, named by
bexio's `document_no`, through the same loader. A reverse-charge bill (Bezugsteuer
codes 19, 20, 32, 33) is mapped with its net as the amount; its VAT is booked to
the Vorsteuer account and taken back on 2203, so the total is the net, as bexio
books it. Expenses are not written. A foreign-currency bill is booked in CHF at its
`exchange_rate`, as bexio books it, with the original in the remarks (see Currencies
under the sales import); it is written only when its CHF total is bexio's CHF
booking on 2000 within 5 rappen.
