app_name = "bi_finance"
app_title = "BI Finance"
app_publisher = "BI Concepts"
app_description = "Swiss QR-bill for Sales Invoice, drawn on our own server"
app_license = "Proprietary"
required_apps = ["erpnext", "erpnextswiss"]

# Payment runs: Payment Entries stay drafts until the bank file is downloaded (payment_run.py).
override_doctype_class = {
    "Payment Proposal": "bi_finance.payment_run.PaymentProposal",
}
override_whitelisted_methods = {
    "erpnextswiss.erpnextswiss.doctype.payment_proposal.payment_proposal.create_payment_proposal": "bi_finance.payment_run.create_payment_proposal",
}

# Jinja print formats cannot import Python, and the PDF converter cannot fetch the
# site's own URLs, so the QR image is a method the print format calls and embeds.
jinja = {
    "methods": [
        "bi_finance.qrbill.sales_invoice_qr_uri",
        "bi_finance.qrbill.sales_invoice_iban",
        "bi_finance.qrbill.sales_invoice_slip_spacer",
    ]
}
