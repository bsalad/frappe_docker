"""Offline tests for payment_run.py: Payment Entries per invoice, the bank file gate, the submit on download.

Invented data only (invented suppliers, bills and bank details; a SIX sample QR-IBAN and
QR reference), no database, no network. The module imports frappe and erpnextswiss, so run
it in the image (finance/docs/payment-runs.md):

    docker run --rm -v "$PWD/finance/apps/bi_finance:/home/frappe/bi_finance_src:ro" \
        frappe-finance-custom:v16.50.0-swiss-bi6 \
        sh -c 'cd /home/frappe/bi_finance_src && ../frappe-bench/env/bin/python -m unittest -v bi_finance.test_payment_run'
"""

import datetime
import unittest
from unittest import mock

import frappe
import jinja2
from erpnextswiss.erpnextswiss.doctype.payment_proposal import payment_proposal as swiss

from bi_finance import payment_run
from bi_finance.payment_run import PaymentProposal, create_payment_proposal, iban_is_valid, paid_amount

def setUpModule():
    # Dates and messages read the site's settings and logs; there is no site here.
    patcher = mock.patch.object(frappe, "get_system_settings", return_value=None)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    # Every message is wrapped in _() before frappe.throw sees it, and _() loads the site's translations.
    # With no site they are empty, so the message stays as written.
    patcher = mock.patch("frappe.translate.get_all_translations", return_value={})
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


FAR_FUTURE = datetime.date(2099, 1, 1)
LONG_AGO = datetime.date(2000, 1, 1)
TODAY = datetime.date.today()
APPS_ROOT = "/home/frappe/frappe-bench/apps/erpnextswiss"
PAY_FROM = "1020 - UBS Kontokorrent - Test"
COMPANY = "Test BI Concepts AG"


def invoice(name, supplier, amount, currency="CHF", skonto_date=None, skonto_amount=None, external="RE-1"):
    return frappe._dict(
        supplier=supplier, purchase_invoice=name, amount=amount, currency=currency,
        due_date=datetime.date(2026, 11, 5), skonto_date=skonto_date,
        skonto_amount=amount if skonto_amount is None else skonto_amount,
        external_reference=external, payment_type="IBAN", esr_reference=None, esr_participation_number=None,
    )


def pinv(name, credit_to="2000 - Kreditoren - Test", currency="CHF", grand_total=1000.0, outstanding=1000.0):
    return frappe._dict(
        name=name, credit_to=credit_to, currency=currency, grand_total=grand_total,
        outstanding_amount=outstanding, due_date=datetime.date(2026, 11, 5), bill_no=None,
    )


def proposal(purchase_invoices, date=datetime.date(2026, 11, 5)):
    # Set the fields directly: building a Document reads its DocType from the database.
    doc = PaymentProposal.__new__(PaymentProposal)
    doc.__dict__.update(
        doctype="Payment Proposal", name="PP-TEST-1", title="Test run", company=COMPANY,
        pay_from_account=PAY_FROM, date=date, purchase_invoices=purchase_invoices, payments=[],
    )
    return doc


class PaidAmount(unittest.TestCase):
    def test_skonto_amount_while_the_skonto_date_holds(self):
        row = invoice("PINV-1", "Lieferant Alpha AG", 1000.0, skonto_date=FAR_FUTURE, skonto_amount=980.0)
        self.assertEqual(paid_amount(row), 980.0)

    def test_outstanding_amount_once_the_skonto_date_has_passed(self):
        row = invoice("PINV-1", "Lieferant Alpha AG", 1000.0, skonto_date=LONG_AGO, skonto_amount=980.0)
        self.assertEqual(paid_amount(row), 1000.0)

    def test_outstanding_amount_without_a_skonto_date(self):
        self.assertEqual(paid_amount(invoice("PINV-1", "Lieferant Alpha AG", 1000.0)), 1000.0)


class PaymentEntries(unittest.TestCase):
    def setUp(self):
        self.created = []
        self.invoices = {"PINV-1": pinv("PINV-1"), "PINV-2": pinv("PINV-2", credit_to="2001 - Kreditoren Ausland - Test", grand_total=500.0, outstanding=500.0)}

        def get_doc(*args, **kwargs):
            if args and args[0] == "Purchase Invoice":
                return self.invoices[args[1]]
            if args and isinstance(args[0], dict):
                entry = mock.MagicMock()
                entry.insert.return_value = entry
                self.created.append(args[0])
                return entry
            raise AssertionError(f"unexpected get_doc {args}")

        patcher = mock.patch.object(frappe, "get_doc", side_effect=get_doc)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(frappe, "get_cached_value", return_value="CHF")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_draft_per_paid_invoice_with_the_bank_as_paid_from(self):
        doc = proposal([
            invoice("PINV-1", "Lieferant Alpha AG", 1000.0, skonto_date=FAR_FUTURE, skonto_amount=980.0, external="RE-17"),
            invoice("PINV-2", "Lieferant Beta GmbH", 500.0, external="RE-18"),
        ])
        doc.make_payment_entries()
        self.assertEqual(len(self.created), 2)
        first, second = self.created
        self.assertEqual(first["payment_type"], "Pay")
        self.assertEqual(first["party_type"], "Supplier")
        self.assertEqual(first["party"], "Lieferant Alpha AG")
        self.assertEqual(first["paid_from"], PAY_FROM)
        self.assertEqual(first["paid_to"], "2000 - Kreditoren - Test")
        self.assertEqual(first["paid_amount"], 980.0)
        self.assertEqual(first["reference_no"], "RE-17")
        self.assertEqual(first["remarks"], "From Payment Proposal PP-TEST-1")
        self.assertEqual(first["references"][0]["reference_name"], "PINV-1")
        self.assertEqual(first["references"][0]["allocated_amount"], 980.0)
        self.assertEqual(second["paid_to"], "2001 - Kreditoren Ausland - Test")
        self.assertEqual(second["references"][0]["allocated_amount"], 500.0)

    def test_a_foreign_currency_invoice_stops_the_run(self):
        doc = proposal([invoice("PINV-3", "Lieferant Gamma SA", 100.0, currency="EUR")])
        with mock.patch.object(frappe, "throw", side_effect=RuntimeError("stopped")) as throw:
            with self.assertRaises(RuntimeError):
                doc.check_pay_from_currency()
        throw.assert_called_once()


