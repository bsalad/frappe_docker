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
validation on submit is rolled back and listed by bexio id; a document that
was new in that run is rolled back whole (not left as a draft).

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

### Credit note (import_credit_note.py)

bexio's credit-voucher endpoint answers 404, so the export has no credit note
file. The one credit note is built from the export's two `KbCreditVoucher`
journal lines (revenue and VAT, both against the receivables), the payment row
of the invoice it is applied to (its `kb_credit_voucher_id`) and that invoice.
It becomes a Sales Invoice return against the invoice, with a negative item and
a negative VAT row, named by bexio's own number from the payment row's text, and
`update_outstanding_for_self` 0 so the return reduces the invoice's outstanding.
The module refuses a second credit note rather than guessing.

    python3 finance/bexio/import_credit_note.py [--export DIR] [--write FILE]

A dry run reads ERPNext and prints the totals; `--write` also writes the draft
plan for the loader (`sh finance/scripts/bexio-drafts.sh <plan> submit`), which
submits it. Its customer, currency and VAT account come from the same lookups as
the invoices. The address is not copied: the sales documents take their customer
only, as the invoices do, so the return's address is the customer's.

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
`<private>/bexio-files-dry-run.txt`. Rows: `no metadata` (no `files.json` or `bill_attachments.json`
entry), `no content` (no file under `files/`), `size differs`, `no document` (the Purchase Invoice is not in
ERPNext yet), `unlinked` (no record lists it), `shared` (two records list it). A file already attached
is recognised by its `bexio_id`, or, for a File that has none, by name and size, so a second run skips it.

The key is `File.bexio_id` (custom field, `swiss-setup.sh fields`). `--apply` writes it on each new
upload, and backfills the Files attached before it existed: a File without a `bexio_id` whose name and
size match exactly one planned file of its Purchase Invoice gets that file's id (a re-encoded image
matches by name). `to backfill` counts those; a File that matches none or several stays as it is and
is listed by its File name in `<private>/bexio-files-dry-run.txt`.

The bills list their files by uuid, and `/3.0/files` has the integer id and the same uuid. Both
`files.json` and `bill_attachments.json` (the attachments of the bills, `export.py --only bill_attachments`,
content from `/3.0/files/<uuid>/download`) are keyed by uuid; the `bexio_id` is the uuid,
not the integer id.

## Purchase bills

`import_purchase.py --dry-run` maps the bills (`bills.json`, with the lines of
`line_items`) to Purchase Invoices. `--apply` writes them as drafts, named by
bexio's `document_no`, through the same loader. A reverse-charge bill (Bezugsteuer
codes 19, 20, 32, 33) is mapped with its net as the amount; its VAT is booked to
the transitory Vorsteuer account 1172 and taken back on 2202, so the total is the net, as bexio
books it. Expenses are not written. A foreign-currency bill is booked in CHF at its
`exchange_rate`, as bexio books it, with the original in the remarks (see Currencies
under the sales import); it is written only when its CHF total is bexio's CHF
booking on 2000 within 5 rappen.

## VAT on the transitory accounts (VAT fix)

The sales and purchase loaders book the VAT of a document to its transitory account
(2202 for a sale at the invoice date, 1172 for a bill at the bill date), as bexio does.
Documents loaded before that booked it where bexio moves it on payment (2200, 1171, 1170).
`import_vat_fix.py` corrects them without touching a submitted document: one Journal Entry per
document, keyed by its bexio id, built from bexio's journal lines.

- invoice: a Sales Invoice's VAT, 2200 to 2202, at the invoice date (`vatfix-invoice-<id>`).
  A rappen that ERPNext's tax row differs from bexio's by goes to 6945, as the posting plan has it.
- bill: a Purchase Invoice's VAT to bexio's bill-date lines, 1172 (and 2202 and 2203 on a
  reverse-charge bill), at the bill date (`vatfix-bill-<uuid>`).
- payment VAT: each bexio journal line that moves VAT when a bill is paid (1171 or 1170 against
  1172, and 2202 against 2203), on the payment date, keyed by the line id. The ids are listed for erp-fd93.
- rounding: a bill open by a rappen, or a payment unallocated by one, is closed against the
  supplier's payable with the difference on 6940 (`rounding-bill-<uuid>`, `rounding-payment-<uuid>`).

The dry run reads ERPNext and prints, per year and account, bexio's document lines, bexio's other
lines (bank and manual entries, not in ERPNext yet), ERPNext now, the corrections and ERPNext after.
A document whose correction does not balance is listed by bexio id, not written.

    python3 finance/bexio/import_vat_fix.py --dry-run [--export DIR]
    python3 finance/bexio/import_vat_fix.py --write FILE [--export DIR]
    finance/scripts/bexio-drafts.sh FILE submit

A second run inserts nothing: a corrected document has no difference left, and the loader finds
each entry by its bexio id.

## Payroll journal lines (no source document)

