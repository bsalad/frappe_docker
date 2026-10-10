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
- `bi_finance/report/cash_position`: the "Cash Position" report (Bank and Cash accounts, their
  balances in original currency and in CHF, month-end history). Read-only. The Treasury
  workspace that shows it is set up by `finance/scripts/swiss-setup.sh treasury`.
- `bi_finance/test_cash_position.py`: offline tests, run in the image (see `finance/docs/erpnext-setup.md`).
