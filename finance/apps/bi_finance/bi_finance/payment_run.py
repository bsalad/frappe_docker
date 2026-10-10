"""Payment runs: erpnextswiss builds the Payment Proposal and the pain.001 file.

This module adds two things to it. A Payment Entry per paid purchase invoice,
created as a draft when the proposal is submitted and submitted when the bank
file is downloaded, so the camt line of the payment reconciles against it. And
a check of the file against SIX's Swiss schema (pain.001.001.09.ch.03), which
UBS takes, besides the generic schema erpnextswiss validates against.

Expense claims and salaries get no Payment Entry here; erpnextswiss's own
intermediate account still covers them.
"""

import os
import re

import frappe
from erpnextswiss.erpnextswiss.doctype.payment_proposal.payment_proposal import PaymentProposal as SwissPaymentProposal
from erpnextswiss.erpnextswiss.doctype.payment_proposal.payment_proposal import _create_payment_proposal_record
from erpnextswiss.erpnextswiss.xml import validate_xml_against_xsd
from frappe import _
from frappe.utils import flt, get_url_to_form, getdate

PAYMENT_REMARKS = "From Payment Proposal {0}"

# The Swiss Payment Standards schema, from erpnextswiss's own copy.
SIX_PAIN_001_XSD = "apps/erpnextswiss/erpnextswiss/public/xsd/pain.001.001.09.ch.03.xsd"

# erpnextswiss writes the company's country as stored, in lower case ('ch'); the creditor's
# is upper case. ISO 3166 codes are upper case, so the schema refuses the company's.
LOWER_COUNTRY_CODE = re.compile(r"<Ctry>([a-z]{2})</Ctry>")

# Country code, two check digits, then up to 30 letters and digits (ISO 13616).
IBAN_SHAPE = re.compile(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{1,30}")


def paid_amount(row):
    """What the run pays for one purchase invoice row.

    The same rule as erpnextswiss's on_submit: the skonto amount while the
    skonto date has not passed, else the outstanding amount.
    """
    if row.skonto_date and getdate(row.skonto_date) >= getdate():
        return flt(row.skonto_amount)
    return flt(row.amount)


def iban_is_valid(iban):
    """The ISO 7064 check: moved to the end, letters as 10 to 35, the number leaves remainder 1 mod 97.

    A file with a wrong check digit passes the schema and the bank still refuses it, so it is caught here.
    """
    iban = iban.replace(" ", "").upper()
    if not IBAN_SHAPE.fullmatch(iban):
        return False
    rearranged = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


class PaymentProposal(SwissPaymentProposal):
    def on_submit(self):
        self.check_pay_from_currency()
        super().on_submit()
        self.make_payment_entries()

    def check_pay_from_currency(self):
        if not self.purchase_invoices:
            return
        if not self.pay_from_account:
            frappe.throw(_("Pay from account is required for a payment run."))
        # A foreign-currency payment needs an exchange rate on the Payment Entry; not built yet.
        account_currency = frappe.get_cached_value("Account", self.pay_from_account, "account_currency")
        for row in self.purchase_invoices:
            if row.currency != account_currency:
                frappe.throw(_("Purchase Invoice {0} is in {1}, but {2} pays in {3}; a payment run does not cover this yet.").format(
                    row.purchase_invoice, row.currency, self.pay_from_account, account_currency))

    def make_payment_entries(self):
        for row in self.purchase_invoices:
            pinv = frappe.get_doc("Purchase Invoice", row.purchase_invoice)
            amount = paid_amount(row)
            frappe.get_doc({
                "doctype": "Payment Entry",
                "payment_type": "Pay",
                "company": self.company,
                "party_type": "Supplier",
                "party": row.supplier,
                "posting_date": getdate(self.date),
                "paid_from": self.pay_from_account,
                "paid_to": pinv.credit_to,
                "paid_amount": amount,
                "received_amount": amount,
                "reference_no": row.external_reference or pinv.name,
                "reference_date": getdate(self.date),
                "remarks": PAYMENT_REMARKS.format(self.name),
                "references": [{
                    "reference_doctype": "Purchase Invoice",
                    "reference_name": pinv.name,
                    "due_date": pinv.due_date,
                    "total_amount": pinv.grand_total,
                    "outstanding_amount": pinv.outstanding_amount,
                    "allocated_amount": amount,
                }],
            }).insert()

    def check_ibans(self):
        # Checked before erpnextswiss renders the file: a wrong IBAN is refused here with its
        # row named, not left for the bank to refuse the file.
        debtor = frappe.get_doc("Account", self.pay_from_account).iban
        if debtor and not iban_is_valid(debtor):
            frappe.throw(_("The IBAN of {0} is not valid; no payment file is made.").format(self.pay_from_account))
        for row in self.payments:
            if row.iban and not iban_is_valid(row.iban):
                frappe.throw(_("The IBAN of {0} is not valid; no payment file is made.").format(row.receiver))

    @frappe.whitelist(methods=["POST"])
    def create_bank_file(self):
        self.check_ibans()
        # validate_xml stays off in erpnextswiss's settings: its check runs before the
        # country code is fixed below, so it would refuse every file.
        result = super().create_bank_file()
        content = LOWER_COUNTRY_CODE.sub(lambda m: "<Ctry>{0}</Ctry>".format(m.group(1).upper()), result["content"])
        validated, errors = validate_xml_against_xsd(
            content, os.path.join(frappe.utils.get_bench_path(), SIX_PAIN_001_XSD))
        if not validated:
            frappe.throw(_("The payment file does not match the Swiss schema: {0}").format(errors))
        result["content"] = content
        # The file is what goes to the bank: its Payment Entries are submitted now.
        self.submit_payment_entries()
        return result

    def submit_payment_entries(self):
        for name in self.payment_entry_names(docstatus=0):
            entry = frappe.get_doc("Payment Entry", name)
            # Dated on the day of the download at the latest: ERPNext does not post into the future.
            entry.posting_date = min(getdate(entry.posting_date), getdate())
            entry.reference_date = entry.posting_date
            entry.submit()

    def payment_entry_names(self, docstatus):
        return frappe.get_all("Payment Entry", pluck="name", filters={
            "payment_type": "Pay",
            "paid_from": self.pay_from_account,
            "remarks": PAYMENT_REMARKS.format(self.name),
            "docstatus": docstatus,
        })

    def on_cancel(self):
        for name in self.payment_entry_names(docstatus=1):
            frappe.get_doc("Payment Entry", name).cancel()
        for name in self.payment_entry_names(docstatus=0):
            frappe.delete_doc("Payment Entry", name, ignore_permissions=True)
        super().on_cancel()


@frappe.whitelist(methods=["POST"])
def create_payment_proposal(date=None, company=None, currency=None):
    # The desk button of the list view. erpnextswiss always reads Expense Claim and Salary
    # Slip, which exist only with HRMS; this site has none, so the query would fail.
    frappe.only_for(("Accounts User", "Accounts Manager", "System Manager"))
    hrms = frappe.db.table_exists("Expense Claim")
    proposal = _create_payment_proposal_record(
        date=date,
        company=company,
        currency=currency,
        include_expense_claims=hrms,
        include_salary_slips=None if hrms else False,
    )
    if not proposal:
        return None
    return get_url_to_form("Payment Proposal", proposal.name)
