"""Offline tests for import_bank.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

import import_bank as ib
import import_master as im

# the lookups the mapping reads; the ids and names are invented
LOOKUPS = {
    "currency": {"1": "CHF", "2": "EUR"},
    "bank_account": {
        "11": {"name": "Hauptkonto - Testbank AG", "currency": "CHF", "account": "1020 - Testbank - X"},
        "12": {"name": "Fremdwaehrung - Zweitbank AG", "currency": "EUR", "account": "1021 - Zweitbank - X"},
    },
}

# the export's shape: amount unsigned, type gives the direction, status says whether bexio booked it
CHF_IN = {"id": 9001, "bank_account_id": 11, "currency_id": 1, "value_date": "2026-03-31", "book_date": "2026-03-31",
          "amount": 1500.0, "type": "CREDIT", "status": "reconciled",
          "description": "PMNT.RCDT.VCOM", "title": "Zahlung Rechnung 1001"}
CHF_OUT = {"id": 9002, "bank_account_id": 11, "currency_id": 1, "value_date": "2026-04-02", "book_date": "2026-04-02",
           "amount": 250.5, "type": "DEBIT", "status": "unreconciled",
           "description": "PMNT.ICDT.DMCT", "title": "Miete"}
EUR_OUT = {"id": 9003, "bank_account_id": 12, "currency_id": 2, "value_date": "2025-12-31", "book_date": "2025-12-31",
           "amount": 99.99, "type": "DEBIT", "status": "auto_reconciled",
           "description": "PMNT.ICDT.AUTT", "title": "Software"}


def record(base, **changes):
    """A copy of a base record with some fields changed."""
    doc = copy.deepcopy(base)
    doc.update(changes)
    return doc


class FakeErp:
    """The reads the importer makes: the Bank Accounts that have a bexio_id, the GL rows and the vouchers given to it."""

    def __init__(self, gl=None, vouchers=None):
        self.gl = gl or []
        self.vouchers = vouchers or {}

    def list(self, doctype, filters=None, fields=("name",)):
        if doctype == "Bank Account":
            return [{"name": "Hauptkonto - Testbank AG", "bexio_id": "11", "account": "1020 - Testbank - X"}]
        if doctype == "GL Entry":
            return self.gl
        return self.vouchers.get(doctype, [])


class MappingTest(unittest.TestCase):
    def test_money_in_is_a_deposit(self):
        doc = ib.bank_transaction(CHF_IN, LOOKUPS)
        self.assertEqual(doc["doctype"], "Bank Transaction")
        self.assertEqual(doc["company"], im.COMPANY)
        self.assertEqual((doc["bexio_id"], doc["date"]), ("9001", "2026-03-31"))
        self.assertEqual(doc["bank_account"], "Hauptkonto - Testbank AG")
        self.assertEqual((doc["currency"], doc["deposit"], doc["withdrawal"]), ("CHF", 1500.0, 0.0))
        self.assertEqual((doc["description"], doc["reference_number"]), ("Zahlung Rechnung 1001", ""))

    def test_money_out_is_a_withdrawal(self):
        doc = ib.bank_transaction(CHF_OUT, LOOKUPS)
        self.assertEqual((doc["deposit"], doc["withdrawal"]), (0.0, 250.5))

    def test_foreign_currency_account_keeps_its_currency(self):
        doc = ib.bank_transaction(EUR_OUT, LOOKUPS)
        self.assertEqual(doc["bank_account"], "Fremdwaehrung - Zweitbank AG")
        self.assertEqual((doc["currency"], doc["deposit"], doc["withdrawal"]), ("EUR", 0.0, 99.99))

    def test_transaction_in_another_currency_than_its_account_is_unmapped(self):
        with self.assertRaisesRegex(ib.Unmapped, "differs from its bank account's CHF"):
            ib.bank_transaction(record(CHF_IN, currency_id=2), LOOKUPS)

    def test_bank_account_without_a_bexio_match_is_unmapped_by_id(self):
        with self.assertRaisesRegex(ib.Unmapped, "bank account 99 has no Bank Account"):
            ib.bank_transaction(record(CHF_IN, bank_account_id=99), LOOKUPS)

    def test_zero_amount_missing_date_and_unknown_type_are_unmapped(self):
        with self.assertRaisesRegex(ib.Unmapped, "zero amount"):
            ib.bank_transaction(record(CHF_IN, amount=0), LOOKUPS)
        with self.assertRaisesRegex(ib.Unmapped, "no value date"):
            ib.bank_transaction(record(CHF_IN, value_date=None), LOOKUPS)
        with self.assertRaisesRegex(ib.Unmapped, "neither CREDIT nor DEBIT"):
            ib.bank_transaction(record(CHF_IN, type="TRANSFER"), LOOKUPS)

    def test_the_booking_link_is_not_mapped(self):
        # the export has no field that names the booking; the posting plan's rule (D7) decides where it goes
        doc = ib.bank_transaction(record(CHF_IN, booked_with="kb_invoice:1001"), LOOKUPS)
        self.assertNotIn("bexio_booked_with", doc)
        self.assertNotIn("booked_with", doc)


class PlanTest(unittest.TestCase):
    def test_unmapped_records_are_reported_not_raised(self):
        results = ib.plan([CHF_IN, record(CHF_IN, id=9009, bank_account_id=99)], LOOKUPS)
        self.assertEqual([r["error"] is None for r in results], [True, False])
        self.assertEqual(results[1]["bexio_id"], "9009")

    def test_booked_follows_bexio_status(self):
        results = ib.plan([CHF_IN, CHF_OUT, EUR_OUT, record(CHF_IN, id=9010, status="ignored")], LOOKUPS)
        self.assertEqual([r["booked"] for r in results], [True, False, True, False])

    def test_fields_the_doctype_lacks_are_named(self):
        meta = {"doctype", "company", "bexio_id", "date", "bank_account", "currency", "deposit", "withdrawal",
                "description", "reference_number"}
        results = ib.plan([CHF_IN], LOOKUPS, meta)
        self.assertEqual(results[0]["unknown"], [])
        results = ib.plan([CHF_IN], LOOKUPS, meta - {"reference_number"})
        self.assertEqual(results[0]["unknown"], ["Bank Transaction.reference_number"])


class SummaryTest(unittest.TestCase):
    def test_totals_per_account_year_and_currency_are_not_converted(self):
        text = ib.summary(ib.plan([CHF_IN, CHF_OUT, EUR_OUT], LOOKUPS))
        rows = {tuple(line.split()[:3]): line.split()[3:] for line in text.splitlines()
                if line.split()[:1] in (["11"], ["12"])}
        self.assertEqual(rows[("11", "2026", "CHF")], ["2", "1,500.00", "250.50"])
        self.assertEqual(rows[("12", "2025", "EUR")], ["1", "0.00", "99.99"])

    def test_summary_names_no_bank_account(self):
        text = ib.summary(ib.plan([CHF_IN, EUR_OUT], LOOKUPS))
        self.assertNotIn("Hauptkonto", text)
        self.assertNotIn("Fremdwaehrung", text)

    def test_unmapped_and_booked_counts_are_in_the_summary(self):
        text = ib.summary(ib.plan([CHF_IN, record(CHF_IN, id=9009, bank_account_id=99), CHF_OUT], LOOKUPS))
        self.assertIn("records: 3, mapped: 2, unmapped: 1", text)
        self.assertIn("booked in bexio (reconciled or auto_reconciled): 1, not booked: 1", text)


class LookupTest(unittest.TestCase):
    def test_bank_account_gets_the_currency_of_the_export_and_its_erp_name(self):
        data = {"currencies": [{"id": 1, "name": "CHF"}, {"id": 2, "name": "EUR"}],
                "bank_accounts": [{"id": 11, "currency_id": 1}, {"id": 12, "currency_id": 2}]}
        lookups = ib.lookups_from_erp(FakeErp(), data)
        self.assertEqual(lookups["bank_account"], {"11": {"name": "Hauptkonto - Testbank AG", "currency": "CHF",
                                                          "account": "1020 - Testbank - X"}})
        self.assertEqual(lookups["currency"], {"1": "CHF", "2": "EUR"})


class MainTest(unittest.TestCase):
    def test_without_the_export_file_it_says_not_exported_yet(self):
        with tempfile.TemporaryDirectory() as export, \
                mock.patch.object(im.Erp, "from_file", side_effect=AssertionError("ERPNext must not be read")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ib.main(["--dry-run", "--export", export]), 0)
        self.assertIn("not exported yet", out.getvalue())

    def _export(self, export, rows):
        files = {"bank_transactions": rows, "bank_accounts": [{"id": 11, "currency_id": 1}],
                 "currencies": [{"id": 1, "name": "CHF"}]}
        for name, body in files.items():
            with open(os.path.join(export, name + ".json"), "w") as f:
                json.dump(body, f)

    def _erp_fields(self):
        class Erp(FakeErp):
            def meta(self, doctype):
                return {"fields": [{"fieldname": f} for f in ("bexio_id", "date", "bank_account", "currency", "deposit",
                                                             "withdrawal", "description", "reference_number", "company")]}
        return Erp()

    def test_dry_run_with_the_export_prints_totals_and_writes_only_the_private_report(self):
        with tempfile.TemporaryDirectory() as export:
            self._export(export, [CHF_IN, record(CHF_IN, id=9009, bank_account_id=99)])
            report = os.path.join(export, "report.txt")
            unmatched = os.path.join(export, "unmatched.txt")
            with mock.patch.object(im.Erp, "from_file", return_value=self._erp_fields()), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                code = ib.main(["--dry-run", "--export", export, "--report", report, "--unmatched", unmatched])
            with open(report) as f:
                self.assertEqual(f.read(), "Bank Transaction 9009: unmapped: bank account 99 has no Bank Account with that bexio_id\n")
            self.assertFalse(os.path.exists(os.path.join(export, "bank-docs.json")))
        self.assertEqual(code, 1)
        self.assertIn("records: 2, mapped: 1, unmapped: 1", out.getvalue())
        self.assertIn("dry run: nothing was written to ERPNext", out.getvalue())

    def test_write_keeps_the_documents_in_a_private_file_for_the_loader(self):
        with tempfile.TemporaryDirectory() as export:
            self._export(export, [CHF_IN, CHF_OUT])
            docs = os.path.join(export, "bank-docs.json")
            report = os.path.join(export, "report.txt")
            unmatched = os.path.join(export, "unmatched.txt")
            with mock.patch.object(im.Erp, "from_file", return_value=self._erp_fields()), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                code = ib.main(["--write", docs, "--export", export, "--report", report, "--unmatched", unmatched])
            with open(docs) as f:
                written = json.load(f)["documents"]
        self.assertEqual(code, 0)
        self.assertEqual([d["bexio_id"] for d in written], ["9001", "9002"])
        self.assertEqual([d["booked"] for d in written], [True, False])
        self.assertEqual(written[0]["values"]["deposit"], 1500.0)
        self.assertNotIn("doctype", written[0]["values"])
        self.assertIn("wrote 2 documents", out.getvalue())

    def test_a_run_without_dry_run_or_write_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ib.main([])


ACCOUNT = "1020 - Testbank - X"


def tx(bexio_id, amount, date="2026-03-31", booked=True):
    """A mapped transaction as match takes it; the amount is in minus out."""
    return {"bexio_id": bexio_id, "account": ACCOUNT, "date": date, "amount": Decimal(amount), "booked": booked}


def voucher(name, amount, doctype="Payment Entry", date="2026-03-31", account=ACCOUNT, bexio_id="b"):
    """A voucher's bank-side line, as read_vouchers gives it; the amount is money in positive."""
    return {"doctype": doctype, "name": name, "bexio_id": bexio_id, "account": account, "date": date, "amount": Decimal(amount)}