class SubmitOnDownload(unittest.TestCase):
    def test_drafts_are_submitted_dated_no_later_than_today(self):
        doc = proposal([invoice("PINV-1", "Lieferant Alpha AG", 1000.0)], date=FAR_FUTURE)
        entry = mock.MagicMock(posting_date=FAR_FUTURE)
        with mock.patch.object(PaymentProposal, "payment_entry_names", return_value=["ACC-PAY-1"]), \
                mock.patch.object(frappe, "get_doc", return_value=entry):
            doc.submit_payment_entries()
        entry.submit.assert_called_once()
        self.assertEqual(entry.posting_date, TODAY)
        self.assertEqual(entry.reference_date, TODAY)

    def test_a_failed_file_check_submits_nothing(self):
        doc = proposal([invoice("PINV-1", "Lieferant Alpha AG", 1000.0)])
        bad = {"content": "<Document/>", "file_name": "payments_x.xml", "message_id": "x"}
        with mock.patch.object(swiss.PaymentProposal, "create_bank_file", return_value=bad), \
                mock.patch.object(PaymentProposal, "check_ibans"), \
                mock.patch.object(PaymentProposal, "submit_payment_entries") as submit, \
                mock.patch.object(frappe, "throw", side_effect=RuntimeError("schema")):
            with self.assertRaises(RuntimeError):
                doc.create_bank_file()
        submit.assert_not_called()

    def test_cancel_cancels_submitted_entries_and_deletes_drafts(self):
        doc = proposal([])
        names = {1: ["ACC-PAY-1"], 0: ["ACC-PAY-2"]}
        with mock.patch.object(PaymentProposal, "payment_entry_names", side_effect=lambda docstatus: names[docstatus]), \
                mock.patch.object(frappe, "get_doc") as get_doc, \
                mock.patch.object(frappe, "delete_doc") as delete_doc, \
                mock.patch.object(swiss.PaymentProposal, "on_cancel") as super_cancel:
            doc.on_cancel()
        get_doc.return_value.cancel.assert_called_once()
        delete_doc.assert_called_once_with("Payment Entry", "ACC-PAY-2", ignore_permissions=True)
        super_cancel.assert_called_once()


def render(path, data):
    # frappe's Jinja loader reads the site config; the template is plain Jinja, so it is rendered directly.
    loader = jinja2.FileSystemLoader(APPS_ROOT)
    return jinja2.Environment(loader=loader).get_template(path).render(**data)


class DeskCreate(unittest.TestCase):
    def run_create(self, hrms):
        record = mock.MagicMock(name="PP-TEST-2")
        with mock.patch.object(frappe, "db", mock.MagicMock(table_exists=mock.Mock(return_value=hrms))), \
                mock.patch.object(frappe, "only_for"), \
                mock.patch.object(payment_run, "_create_payment_proposal_record", return_value=record) as create, \
                mock.patch.object(payment_run, "get_url_to_form", return_value="/desk#Form/Payment Proposal/PP-TEST-2"):
            url = create_payment_proposal(date="2026-11-05", company=COMPANY)
        return url, create

    def test_without_hrms_expense_claims_and_salary_slips_are_left_out(self):
        url, create = self.run_create(hrms=False)
        self.assertEqual(create.call_args.kwargs["include_expense_claims"], False)
        self.assertEqual(create.call_args.kwargs["include_salary_slips"], False)
        self.assertEqual(url, "/desk#Form/Payment Proposal/PP-TEST-2")

    def test_with_hrms_the_erpnextswiss_defaults_apply(self):
        _, create = self.run_create(hrms=True)
        self.assertEqual(create.call_args.kwargs["include_expense_claims"], True)
        self.assertIsNone(create.call_args.kwargs["include_salary_slips"])


