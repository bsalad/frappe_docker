# bi_finance

Frappe app for BI Concepts' finance site. It adds the Swiss QR-bill to Sales
Invoice prints, with the QR code drawn on our own server (no external host, see
`finance/docs/swiss.md`).

- `bi_finance/qrbill.py`: payload per SIX QR-bill v2.3 and the QR image.
- `bi_finance/bi_sales_invoice_qr.html`: the print format "BI Sales Invoice QR",
  installed by `finance/scripts/swiss-setup.sh qrbill`.
- `bi_finance/qrbill.py` `sales_invoice_slip_spacer`: the gap that puts the slip at the
  foot of the last page. The format renders the body once to measure it (a marker's place
  in the PDF), so each print costs one extra wkhtmltopdf run. Assumes A4 with the format's
  15 mm top and 1 mm bottom margins.
- `bi_finance/test_qrbill.py`: offline tests, run in the image (see `finance/docs/erpnext-setup.md`).
- `bi_finance/camt.py`: reads a UBS camt.053 or camt.054 file, versions .04 and .08 (the namespace says
  which), into the account, the OPBD and CLBD balances and one dict per Bank Transaction; plans the dedupe
  (rule "id": the transaction_id is on the account or earlier in the file; rule "bexio": a bexio line of the
  account with the same date (value or booking) and amount, taken once; else "new"). Pure Python.
- `bi_finance/camt_import.py`: the ERPNext side. `dry_run(file_url, bank_account)` writes nothing;
  `import_statement(...)` creates the new Bank Transactions (submitted, Unreconciled) and stamps the bexio
  lines it matched. Refuses a file whose IBAN or currency is not the Bank Account's. The balance check is
  reported against the bank GL account (OPBD: before the date, CLBD: up to it), never blocking.
- `bi_finance/test_camt.py`: offline tests of both, with invented files: `python3 -m unittest bi_finance.test_camt`
  from this directory, or in the image as in `finance/docs/erpnext-setup.md`.
- `bi_finance/bi_finance/` is the module folder "BI Finance": the doctypes, reports, workspaces, number
  cards and charts that migrate syncs live there, and nowhere else (`bi_finance/test_layout.py` checks it).
- `bi_finance/bi_finance/report/cash_position`: the "Cash Position" report (Bank and Cash accounts, their
  balances in original currency and in CHF, month-end history). Read-only. The Treasury
  workspace (`bi_finance/bi_finance/workspace/treasury`), its number cards (`number_card/`) and the chart
  (`dashboard_chart/`) show it; migrate syncs them.
- `bi_finance/test_cash_position.py`, `bi_finance/test_treasury.py`: offline tests, run in the image
  (see `finance/docs/erpnext-setup.md`).
- Wise sync (read-only API): `bi_finance/bi_finance/doctype/wise_settings` (the token, the profile, hourly on/off),
  `bi_finance/wise_client.py` (the three GET calls and the statement rows), `bi_finance/bank_feed.py` (the
  shared feed: rows written once by transaction id, deduplicated against imported lines by account, date and
  amount; PayPal reuses it), `bi_finance/wise.py` (balances as Bank Accounts, the sync, the hourly job).
  `bi_finance/test_wise.py` is offline (fake HTTP), run in the image with `bi_finance.test_wise`.
  CHF rates: a live run stores the day rates of each Wise currency as Currency Exchange rows, from frankfurter.dev v2
  (the source set in Currency Exchange Settings), one request per currency and range; a dry run stores none.