class MatchTest(unittest.TestCase):
    def test_one_voucher_of_the_same_account_date_and_amount_is_a_pass_one_match(self):
        matches = ib.match([tx("1", "1500.00")], [voucher("PE-1", "1500.00")])
        self.assertEqual(matches["1"], (1, [voucher("PE-1", "1500.00")], ""))

    def test_an_unbooked_transaction_is_not_matched_at_all(self):
        self.assertEqual(ib.match([tx("1", "1500.00", booked=False)], [voucher("PE-1", "1500.00")]), {})

    def test_the_voucher_must_be_on_the_same_day_and_account(self):
        matches = ib.match([tx("1", "1500.00", date="2026-03-30")], [voucher("PE-1", "1500.00")])
        self.assertEqual(matches["1"], (None, [], "no candidate"))
        matches = ib.match([tx("1", "1500.00")], [voucher("PE-1", "1500.00", account="1021 - Zweitbank - X")])
        self.assertEqual(matches["1"], (None, [], "no candidate"))

    def test_the_sign_is_part_of_the_amount(self):
        matches = ib.match([tx("1", "-1500.00")], [voucher("PE-1", "1500.00")])
        self.assertEqual(matches["1"], (None, [], "no candidate"))

    def test_two_vouchers_with_the_same_key_leave_the_transaction_unmatched(self):
        matches = ib.match([tx("1", "1500.00")], [voucher("PE-1", "1500.00"), voucher("JE-1", "1500.00", doctype="Journal Entry")])
        self.assertEqual(matches["1"], (None, [], "2 candidates"))

    def test_a_combined_transfer_of_the_days_payments_is_a_pass_two_match_when_unique(self):
        pays = [voucher("PE-700", "700.00"), voucher("PE-800", "800.00"), voucher("PE-50", "50.00")]
        matches = ib.match([tx("1", "1500.00")], pays)
        self.assertEqual(matches["1"], (2, [pays[0], pays[1]], ""))

    def test_several_combinations_of_the_days_payments_leave_it_unmatched(self):
        pays = [voucher("PE-500a", "500.00"), voucher("PE-1000a", "1000.00"),
                voucher("PE-500b", "500.00"), voucher("PE-1000b", "1000.00")]
        matches = ib.match([tx("1", "1500.00")], pays)
        self.assertEqual(matches["1"], (None, [], "4 combinations of the day's payments"))

    def test_only_payment_entries_are_combined(self):
        pays = [voucher("PE-700", "700.00"), voucher("PE-900", "900.00"),
                voucher("JE-300", "300.00", doctype="Journal Entry")]
        matches = ib.match([tx("1", "1000.00")], pays)
        self.assertEqual(matches["1"], (None, [], "no candidate"))

    def test_a_voucher_matched_alone_is_not_used_in_a_combination(self):
        pays = [voucher("PE-300", "300.00"), voucher("PE-700", "700.00")]
        matches = ib.match([tx("1", "300.00"), tx("2", "1000.00")], pays)
        self.assertEqual(matches["1"][0], 1)
        self.assertEqual(matches["2"], (None, [], "no candidate"))

    def test_a_day_with_too_many_payments_is_not_searched(self):
        pays = [voucher("PE-{}".format(n), "1.00") for n in range(ib.COMBINE_LIMIT + 1)]
        matches = ib.match([tx("1", "2.00")], pays)
        self.assertEqual(matches["1"], (None, [], "{} payments that day: too many to combine".format(ib.COMBINE_LIMIT + 1)))

    def test_a_voucher_that_two_transactions_claim_takes_neither(self):
        matches = ib.match([tx("1", "1500.00"), tx("2", "1500.00")], [voucher("PE-1", "1500.00")])
        self.assertEqual(matches["1"], (None, [], "a voucher that another transaction also matches"))
        self.assertEqual(matches["2"], (None, [], "a voucher that another transaction also matches"))

    def test_the_result_does_not_depend_on_the_order_of_the_transactions(self):
        pays = [voucher("PE-700", "700.00"), voucher("PE-800", "800.00"), voucher("PE-50", "50.00")]
        forward = ib.match([tx("1", "1500.00"), tx("2", "850.00")], pays)
        backward = ib.match([tx("2", "850.00"), tx("1", "1500.00")], pays)
        self.assertEqual(forward, backward)