class IbanCheck(unittest.TestCase):
    def test_an_invented_iban_with_right_check_digits_passes(self):
        self.assertTrue(iban_is_valid("CH93 0076 2011 6238 5295 7"))
        self.assertTrue(iban_is_valid("CH52 0000 0000 0123 4567 8"))

    def test_a_wrong_check_digit_fails(self):
        self.assertFalse(iban_is_valid("CH21 0900 0000 2500 9779 8"))

    def test_a_malformed_string_fails_without_raising(self):
        self.assertFalse(iban_is_valid("CH93-0076-2011-6238-5295-7"))
        self.assertFalse(iban_is_valid(""))

    def test_a_bad_debtor_iban_stops_the_file_before_it_is_rendered(self):
        doc = proposal([])
        doc.payments = [frappe._dict(receiver="Lieferant Alpha AG", iban="CH52 0000 0000 0123 4567 8")]
        with mock.patch.object(frappe, "get_doc", return_value=frappe._dict(iban="CH21 0900 0000 2500 9779 8")), \
                mock.patch.object(frappe, "throw", side_effect=RuntimeError("iban")) as throw, \
                mock.patch.object(swiss.PaymentProposal, "create_bank_file") as render:
            with self.assertRaises(RuntimeError):
                doc.create_bank_file()
        throw.assert_called_once()
        render.assert_not_called()

    def test_a_bad_creditor_iban_stops_the_file_and_names_the_row(self):
        doc = proposal([])
        doc.payments = [frappe._dict(receiver="Lieferant Beta GmbH", iban="CH21 0900 0000 2500 9779 8")]
        with mock.patch.object(frappe, "get_doc", return_value=frappe._dict(iban="CH93 0076 2011 6238 5295 7")), \
                mock.patch.object(frappe, "throw", side_effect=RuntimeError("iban")) as throw, \
                mock.patch.object(swiss.PaymentProposal, "create_bank_file") as render:
            with self.assertRaises(RuntimeError):
                doc.create_bank_file()
        self.assertIn("Lieferant Beta GmbH", throw.call_args.args[0])
        render.assert_not_called()


class PainFile(unittest.TestCase):
    """The file erpnextswiss renders from invented rows passes both schemas: the generic pain.001.001.09 and SIX's ch.03."""

    def proposal_with_rows(self):
        doc = proposal([])
        doc.payments = [
            frappe._dict(
                receiver="Lieferant Alpha AG", receiver_id="SUP-A", iban="CH52 0000 0000 0123 4567 8", bic="TESTCHZZ",
                payment_type="IBAN", receiver_address_line1="Musterweg 7", receiver_address_line2="8000 Zürich",
                receiver_pincode="8000", receiver_city="Zürich", receiver_country="Switzerland", amount=980.0,
                currency="CHF", reference="RE-17", execution_date=datetime.date(2026, 11, 5), esr_reference=None,
                esr_participation_number=None, is_salary=0, idx=1,
            ),
            frappe._dict(
                receiver="Lieferant Beta GmbH", receiver_id="SUP-B", iban="CH98 3000 5248 2100 1701 C",
                bic="", payment_type="ESR", receiver_address_line1="Teststrasse 15a", receiver_address_line2="3000 Bern",
                receiver_pincode="3000", receiver_city="Bern", receiver_country="Switzerland", amount=500.0,
                currency="CHF", reference="RE-18", execution_date=datetime.date(2026, 11, 5),
                esr_reference="00 17010 00000 00000 00012 02132", esr_participation_number="CH98 3000 5248 2100 1701 C",
                is_salary=0, idx=2,
            ),
        ]
        return doc

    def test_rendered_file_passes_both_schemas(self):
        doc = self.proposal_with_rows()
        settings = frappe._dict(xml_version="09", banking_region="CH", validate_xml=0, use_unidecode=1)
        account = frappe._dict(iban="CH93 0076 2011 6238 5295 7", bic="TESTCHZZ")
        address = frappe._dict(address_line1="Musterweg 1", pincode="8000", city="Zürich", country_code="ch")

        def get_doc(*args, **kwargs):
            if args[0] == "ERPNextSwiss Settings":
                return settings
            if args[0] == "Account":
                return account
            raise AssertionError(f"unexpected get_doc {args}")

        doc.check_permission = mock.MagicMock()
        with mock.patch.object(frappe, "get_doc", side_effect=get_doc), \
                mock.patch.object(frappe, "get_value", return_value="CH"), \
                mock.patch.object(frappe, "render_template", side_effect=render), \
                mock.patch.object(swiss, "get_primary_address", return_value=address), \
                mock.patch.object(PaymentProposal, "submit_payment_entries") as submit:
            content = PaymentProposal.create_bank_file(doc)["content"]
            submit.assert_called_once()

        self.assertIn("<Ctry>CH</Ctry>", content)
        self.assertNotIn("<Ctry>ch</Ctry>", content)
        for xsd in (
            "apps/erpnextswiss/erpnextswiss/public/xsd/pain.001.001.09.xsd",
            payment_run.SIX_PAIN_001_XSD,
        ):
            validated, errors = swiss.validate_xml_against_xsd(content, "/home/frappe/frappe-bench/" + xsd)
            self.assertTrue(validated, f"{xsd}: {errors}")


if __name__ == "__main__":
    unittest.main()
