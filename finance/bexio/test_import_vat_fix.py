"""Offline tests for import_vat_fix.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import unittest
from decimal import Decimal

import import_vat_fix as vf

# bexio account ids of the test chart, by account number
NUMBERS = {1: "1170", 2: "1171", 3: "1172", 4: "2200", 5: "2202", 6: "2203", 7: "2000", 8: "1100", 9: "6940",
           10: "6945", 11: "9100", 12: "1020"}
ACCOUNTS = [{"id": i, "account_no": n} for i, n in NUMBERS.items()]
GL = {n: "{} - Test - bic".format(n) for n in NUMBERS.values()}


def line(debit, credit, amount, day="2025-03-12", line_id=1, ref_class="KbInvoice", ref_id=None, ref_uuid=None):
    """A bexio journal line; debit and credit are bexio account ids of the test chart (see NUMBERS)."""
    return {"id": line_id, "ref_class": ref_class, "ref_id": ref_id, "ref_uuid": ref_uuid, "date": day + "T00:00:00+01:00",
            "debit_account_id": debit, "credit_account_id": credit,
            "amount": amount, "base_currency_amount": amount}


def lookups(sales=(), bills=(), payments=(), gl_vat=None, loaded=()):
    return vf.Lookups(
        accounts={n: GL[n] for n in NUMBERS.values()},
        sales=list(sales), bills=list(bills), payments=list(payments),
        gl_vat=gl_vat or {}, gl_check={}, loaded=loaded,
    )


def erp_vat(**by_voucher):
    """ERPNext's VAT per voucher name: {name: {account number: debit minus credit}}."""
    return {name: {n: Decimal(str(v)) for n, v in accounts.items()} for name, accounts in by_voucher.items()}


class FixLinesTest(unittest.TestCase):
    def test_a_sales_invoice_booked_on_2200_moves_to_2202(self):
        lines, balanced = vf.fix_lines({"2202": Decimal("-100")}, {"2200": Decimal("-100")})
        self.assertTrue(balanced)
        self.assertEqual(lines, {"2202": Decimal("-100"), "2200": Decimal("100")})

    def test_no_difference_is_no_correction(self):
        self.assertEqual(vf.fix_lines({"1172": Decimal("9.24")}, {"1172": Decimal("9.24")}), ({}, True))

    def test_a_difference_that_does_not_balance_is_reported(self):
        _, balanced = vf.fix_lines({"2202": Decimal("-100")}, {"2200": Decimal("-99.99")})
        self.assertFalse(balanced)


class BexioVatTest(unittest.TestCase):
    def test_document_lines_on_vat_accounts_by_key(self):
        journal = [line(8, 5, 100, ref_class="KbInvoice", ref_id=7),
                   line(8, 4, 100, ref_class="KbInvoice", ref_id=7, line_id=2),
                   line(8, 6, 50, ref_class="KbInvoice", ref_id=8, line_id=3)]
        ids = {v: k for k, v in NUMBERS.items()}
        journal[0]["credit_account_id"] = ids["2202"]
        journal[1]["credit_account_id"] = ids["2200"]
        vat = vf.bexio_vat(journal, {str(i): n for n, i in ids.items()}, "KbInvoice", "ref_id")
        self.assertEqual(vat, {"7": {"2202": Decimal("-100.00"), "2200": Decimal("-100.00")},
                               "8": {"2203": Decimal("-50.00")}})

    def test_a_bill_key_is_its_uuid(self):
        journal = [line(3, 7, 9.24, ref_class="KbBill", ref_uuid="b-1")]
        vat = vf.bexio_vat(journal, {str(i): n for i, n in NUMBERS.items()}, "KbBill", "ref_uuid")
        self.assertEqual(vat, {"b-1": {"1172": Decimal("9.24")}})


class PaymentMovesTest(unittest.TestCase):
    def test_vat_moves_are_listed_with_zero_lines_and_their_bill(self):
        numbers = {str(i): n for i, n in NUMBERS.items()}
        journal = [
            line(2, 3, 9.24, day="2021-02-15", line_id=42, ref_class="KbClientAccountEntry", ref_uuid="g-1"),
            line(2, 3, 0, day="2021-03-26", line_id=50, ref_class="KbClientAccountEntry", ref_uuid="g-2"),
            line(7, 12, 500, day="2021-03-26", line_id=51, ref_class="KbClientAccountEntry", ref_uuid="g-2"),
            line(3, 7, 9.24, day="2021-01-12", line_id=6, ref_class="KbBill", ref_uuid="g-1"),
        ]
        moves = vf.payment_moves(journal, numbers)
        self.assertEqual(moves, [(42, "2021-02-15", "1171", "1172", Decimal("9.24"), "g-1"),
                                 (50, "2021-03-26", "1171", "1172", Decimal("0.00"), "g-2")])