class VoucherTest(unittest.TestCase):
    def test_a_voucher_is_one_bexio_keyed_document_and_account_with_its_net_debit(self):
        gl = [{"voucher_type": "Payment Entry", "voucher_no": "PE-1", "account": ACCOUNT, "posting_date": "2026-03-31",
               "debit": 1500.0, "credit": 0.0},
              {"voucher_type": "Journal Entry", "voucher_no": "JE-1", "account": ACCOUNT, "posting_date": "2026-04-02",
               "debit": 300.0, "credit": 0.0},
              {"voucher_type": "Journal Entry", "voucher_no": "JE-1", "account": ACCOUNT, "posting_date": "2026-04-02",
               "debit": 0.0, "credit": 100.0},
              {"voucher_type": "Journal Entry", "voucher_no": "JE-9", "account": ACCOUNT, "posting_date": "2026-04-02",
               "debit": 165.66, "credit": 0.0},
              {"voucher_type": "Payment Entry", "voucher_no": "PE-NOKEY", "account": ACCOUNT, "posting_date": "2026-04-02",
               "debit": 0.0, "credit": 10.0}]
        vouchers = {"Payment Entry": [{"name": "PE-1", "bexio_id": "pay-1"}, {"name": "PE-7", "bexio_id": "pay-7"}],
                    "Journal Entry": [{"name": "JE-1", "bexio_id": "manual-7"},
                                      {"name": "JE-9", "bexio_id": "manual-1099-correction"}]}
        found = ib.read_vouchers(FakeErp(gl=gl, vouchers=vouchers), [ACCOUNT])
        self.assertEqual([(v["name"], str(v["amount"]), v["bexio_id"]) for v in found],
                         [("PE-1", "1500.00", "pay-1"), ("JE-1", "200.00", "manual-7")])

    def test_the_gl_account_name_gives_its_number(self):
        self.assertEqual(ib.account_number("1020 - UBS Kontokorrent - bic"), "1020")

    def test_match_transactions_takes_the_gl_account_and_the_signed_amount(self):
        results = ib.plan([CHF_IN, CHF_OUT], LOOKUPS)
        mapped = ib.match_transactions(results, LOOKUPS)
        self.assertEqual([(t["bexio_id"], t["account"], str(t["amount"]), t["booked"]) for t in mapped],
                         [("9001", "1020 - Testbank - X", "1500.00", True), ("9002", "1020 - Testbank - X", "-250.50", False)])