- `bi_finance/bi_finance/doctype/bank_statement_upload/`: the doctype "Bank Statement Upload" (module BI Finance,
  `modules.txt`). A record per uploaded file: the Bank Account, the file, Dry run and Import buttons calling
  `camt_import` (the Bank Account is proposed from the file's IBAN), the summary as the dry run gave it, and the count and who of the import. An upload imports once,
  after a dry run on the same file; a failed run leaves the record Failed and its ledger writes rolled back.
  Each stamp of a bexio line leaves a Comment on that Bank Transaction, naming the upload and the date.
- `bi_finance/test_bank_statement_upload.py`: offline tests of the record, the stamp comment and the .json,
  with the frappe calls mocked; run in the image as `bi_finance.test_bank_statement_upload`.
- `bi_finance/cash_forecast.py`: the 13-week cash forecast without the site: the weeks of each line, the
  recurring costs (monthly, quarterly, yearly) from the purchase bills per supplier and from the outgoing bank
  lines per description key (the first three words, digits, dates and months dropped), the payroll run (the salary
  accounts 5000 to 5099 only, the 25th or the Friday before a weekend; the 57xx social contributions come with the
  insurers' bills, the 58xx costs with the bills or bank lines), the VAT owed (due at the end of the quarter's second month; a refund 30 days
  after), the running balance and the lowest week. Pure Python.
  The bank lines left out of the recurring costs: a payroll or VAT word, a payroll or VAT Journal Entry, a
  Payment Entry of a Purchase Invoice, or a bill of the same amount dated within five days.
  A bank group must have 80% of its gaps in its period, a bill group only its median gap in it; the lines of a
  bank group on one day count as one occurrence, their sum.
  A third source groups the bank outflows booked by Journal Entry by their contra account (`journal_occurrences`):
  one line per account with a regular series, labelled with the account's name from the chart. Left out as another line
  carries them: 5xxx and 1091 (payroll), 2270 to 2279 (insurers), 1170 to 1172, 2200 and 2202 (VAT), 2000 (bills), bank
  and cash accounts (transfers) and entries reconciled to a purchase invoice. The owners' current accounts (2100, 2121)
  are labelled "(average, discretionary)" and the filter "Include owner accounts" leaves them out. A bank group every
  line of which a Journal Entry reconciles to is left out of the bank source (`counted_by_contra`); a group with
  other lines stays whole, so some bank lines are counted by both sources (the dry run measures it).
- `bi_finance/bi_finance/report/cash_flow_forecast/`: the "Cash Flow Forecast" report (weeks, chart, lowest point) and
  `bi_finance/bi_finance/report/cash_flow_forecast_lines/`: the "Cash Flow Forecast Lines" report, each line with its source document.
  Both read the books through the Payment Ledger and the GL and call `cash_forecast.py`. Both are shortcuts
  on the Treasury workspace (`workspace/treasury`); `test_treasury.py` checks them.
- `bi_finance/test_cash_forecast.py`: offline tests of `cash_forecast.py` with invented data:
  `python3 -m unittest bi_finance.test_cash_forecast` from this directory.
- `bi_finance/cash_conversion.py`: the Cash Conversion Cycle arithmetic, pure Python: DSO, DPO and DIO per month
  (balance at month end over the month's flow, times its days; DIO 0 with no stock), CCC = DSO + DIO - DPO, the
  rolling 12-month values, and the actual days to pay from the Payment Entry references. Account groups too.
- `bi_finance/bi_finance/bi_finance/report/cash_conversion_cycle/`: the report "Cash Conversion Cycle" (GL and Payment
  Entries, since 2019), with the number cards `bi_finance/bi_finance/bi_finance/number_card/cash_conversion_*` and the
  dashboard chart `bi_finance/bi_finance/bi_finance/dashboard_chart/cash_conversion_trend` (the module folder, where migrate syncs them).
- `bi_finance/test_cash_conversion.py`: offline tests with invented numbers: `python3 -m unittest bi_finance.test_cash_conversion`
  from this directory (no frappe needed).
- Synced JSON (the module folder): migrate skips a file whose `modified` the site already has, so a change without a new stamp never ships.
  Change a synced JSON, bump its `modified` to now (UTC, `2026-10-10 10:35:00.000000`), then update its `sha` and `modified` in
  `bi_finance/synced_manifest.json`. `python3 -m unittest bi_finance.test_layout` fails until it does and prints the new sha and stamp.
