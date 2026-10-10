# Payment runs (pain.001 for UBS)

A payment run pays due purchase invoices from the UBS current account. ERPNext
makes the Payment Proposal and the pain.001 file (erpnextswiss); the app
`bi_finance` adds the Payment Entries and the Swiss schema check
(`finance/apps/bi_finance/bi_finance/payment_run.py`). Benchi uploads the file in
UBS e-banking; the camt import later matches the bank lines to the Payment Entries.

Nothing is sent to a bank by ERPNext or by this code.

## Set up (once)

1. The Bank Account "UBS Kontokorrent - UBS Switzerland AG" holds the IBAN. The
   step below copies it to the GL Account "UBS Kontokorrent" (erpnextswiss reads it
   there), and sets the BIC of UBS Switzerland AG (UBSWCHZH80A, public) on the Bank
   and the GL Account when they are empty. It does the same for every other company
   bank account with an IBAN (the Wise account today). Without an IBAN and a BIC, no file can be made.
2. `finance/scripts/swiss-setup.sh payments`. It sets the erpnextswiss settings the
   run needs (pain.001.001.09, region CH, unidecode on), makes the IBAN and BIC fixes
   above, and says if the account still lacks an IBAN or BIC. A second run changes nothing.

## Run

1. Payment Proposal list, the create button. It takes the open Purchase Invoices due
   by the planning date (`planning_days` in ERPNextSwiss Settings) and the pay-from account.
   Check the list and the total.
2. Submit. Each paid invoice gets a Payment Entry as a **draft** (pay from the UBS
   account, to the creditor account of the invoice, allocated to that invoice).
   Invoices are then marked as proposed, so a second run does not pick them up.
3. "Download bank file" on the submitted proposal. The file must pass the Swiss
   schema check or nothing is downloaded. On success the draft Payment Entries are
   **submitted**, dated the day of the download at the latest.
4. Upload the file in UBS e-banking and approve it there.

Cancelling the proposal cancels its submitted Payment Entries and deletes the
drafts, and un-marks the invoices.

## The file

- Version: pain.001.001.09, the ISO 20022 version of the Swiss Payment Standards.
  erpnextswiss validates against the generic schema of that version; this app also
  validates against SIX's Swiss variant (`pain.001.001.09.ch.03.xsd`), which is the
  one banks name in their documentation.
- EndToEndId is what erpnextswiss writes: the supplier's invoice number (`bill_no`, else
  the Purchase Invoice name), cut to 35 characters; the invoices of one supplier in a run
  are one file row, so their numbers are joined by a space. It is **not** the Payment Entry
  name. Each Payment Entry carries the same invoice number as its `reference_no`, so the
  camt import (erp-f5ss) matches a bank line to Payment Entries on that number and the amount.
- The company's country code is written upper case (erpnextswiss writes it lower case).
- **Not verified:** that UBS e-banking accepts this file. No UBS source found
  confirms the version or the Swiss variant. Before the first live file, upload a
  test file to UBS's ISO 20022 test platform (or ask UBS, 0848 848 064) and note
  the result on the bead.

## Limits

- One Payment Entry per invoice. An aggregated transfer (several invoices to one
  supplier) is one bank line, so the camt match is one line to several Payment
  Entries; check that ERPNext's bank reconciliation takes it.
- Invoices in a currency other than the pay-from account's are refused (a Payment
  Entry needs an exchange rate, not built yet).
- Expense claims and salaries are left out of a run while the site has no HRMS (no
  `Expense Claim` table); erpnextswiss's query on them fails without it. They get no
  Payment Entry from this app (erpnextswiss's intermediate account still covers them).
- The Payment Entry is dated on the download day, not on the execution date.

## Tests

Offline, invented data, in the image (no site, no database):

```sh
docker run --rm -v "$PWD/finance/apps/bi_finance:/home/frappe/bi_finance_src:ro" \
    frappe-finance-custom:v16.50.0-swiss-bi9 \
    sh -c 'mkdir -p /home/frappe/logs && cd /home/frappe/bi_finance_src && ../frappe-bench/env/bin/python -m unittest -v bi_finance.test_payment_run'
```

They cover the amount rule (skonto), the Payment Entries, the currency refusal, the
submit on download, the cancel, the create button without HRMS, and a rendered file
that passes both schemas.

The IBAN and BIC rule of the `payments` step is tested offline with the other
swiss-setup tests: `python3 -m unittest discover -s finance/scripts -p 'test_*.py'`.