class BalanceTest(unittest.TestCase):
    def test_year_end_balances_are_cumulative_per_account(self):
        lines = [("1020", "2021-02-01", Decimal("100.00")), ("1020", "2023-05-01", Decimal("-40.00")),
                 ("1021", "2021-06-01", Decimal("7.00"))]
        balances = ib.year_end_balances(lines, ["2021", "2022", "2023"])
        self.assertEqual(balances[("1020", "2021")], Decimal("100.00"))
        self.assertEqual(balances[("1020", "2022")], Decimal("100.00"))
        self.assertEqual(balances[("1020", "2023")], Decimal("60.00"))
        self.assertEqual(balances[("1021", "2023")], Decimal("7.00"))

    def test_journal_bank_lines_leave_out_the_carry_forward_lines_and_sign_the_sides(self):
        accounts = [{"id": 77, "account_no": 1020}, {"id": 144, "account_no": 3200}]
        journal = [{"date": "2021-01-01T00:00:00+01:00", "debit_account_id": 77, "credit_account_id": 144,
                    "base_currency_amount": 500, "description": "provisorischer Saldovortrag"},
                   {"date": "2021-03-02T00:00:00+01:00", "debit_account_id": 144, "credit_account_id": 77,
                    "base_currency_amount": "250.5", "description": "Miete"}]
        self.assertEqual(ib.journal_bank_lines(journal, accounts, {"1020"}), [("1020", "2021-03-02", Decimal("-250.50"))])

    def test_balance_lines_compare_the_ledger_with_the_journal_per_year_end(self):
        gl = [{"account": ACCOUNT, "posting_date": "2021-03-31", "debit": 100.0, "credit": 0.0},
              {"account": ACCOUNT, "posting_date": "2021-04-30", "debit": 0.0, "credit": 30.0}]
        lookups = {"bank_account": {"11": {"account": ACCOUNT}}}
        journal = [{"date": "2021-03-31T00:00:00+02:00", "debit_account_id": 77, "credit_account_id": 144,
                    "base_currency_amount": 100, "description": "Einzahlung"},
                   {"date": "2021-04-30T00:00:00+02:00", "debit_account_id": 144, "credit_account_id": 77,
                    "base_currency_amount": 30, "description": "Miete"}]
        accounts = [{"id": 77, "account_no": 1020}, {"id": 144, "account_no": 3200}]
        with tempfile.TemporaryDirectory() as export:
            for name, body in (("journal", journal), ("accounts", accounts)):
                with open(os.path.join(export, name + ".json"), "w") as f:
                    json.dump(body, f)
            text = ib.balance_lines(FakeErp(gl=gl), lookups, export)
        self.assertEqual([line.split() for line in text.splitlines()[1:]], [["1020", "2021", "70.00", "70.00", "0.00"]])