class PlanTest(unittest.TestCase):
    def data(self, journal):
        return {"journal": journal, "accounts": ACCOUNTS}

    def test_a_sales_invoice_is_corrected_to_2202_on_its_date(self):
        journal = [line(8, 5, 100, ref_class="KbInvoice", ref_id=7)]
        found = lookups(sales=[{"name": "SINV-1", "bexio_id": "7", "posting_date": "2025-03-12"}],
                        gl_vat=erp_vat(**{"SINV-1": {"2200": -100}}))
        documents, problems, _, _ = vf.plan(self.data(journal), found)
        self.assertEqual(problems, [])
        self.assertEqual(len(documents), 1)
        doc = documents[0]
        self.assertEqual(doc["bexio_id"], "vatfix-invoice-7")
        self.assertEqual(doc["values"]["posting_date"], "2025-03-12")
        self.assertEqual(doc["values"]["accounts"], [
            {"account": GL["2200"], "debit_in_account_currency": 100.0},
            {"account": GL["2202"], "credit_in_account_currency": 100.0}])

    def test_a_sales_rappen_goes_to_6945_and_the_vat_is_bexios(self):
        journal = [line(8, 5, 55.44, ref_class="KbInvoice", ref_id=7)]
        found = lookups(sales=[{"name": "SINV-1", "bexio_id": "7", "posting_date": "2025-03-12"}],
                        gl_vat=erp_vat(**{"SINV-1": {"2200": -55.45}}))
        documents, problems, _, _ = vf.plan(self.data(journal), found)
        self.assertEqual(problems, [])
        rows = {r["account"]: r for r in documents[0]["values"]["accounts"]}
        self.assertEqual(rows[GL["2202"]], {"account": GL["2202"], "credit_in_account_currency": 55.44})
        self.assertEqual(rows[GL["2200"]], {"account": GL["2200"], "debit_in_account_currency": 55.45})
        self.assertEqual(rows[GL["6945"]], {"account": GL["6945"], "credit_in_account_currency": 0.01})

    def test_a_reverse_charge_bill_is_corrected_on_its_bill_date(self):
        journal = [line(3, 5, 1944, ref_class="KbBill", ref_uuid="bill-1", day="2026-02-28")]
        found = lookups(bills=[{"name": "PINV-1", "bexio_id": "bill-1", "posting_date": "2026-02-28", "supplier": "S1",
                                "outstanding_amount": 0}],
                        gl_vat=erp_vat(**{"PINV-1": {"1171": 1944, "2203": -1944}}))
        documents, problems, _, _ = vf.plan(self.data(journal), found)
        self.assertEqual(problems, [])
        self.assertEqual(documents[0]["bexio_id"], "vatfix-bill-bill-1")
        amounts = {r["account"]: r.get("debit_in_account_currency", -r.get("credit_in_account_currency", 0))
                   for r in documents[0]["values"]["accounts"]}
        self.assertEqual(amounts, {GL["1171"]: -1944.0, GL["1172"]: 1944.0, GL["2202"]: -1944.0, GL["2203"]: 1944.0})

    def test_a_bill_already_corrected_gets_no_entry(self):
        journal = [line(3, 5, 1944, ref_class="KbBill", ref_uuid="bill-1", day="2026-02-28")]
        found = lookups(bills=[{"name": "PINV-1", "bexio_id": "bill-1", "posting_date": "2026-02-28", "supplier": "S1",
                                "outstanding_amount": 0}],
                        gl_vat=erp_vat(**{"PINV-1": {"1172": 1944, "2202": -1944}}))
        documents, problems, _, _ = vf.plan(self.data(journal), found)
        self.assertEqual((documents, problems), ([], []))

    def test_a_bill_open_by_a_rappen_is_closed_against_6940_with_its_reference(self):
        found = lookups(bills=[{"name": "PINV-2", "bexio_id": "bill-2", "posting_date": "2024-01-05", "supplier": "S2",
                                "outstanding_amount": 0.01}])
        documents, problems, _, _ = vf.plan(self.data([]), found)
        self.assertEqual(problems, [])
        self.assertEqual(documents[0]["bexio_id"], "rounding-bill-bill-2")
        rows = documents[0]["values"]["accounts"]
        self.assertEqual(rows[0], {"account": GL["2000"], "debit_in_account_currency": 0.01, "party_type": "Supplier",
                                   "party": "S2", "reference_type": "Purchase Invoice", "reference_name": "PINV-2"})
        self.assertEqual(rows[1], {"account": GL["6940"], "credit_in_account_currency": 0.01})

    def test_a_payment_unallocated_by_a_rappen_is_closed_against_the_supplier(self):
        found = lookups(payments=[{"name": "PAY-1", "bexio_id": "pay-1", "posting_date": "2024-01-05", "party": "S2",
                                   "unallocated_amount": 0.02}])
        documents, problems, _, _ = vf.plan(self.data([]), found)
        self.assertEqual(problems, [])
        rows = documents[0]["values"]["accounts"]
        self.assertEqual(rows[0], {"account": GL["2000"], "credit_in_account_currency": 0.02, "party_type": "Supplier",
                                   "party": "S2"})
        self.assertEqual(rows[1], {"account": GL["6940"], "debit_in_account_currency": 0.02})

    def test_a_bill_open_by_more_than_a_rappen_is_listed_not_written(self):
        found = lookups(bills=[{"name": "PINV-3", "bexio_id": "bill-3", "posting_date": "2024-01-05", "supplier": "S3",
                                "outstanding_amount": 5}])
        documents, problems, _, _ = vf.plan(self.data([]), found)
        self.assertEqual(documents, [])
        self.assertEqual(problems[0][:2], ("purchase", "bill-3"))

    def test_a_payment_move_already_loaded_is_not_written_again(self):
        journal = [line(2, 3, 9.24, day="2021-02-15", line_id=42, ref_class="KbClientAccountEntry", ref_uuid="g-1")]
        _, _, listing, _ = vf.plan(self.data(journal), lookups(loaded={"42"}))
        self.assertEqual(listing, ["journal 42 group g-1: booked by a VAT Journal Entry of import_vat_fix"])
        documents, _, _, _ = vf.plan(self.data(journal), lookups(loaded={"42"}))
        self.assertEqual(documents, [])

    def test_a_payment_move_is_one_journal_entry_on_the_payment_date(self):
        journal = [line(2, 3, 9.24, day="2021-02-15", line_id=42, ref_class="KbClientAccountEntry", ref_uuid="g-1")]
        documents, _, _, _ = vf.plan(self.data(journal), lookups())
        self.assertEqual(documents[0]["bexio_id"], "42")
        self.assertEqual(documents[0]["values"]["posting_date"], "2021-02-15")
        self.assertEqual(documents[0]["values"]["accounts"], [
            {"account": GL["1171"], "debit_in_account_currency": 9.24},
            {"account": GL["1172"], "credit_in_account_currency": 9.24}])


