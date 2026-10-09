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