class ReconcileOutputTest(unittest.TestCase):
    def test_only_matched_transactions_are_reconciled_and_only_booked_ones_are_listed_as_unmatched(self):
        results = ib.plan([CHF_IN, CHF_OUT], LOOKUPS)
        matches = {"9001": (None, [], "no candidate")}
        self.assertEqual(ib.reconcile_lines(matches), [])
        self.assertEqual(ib.unmatched_lines(results, matches), ["Bank Transaction 9001: no candidate"])

    def test_reconcile_lines_name_each_voucher_by_doctype_and_name(self):
        pays = [voucher("PE-700", "700.00"), voucher("PE-800", "800.00")]
        matches = ib.match([tx("1", "1500.00")], pays)
        self.assertEqual(ib.reconcile_lines(matches),
                         [{"bexio_id": "1", "vouchers": [{"doctype": "Payment Entry", "name": "PE-700"},
                                                         {"doctype": "Payment Entry", "name": "PE-800"}]}])

    def test_the_written_file_carries_the_reconciliations_for_the_loader(self):
        results = ib.plan([CHF_IN], LOOKUPS)
        reconcile = [{"bexio_id": "9001", "vouchers": [{"doctype": "Payment Entry", "name": "PE-1"}]}]
        with tempfile.TemporaryDirectory() as export:
            path = os.path.join(export, "docs.json")
            self.assertEqual(ib.write_documents(results, path, reconcile), 1)
            with open(path) as f:
                written = json.load(f)
        self.assertEqual(written["reconcile"], reconcile)
        self.assertEqual(len(written["documents"]), 1)

    def test_the_match_summary_counts_per_account_and_year_and_names_no_voucher(self):
        results = ib.plan([CHF_IN, CHF_OUT], LOOKUPS)
        matches = ib.match(ib.match_transactions(results, LOOKUPS), [voucher("PE-1500", "1500.00")])
        text = ib.match_summary(results, matches)
        rows = [line.split() for line in text.splitlines()[1:] if line.split()[0] == "11"]
        self.assertEqual(rows, [["11", "2026", "1", "0", "0", "1"]])
        self.assertNotIn("PE-1500", text)


