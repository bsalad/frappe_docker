"""Offline tests for import_payments_out.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import io
import unittest
from decimal import Decimal

import import_master as im
import import_payments_out as po
from test_import_master import FakeErp

# bexio account ids of the test chart, by account number
NUMBERS = {10: "2000", 11: "1020", 12: "1021", 13: "1170", 14: "1171", 15: "1172", 16: "4400", 17: "4906",
           18: "2202", 19: "2203"}


def line(debit, credit, amount, uuid="g-1", day="2025-03-12", line_id=1, ref_class="KbClientAccountEntry"):
    """A journal line; debit and credit are account numbers of the test chart."""
    ids = {v: k for k, v in NUMBERS.items()}
    return {"id": line_id, "ref_class": ref_class, "ref_uuid": uuid, "ref_id": 1, "date": day + "T00:00:00+01:00",
            "debit_account_id": ids[debit], "credit_account_id": ids[credit],
            "amount": amount, "base_currency_amount": amount}


def lookups(invoices=None, gl=None, cost_center="Main - Test"):
    return po.Lookups(
        invoices=dict(invoices if invoices is not None else {"b-1": invoice()}),
        gl=dict(gl if gl is not None else {"1020": "1020 - UBS Test - bic", "4400": "4400 - Einkauf - bic",
                                           "2000": "2000 - Kreditoren - bic"}),
        cost_center=cost_center,
    )


def invoice(**extra):
    return dict({"name": "ACC-PINV-0001", "bexio_id": "b-1", "supplier": "Lieferant Test AG",
                 "credit_to": "2000 - Kreditoren - bic", "currency": "CHF", "grand_total": 402.85,
                 "outstanding_amount": 402.85}, **extra)


class SettleTest(unittest.TestCase):
    def test_a_plain_payment_settles_the_bill_from_the_bank(self):
        item = po.settle("g-1", [line("2000", "1020", 402.85)], NUMBERS)
        self.assertEqual((item["payable"], item["bank"], item["bank_account"], item["day"]),
                         (Decimal("402.85"), Decimal("402.85"), "1020", "2025-03-12"))
        self.assertEqual((item["deductions"], item["vat"]), ([], []))

    def test_the_vat_move_is_listed_and_does_not_change_the_payment(self):
        item = po.settle("g-1", [line("2000", "1020", 110.00), line("1171", "1172", 10.00, line_id=2)], NUMBERS)
        self.assertEqual((item["payable"], item["bank"]), (Decimal("110.00"), Decimal("110.00")))
        self.assertEqual([v["id"] for v in item["vat"]], [2])

    def test_a_deduction_is_the_difference_between_the_bill_and_the_bank(self):
        item = po.settle("g-1", [line("2000", "1020", 97.00), line("2000", "4400", 3.00, line_id=2)], NUMBERS)
        self.assertEqual((item["payable"], item["bank"]), (Decimal("100.00"), Decimal("97.00")))
        self.assertEqual(item["deductions"], [("4400", Decimal("3.00"))])

    def test_a_line_without_a_home_is_unmapped(self):
        with self.assertRaisesRegex(po.Unmapped, "no home"):
            po.settle("g-1", [line("2000", "1020", 5.00), line("4906", "1020", 5.00, line_id=2)], NUMBERS)

    def test_a_payment_without_a_bank_line_is_unmapped(self):
        with self.assertRaisesRegex(po.Unmapped, "no credit on a bank account"):
            po.settle("g-1", [line("2000", "4400", 5.00)], NUMBERS)

    def test_the_reverse_charge_vat_move_is_listed_too(self):
        item = po.settle("g-1", [line("2000", "1020", 500.00), line("2202", "2203", 38.46, line_id=2)], NUMBERS)
        self.assertEqual([v["id"] for v in item["vat"]], [2])
        self.assertEqual(item["payable"], Decimal("500.00"))

    def test_lines_of_one_payment_on_two_days_are_unmapped(self):
        with self.assertRaisesRegex(po.Unmapped, "different days"):
            po.settle("g-1", [line("2000", "1020", 5.00), line("2000", "4400", 0.00, day="2025-03-13", line_id=2)], NUMBERS)

    def test_a_zero_line_moves_nothing(self):
        item = po.settle("g-1", [line("2000", "1020", 5.00), line("2000", "4400", 0.00, line_id=2)], NUMBERS)
        self.assertEqual(item["deductions"], [])


class PairTest(unittest.TestCase):
    def settled(self, uuid, amount, day):
        return {"uuid": uuid, "payable": Decimal(amount), "day": day}

    def test_payments_and_bills_of_one_amount_pair_in_date_order(self):
        settled = [self.settled("g-2", "50.00", "2025-06-01"), self.settled("g-1", "50.00", "2025-02-01")]
        booked = {"b-1": Decimal("50.00"), "b-2": Decimal("50.00")}
        pairs, unpaired, open_bills = po.pair(settled, booked, {"b-1": "2025-01-10", "b-2": "2025-05-10"})
        self.assertEqual([(item["uuid"], bill) for item, bill in pairs], [("g-1", "b-1"), ("g-2", "b-2")])
        self.assertEqual((unpaired, open_bills), ([], []))

    def test_a_payment_without_a_bill_of_its_amount_is_unpaired_and_a_bill_without_one_is_open(self):
        settled = [self.settled("g-1", "50.00", "2025-02-01"), self.settled("g-2", "70.00", "2025-02-02")]
        booked = {"b-1": Decimal("50.00"), "b-2": Decimal("30.00")}
        pairs, unpaired, open_bills = po.pair(settled, booked, {"b-1": "2025-01-10", "b-2": "2025-01-11"})
        self.assertEqual([(item["uuid"], bill) for item, bill in pairs], [("g-1", "b-1")])
        self.assertEqual([item["uuid"] for item in unpaired], ["g-2"])
        self.assertEqual(open_bills, ["b-2"])


class PaymentEntryTest(unittest.TestCase):
    def item(self, **extra):
        return dict({"uuid": "g-1", "day": "2025-03-12", "payable": Decimal("402.85"), "bank": Decimal("402.85"),
                     "bank_account": "1020", "deductions": [], "vat": []}, **extra)

    def test_a_payment_entry_pays_the_purchase_invoice_from_the_bank(self):
        doc = po.payment_entry(self.item(), "b-1", lookups())
        self.assertEqual(doc["doctype"], "Payment Entry")
        self.assertEqual((doc["payment_type"], doc["party_type"], doc["party"]), ("Pay", "Supplier", "Lieferant Test AG"))
        self.assertEqual((doc["paid_from"], doc["paid_to"]), ("1020 - UBS Test - bic", "2000 - Kreditoren - bic"))
        self.assertEqual((doc["paid_amount"], doc["received_amount"]), (402.85, 402.85))
        self.assertEqual(doc["references"], [{"reference_doctype": "Purchase Invoice", "reference_name": "ACC-PINV-0001",
                                              "allocated_amount": 402.85, "total_amount": 402.85, "outstanding_amount": 402.85}])
        self.assertEqual((doc["posting_date"], doc["bexio_id"]), ("2025-03-12", "g-1"))
        self.assertNotIn("deductions", doc)

    def test_a_rounding_gap_is_left_unallocated_and_the_bank_gets_bexios_amount(self):
        item = self.item(payable=Decimal("703.11"), bank=Decimal("703.11"))
        doc = po.payment_entry(item, "b-1", lookups(invoices={"b-1": invoice(grand_total=703.11, outstanding_amount=703.10)}))
        self.assertEqual((doc["paid_amount"], doc["references"][0]["allocated_amount"]), (703.11, 703.10))
        self.assertEqual(doc["unallocated_amount"], 0.01)

    def test_a_gap_beyond_the_rounding_is_unmapped(self):
        item = self.item(payable=Decimal("703.11"), bank=Decimal("703.11"))
        with self.assertRaisesRegex(po.Unmapped, "exceeds the outstanding"):
            po.payment_entry(item, "b-1", lookups(invoices={"b-1": invoice(grand_total=703.11, outstanding_amount=703.00)}))

    def test_a_rounding_gap_with_a_deduction_is_unmapped(self):
        item = self.item(payable=Decimal("100.00"), bank=Decimal("97.00"), deductions=[("4400", Decimal("3.00"))])
        with self.assertRaisesRegex(po.Unmapped, "exceeds the outstanding"):
            po.payment_entry(item, "b-1", lookups(invoices={"b-1": invoice(grand_total=100.00, outstanding_amount=99.99)}))

    def test_a_deduction_goes_on_its_account_with_the_cost_center(self):
        item = self.item(payable=Decimal("100.00"), bank=Decimal("97.00"), deductions=[("4400", Decimal("3.00"))])
        doc = po.payment_entry(item, "b-1", lookups(invoices={"b-1": invoice(grand_total=100.00, outstanding_amount=100.00)}))
        self.assertEqual(doc["deductions"], [{"account": "4400 - Einkauf - bic", "cost_center": "Main - Test", "amount": -3.0}])
        self.assertEqual((doc["paid_amount"], doc["references"][0]["allocated_amount"]), (97.0, 100.0))

    def test_a_deduction_without_one_cost_center_is_unmapped(self):
        item = self.item(deductions=[("4400", Decimal("3.00"))])
        with self.assertRaisesRegex(po.Unmapped, "Cost Center"):
            po.payment_entry(item, "b-1", lookups(cost_center=None))

    def test_a_bill_without_a_submitted_purchase_invoice_is_unmapped(self):
        with self.assertRaisesRegex(po.Unmapped, "no submitted Purchase Invoice"):
            po.payment_entry(self.item(), "b-9", lookups())

    def test_a_purchase_invoice_in_another_currency_is_unmapped(self):
        with self.assertRaisesRegex(po.Unmapped, "currency other than CHF"):
            po.payment_entry(self.item(), "b-1", lookups(invoices={"b-1": invoice(currency="EUR")}))

    def test_a_purchase_invoice_whose_total_is_not_bexios_booking_is_unmapped(self):
        with self.assertRaisesRegex(po.Unmapped, "differs from bexio's booking"):
            po.payment_entry(self.item(), "b-1", lookups(invoices={"b-1": invoice(grand_total=400.00)}))

    def test_a_payment_above_what_the_invoice_still_owes_is_unmapped(self):
        with self.assertRaisesRegex(po.Unmapped, "exceeds the outstanding"):
            po.payment_entry(self.item(), "b-1", lookups(invoices={"b-1": invoice(outstanding_amount=0)}))


class PlanTest(unittest.TestCase):
    def data(self):
        journal = [
            line("2000", "1020", 402.85, uuid="g-1", day="2025-03-12", line_id=1),
            line("1171", "1172", 10.00, uuid="g-1", day="2025-03-12", line_id=2),
            line("2000", "1020", 100.00, uuid="g-2", day="2025-04-02", line_id=3),
            line("2000", "1020", 500.00, uuid="g-3", day="2025-05-02", line_id=4),
            # bexio's booking of the bills, on the payables account, by bill uuid
            line("1171", "1172", 10.00, uuid="b-1", day="2025-01-10", line_id=5, ref_class="KbBill"),
            line("1172", "2000", 402.85, uuid="b-1", day="2025-01-10", line_id=6, ref_class="KbBill"),
            line("1172", "2000", 100.00, uuid="b-2", day="2025-02-10", line_id=7, ref_class="KbBill"),
        ]
        accounts = [{"id": k, "account_no": v} for k, v in NUMBERS.items()]
        bills = [{"id": "b-1", "bill_date": "2025-01-10"}, {"id": "b-2", "bill_date": "2025-02-10"}]
        return {"journal": journal, "accounts": accounts, "bills": bills}

    def test_each_payment_is_paired_with_its_bill_and_the_rest_is_listed(self):
        erp_lookups = lookups(invoices={"b-1": invoice(), "b-2": invoice(name="ACC-PINV-0002", bexio_id="b-2",
                                                                         grand_total=100.00, outstanding_amount=100.00)})
        results, open_bills = po.plan(self.data(), erp_lookups)
        mapped = {r["uuid"]: r["bill"] for r in results if r.get("doc")}
        self.assertEqual(mapped, {"g-1": "b-1", "g-2": "b-2"})
        self.assertEqual([r["uuid"] for r in results if r["error"]], ["g-3"])
        self.assertEqual(open_bills, [])
        self.assertEqual([v["id"] for r in results for v in r["vat"]], [2])

    def test_the_payment_with_no_bill_of_its_amount_is_unmapped_with_that_reason(self):
        results, _ = po.plan(self.data(), lookups())
        reasons = {r["uuid"]: r["error"] for r in results if r["error"]}
        self.assertEqual(reasons, {"g-2": "the bill has no submitted Purchase Invoice in ERPNext",
                                   "g-3": "no bill of this amount in bexio's journal"})

    def test_a_payment_an_earlier_run_loaded_is_not_mapped_again(self):
        erp_lookups = lookups(invoices={"b-1": invoice(outstanding_amount=0)})
        erp_lookups.loaded = {"g-1"}
        results, _ = po.plan(self.data(), erp_lookups)
        loaded = next(r for r in results if r["uuid"] == "g-1")
        self.assertEqual((loaded["loaded"], loaded["doc"], loaded["error"]), (True, None, None))
        self.assertIn("already submitted in ERPNext by an earlier run, not handed over again: 1", po.summary(results, []))

    def test_a_bill_with_no_payment_is_open(self):
        data = self.data()
        data["journal"].append(line("1172", "2000", 77.00, uuid="b-3", line_id=9, ref_class="KbBill"))
        data["bills"].append({"id": "b-3", "bill_date": "2025-03-01"})
        _, open_bills = po.plan(data, lookups())
        self.assertEqual(open_bills, ["b-3"])

    def test_summary_gives_totals_and_no_bexio_ids(self):
        results, open_bills = po.plan(self.data(), lookups())
        text = po.summary(results, open_bills)
        self.assertIn("2025", text)
        self.assertIn("VAT lines moved on payment, not booked here", text)
        self.assertNotIn("g-1", text)
        self.assertNotIn("b-1", text)


class LookupsTest(unittest.TestCase):
    def test_from_erp_keys_the_submitted_invoices_by_bexio_id_and_takes_the_accounts_by_number(self):
        erp = FakeErp({
            "Purchase Invoice": [{"name": "ACC-PINV-0001", "bexio_id": "b-1", "supplier": "Lieferant Test AG",
                                  "credit_to": "2000 - Kreditoren - bic", "currency": "CHF", "grand_total": 402.85,
                                  "outstanding_amount": 402.85, "docstatus": 1, "company": im.COMPANY}],
            "Account": [{"name": "1020 - UBS Test - bic", "account_number": "1020", "is_group": 0, "company": im.COMPANY}],
            "Cost Center": [{"name": "Main - Test", "is_group": 0, "company": im.COMPANY}],
        })
        found = po.Lookups.from_erp(erp)
        self.assertEqual(found.invoices["b-1"]["name"], "ACC-PINV-0001")
        self.assertEqual(found.gl, {"1020": "1020 - UBS Test - bic"})
        self.assertEqual(found.cost_center, "Main - Test")


class MainTest(unittest.TestCase):
    def test_main_takes_exactly_one_of_the_dry_run_and_the_write(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            po.main([])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            po.main(["--dry-run", "--write", "x.json"])


if __name__ == "__main__":
    unittest.main()