The journal lines that no exported document carries (the posting plan's `unsourced` bucket: payroll and social
insurance) become one Journal Entry per line. `import_payroll.py` selects them and maps them; the posting plan
decides which lines these are, so the module reuses `posting_plan.classify` and does not look at accounts.

- key: `bexio_id` = `journal-<line id>`, and `user_remark` = `bexio journal <line id>: <description>`. A rerun inserts nothing.
- left out, by journal id, in `<private>/bexio-payroll-left-out.txt`: a line a banking entry and a manual entry both
  claim (posting plan rule 5), and a line an earlier VAT Journal Entry already books (`bexio-vat-on-payment*-ids.txt`).
- an account without an ERPNext Account by its bexio id is listed, and `--write` does not write the plan.

    python3 finance/bexio/import_payroll.py --dry-run [--export DIR]
    python3 finance/bexio/import_payroll.py --write FILE [--export DIR]
    finance/scripts/bexio-drafts.sh FILE submit
    python3 finance/bexio/import_payroll.py --check [--export DIR]

`--check` reads ERPNext after the loader run and writes nothing to it: the submitted entries keyed `journal-%`
against the lines, the GL per account and year against bexio's lines, the posted ids to `<private>/bexio-payroll-ids.txt`
(for the manual entries and bank bead, erp-fd93 and erp-7avs), and any missing or differing line to
`<private>/bexio-payroll-live-differences.txt`.

## Manual entries (Journal Entries)

`import_manual_entries.py` maps each bexio manual entry (single, compound, group, banking and untyped) to one
Journal Entry keyed `manual-<id>`. The VAT of a row goes to the account of its code's kind, the account bexio's
journal books it on, not to `tax_account_id`: 2200 for a sales code, 1170 for a purchase code of Material und
Dienstleistungen, 1171 for one of Investitionen und Aufwand. A reverse-charge code (Bezugsteuer) books its tax on
the Vorsteuer of its kind (1170 or 1171) and takes it back on 2203, as bexio's journal does for a manual entry. A
zero-rate code has no VAT split. A row's `tax_account_id` must be one of the row's own accounts; a row that is not
is listed, not guessed.

- an entry on a depreciation account is a Depreciation Entry; an entry on a receivable or payable account needs a
  party, which a manual entry does not carry, so it is listed and not written.
- `--extra FILE` adds entries the export does not hold (a correction), as a private list in bexio's shape.

    python3 finance/bexio/import_manual_entries.py --dry-run [--export DIR] [--extra FILE]
    python3 finance/bexio/import_manual_entries.py --write FILE [--export DIR] [--extra FILE]
    finance/scripts/bexio-drafts.sh FILE submit

The dry run also compares the mapping with bexio's journal, per entry and account, for 1170, 1171, 2200 and every
profit-and-loss account. The lines of an entry are its row lines and its VAT lines (bexio numbers a row's VAT line
right after the row; otherwise the entry's date and the row's text). Entries that differ are listed by id in
`<private>/bexio-manual-entries-journal-check.txt`; the terminal shows totals per year only.

`import_manual_fix.py` corrects the VAT of the live entries without touching a submitted document: for each live
entry (a submitted Journal Entry `manual-<id>`) the difference per account between the mapping and ERPNext's GL,
its own corrections included (`manual-<id>-<suffix>`, `vatfix-manual-<id>`), is one Journal Entry on the entry's
posting date, keyed `vatfix-manual-<id>` (or `vatfix-manual-<id>-2`, `-3` … when that key is taken: the loader never
rewrites a submitted document). A group with no difference gets nothing; an unbalanced one, or an account not in CHF,
is listed by bexio id. An entry listed in `<private>/bexio-manual-journal-wins.txt` (bexio id and reason) is the
exception to the mapping: its expected side is what bexio's journal books for it, and the dry run counts it apart.

    python3 finance/bexio/import_manual_fix.py --dry-run [--export DIR]
    python3 finance/bexio/import_manual_fix.py --write FILE [--export DIR]
    finance/scripts/bexio-drafts.sh FILE submit

A second run inserts nothing: a corrected entry has no difference left, and the loader finds each entry by its bexio id.

## Trial balance check

`check_trial_balance.py` compares ERPNext with bexio per business year and account, after the
import chain has run. bexio's side comes from `journal.json`, ERPNext's from its GL Entry rows
(not cancelled), read through the API user; nothing is written. Balance-sheet accounts (roots 1
and 2) compare the closing balance at the year's end; profit-and-loss accounts (roots 3 to 9)
compare the net movement within the year, since ERPNext has no Period Closing Voucher yet. bexio's
carry-forward lines and account 9100 are left out on bexio's side, and the vouchers with a leg on
9100 on ERPNext's side, so both sides compare the same postings.

    python3 finance/bexio/check_trial_balance.py [--export DIR] [--out CSV]

It prints totals only: years, accounts and rows compared, rows with a difference (more than half
a rappen), and the band of the largest one. The rows go to `<private>/bexio-trial-balance-<date>.csv`.
Exit status 1 means at least one difference.