class LiveCheckTest(unittest.TestCase):
    def _row(self, bexio_id, **changes):
        row = {"name": "BTN-" + bexio_id, "bexio_id": bexio_id, "bank_account": "Hauptkonto - Testbank AG",
               "date": "2026-03-31", "deposit": 1500.0, "withdrawal": 0.0, "status": "Reconciled", "docstatus": 1}
        row.update(changes)
        return row

    def test_a_right_document_is_no_difference_and_a_wrong_status_is_listed_by_bexio_id(self):
        results = ib.plan([CHF_IN, CHF_OUT], LOOKUPS)
        matches = {"9001": (1, [voucher("PE-1", "1500.00")], "")}
        rows = [self._row("9001"), self._row("9002", date="2026-04-02", deposit=0.0, withdrawal=250.5)]
        lines, differences = ib.live_check(rows, results, matches, LOOKUPS)
        self.assertEqual(differences, ["Bank Transaction 9002: differs (status Reconciled, submitted True)"])
        self.assertEqual(lines[-1], "differences by bexio id: 1")

    def test_the_table_shows_count_reconciled_and_sums_as_dry_run_over_erpnext(self):
        results = ib.plan([CHF_IN, CHF_OUT], LOOKUPS)
        matches = {"9001": (1, [voucher("PE-1", "1500.00")], "")}
        rows = [self._row("9001"), self._row("9002", date="2026-04-02", deposit=0.0, withdrawal=250.5, status="Unreconciled")]
        lines, _ = ib.live_check(rows, results, matches, LOOKUPS)
        self.assertEqual([line.split() for line in lines[1:-1]],
                         [["11", "2026", "2", "/", "2", "1", "/", "1", "1,500.00", "/", "1,500.00", "250.50", "/", "250.50"]])

    def test_a_missing_document_and_an_extra_one_are_listed(self):
        results = ib.plan([CHF_IN], LOOKUPS)
        matches = {"9001": (None, [], "no candidate")}
        rows = [self._row("9009", status="Unreconciled")]
        _, differences = ib.live_check(rows, results, matches, LOOKUPS)
        self.assertEqual(differences, ["Bank Transaction 9001: not in ERPNext",
                                       "Bank Transaction 9009: in ERPNext, not in the export"])


if __name__ == "__main__":
    unittest.main()