class SecondRunTest(unittest.TestCase):
    """A correction an earlier run submitted counts as ERPNext's VAT, so the document is not corrected again."""

    def data(self, journal):
        return {"journal": journal, "accounts": ACCOUNTS}

    def test_a_sales_invoice_already_corrected_gets_no_entry(self):
        journal = [line(8, 5, 100, ref_class="KbInvoice", ref_id=7)]
        found = vf.Lookups(
            accounts={n: GL[n] for n in NUMBERS.values()},
            sales=[{"name": "SINV-1", "bexio_id": "7", "posting_date": "2025-03-12"}], bills=[], payments=[],
            gl_vat=erp_vat(**{"SINV-1": {"2200": -100}}), gl_check={}, loaded=["vatfix-invoice-7"],
            gl_correction={"7": {"2200": Decimal("100"), "2202": Decimal("-100")}},
        )
        self.assertEqual(vf.plan(self.data(journal), found)[0], [])

    def test_a_payment_with_its_rounding_entry_loaded_gets_no_second_one(self):
        found = lookups(payments=[{"name": "PAY-1", "bexio_id": "pay-1", "posting_date": "2024-01-05", "party": "S2",
                                   "unallocated_amount": 0.02}], loaded=["rounding-payment-pay-1"])
        self.assertEqual(vf.plan(self.data([]), found)[0], [])

    def test_a_bill_with_its_rounding_entry_loaded_gets_no_second_one(self):
        found = lookups(bills=[{"name": "PINV-2", "bexio_id": "bill-2", "posting_date": "2024-01-05", "supplier": "S2",
                                "outstanding_amount": 0.01}], loaded=["rounding-bill-bill-2"])
        self.assertEqual(vf.plan(self.data([]), found)[0], [])

    def test_a_correction_key_is_the_document_it_belongs_to(self):
        self.assertEqual(vf.document_key("vatfix-invoice-7"), "7")
        self.assertEqual(vf.document_key("vatfix-credit-4"), "4")
        self.assertEqual(vf.document_key("vatfix-bill-51f12078-5dfa"), "51f12078-5dfa")
        self.assertEqual(vf.document_key("42"), "")


class TotalsTest(unittest.TestCase):
    def test_documents_and_other_lines_are_split_and_9100_is_left_out(self):
        numbers = {str(i): n for i, n in NUMBERS.items()}
        journal = [line(8, 4, 100, ref_class="KbInvoice", ref_id=1),
                   line(12, 4, 100, ref_class=None, line_id=2),
                   line(4, 11, 50, ref_class=None, line_id=3, day="2025-01-01")]
        self.assertEqual(vf.bexio_totals(journal, numbers, True)[("2025", "2200")], Decimal("-100.00"))
        other = vf.bexio_totals(journal, numbers, False)
        self.assertEqual(other[("2025", "2200")], Decimal("-100.00"))
        self.assertNotIn(("2025", "9100"), other)


if __name__ == "__main__":
    unittest.main()
