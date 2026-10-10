"""Offline tests for import_payroll.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import io
import os
import tempfile
import unittest
from decimal import Decimal

import import_payroll as ipr
import posting_plan as pp

# bexio account ids of the test chart -> ERPNext account names
ACCOUNTS = {"A5000": "5000 - Lohn Test - bic", "A1020": "1020 - Bank Test - bic", "A2270": "2270 - Sozial Test - bic",
            "A1091": "1091 - Vorschuss Test - bic"}


def line(line_id, debit, credit, amount, day="2024-01-31", description="Lohn Test", ref_class=None, currency=1, factor=1, base=None):
    """A bexio journal line; debit and credit are bexio account ids of the test chart."""
    return {"id": line_id, "ref_class": ref_class, "ref_id": None, "ref_uuid": None, "date": day + "T00:00:00+01:00",
            "debit_account_id": debit, "credit_account_id": credit, "amount": amount,
            "base_currency_amount": amount if base is None else base, "currency_id": currency,
            "currency_factor": factor, "description": description}


def manual_row(row_id, description):
    return {"id": row_id, "description": description}


def export(journal, manual_entries=()):
    return {"journal": list(journal), "manual_entries": list(manual_entries), "invoice_payments": []}


def test_data():
    """Invented lines: one payroll line of 2024, one in EUR, one of 2025, a rule 5 conflict, a line of a document, and a VAT-booked one."""
    journal = [
        line(1, "A5000", "A1020", 1000),
        line(2, "A5000", "A1020", 200, day="2024-03-31", description="Lohn EUR", currency=2, factor=0.95, base=190),
        line(3, "A2270", "A1020", 50, day="2024-02-29", description="Sozial Test"),
        line(4, "A1091", "A1020", 75, day="2024-04-30", description="Vorschuss Test"),
        line(5, "A1020", "A5000", 300, day="2025-01-31", description="Lohn 2025"),
        line(6, "A1091", "A1020", 10, day="2024-05-31", description="Rechnung", ref_class="KbInvoice"),
    ]
    manual_entries = [
        # rule 5: a banking entry and a manual entry of the same day and text; line 3 has no row of its own
        {"id": "m-1", "type": "banking_transaction", "date": "2024-02-29", "entries": [manual_row(301, "Sozial Test")]},
        {"id": "m-2", "type": "manual_single_entry", "date": "2024-02-29", "entries": [manual_row(302, "Sozial Test")]},
    ]
    return export(journal, manual_entries)


class SelectTest(unittest.TestCase):
    def test_takes_the_unsourced_lines_and_leaves_out_rule_5_and_the_vat_booked(self):
        data = test_data()
        buckets = pp.classify(data)
        self.assertEqual(buckets[3], "unsourced")
        lines, left_out = ipr.select(data, buckets, vat_ids={4})
        self.assertEqual([l["id"] for l in lines], [1, 2, 5])
        self.assertEqual(left_out, [(3, ipr.RULE_5), (4, ipr.VAT_BOOKED)])

    def test_a_document_line_is_never_a_payroll_line(self):
        data = test_data()
        buckets = pp.classify(data)
        lines, _ = ipr.select(data, buckets, vat_ids=set())
        self.assertNotIn(6, [l["id"] for l in lines])

    def test_rule_5_finds_the_line_with_parents_of_two_kinds(self):
        self.assertEqual(ipr.rule5_conflicts(test_data()), {3})

    def test_vat_booked_ids_are_the_second_word_of_each_line(self):
        with tempfile.TemporaryDirectory() as private:
            for name, text in (("bexio-vat-on-payment-ids.txt", "journal 14 receipt 3\n"),
                               ("bexio-vat-on-payment-out-ids.txt", "journal 42 group abc: booked elsewhere\nnone\n")):
                with open(os.path.join(private, name), "w", encoding="utf-8") as f:
                    f.write(text)
            self.assertEqual(ipr.vat_booked_ids(private), {14, 42})


class JournalEntryTest(unittest.TestCase):
    def test_one_debit_row_and_one_credit_row_on_the_mapped_accounts(self):
        doc = ipr.journal_entry(line(1, "A5000", "A1020", 1000), ACCOUNTS)
        rows = doc["values"]["accounts"]
        self.assertEqual(len(rows), 2)
        debit, credit = rows
        self.assertEqual((debit["account"], debit["debit"], debit["debit_in_account_currency"]), ("5000 - Lohn Test - bic", 1000, 1000))
        self.assertEqual((credit["account"], credit["credit"], credit["credit_in_account_currency"]), ("1020 - Bank Test - bic", 1000, 1000))
        self.assertNotIn("credit", debit)
        self.assertNotIn("debit", credit)

    def test_the_document_is_keyed_by_the_journal_line_id(self):
        doc = ipr.journal_entry(line(7, "A5000", "A1020", 12.5, day="2023-12-31", description="Lohn Test"), ACCOUNTS)
        self.assertEqual(doc["bexio_id"], "journal-7")
        self.assertEqual(doc["values"]["bexio_id"], "journal-7")
        self.assertEqual(doc["values"]["posting_date"], "2023-12-31")
        self.assertEqual(doc["values"]["voucher_type"], "Journal Entry")
        self.assertEqual(doc["values"]["user_remark"], "bexio journal 7: Lohn Test")
        self.assertEqual(doc["values"]["multi_currency"], 0)

    def test_a_line_in_another_currency_keeps_its_amount_and_the_rate(self):
        doc = ipr.journal_entry(line(2, "A5000", "A1020", 200, currency=2, factor=0.95, base=190), ACCOUNTS)
        debit, credit = doc["values"]["accounts"]
        self.assertEqual((debit["debit"], debit["debit_in_account_currency"], debit["exchange_rate"]), (190, 200, 0.95))
        self.assertEqual((credit["credit"], credit["credit_in_account_currency"]), (190, 200))
        self.assertEqual(doc["values"]["multi_currency"], 1)

    def test_an_account_without_an_erpnext_account_is_unmapped_not_guessed(self):
        documents, unmapped = ipr.plan([line(1, "A5000", "A9999", 10)], ACCOUNTS)
        self.assertEqual(documents, [])
        self.assertEqual(len(unmapped), 1)
        self.assertEqual(unmapped[0][0], 1)
        self.assertIn("A9999", unmapped[0][1])


class TotalsTest(unittest.TestCase):
    def test_count_and_chf_per_year(self):
        data = test_data()
        lines, _ = ipr.select(data, pp.classify(data), vat_ids={4})
        per_year = ipr.totals(lines)
        self.assertEqual(per_year["2024"], [2, Decimal("1190")])
        self.assertEqual(per_year["2025"], [1, Decimal("300")])

    def test_the_plan_writes_one_document_per_line_in_the_selection(self):
        data = test_data()
        lines, _ = ipr.select(data, pp.classify(data), vat_ids={4})
        documents, unmapped = ipr.plan(lines, ACCOUNTS)
        self.assertEqual(unmapped, [])
        self.assertEqual([d["bexio_id"] for d in documents], ["journal-1", "journal-2", "journal-5"])


class FakeErp:
    """The two reads of check(): Journal Entries and GL Entries, from invented rows."""

    def __init__(self, entries, gl):
        self.entries = entries  # [{"name", "bexio_id"}]
        self.gl = gl            # [{"voucher_no", "account", "debit", "credit"}]

    def list(self, doctype, filters=None, fields=("name",)):
        if doctype == "Journal Entry":
            return list(self.entries)
        wanted = next(f[2] for f in filters if f[0] == "voucher_no")
        return [row for row in self.gl if row["voucher_no"] in wanted]


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.lines = [line(1, "A5000", "A1020", 1000), line(5, "A1020", "A5000", 300, day="2025-01-31", description="Lohn 2025")]
        self.accounts = {"A5000": "5000 - Lohn Test - bic", "A1020": "1020 - Bank Test - bic"}
        self.private = tempfile.mkdtemp()

    def erp(self, gl_rows, entries=None):
        entries = [{"name": "ACC-JV-1", "bexio_id": "journal-1"}, {"name": "ACC-JV-5", "bexio_id": "journal-5"}] if entries is None else entries
        return FakeErp(entries, gl_rows)

    def gl(self):
        return [
            {"voucher_no": "ACC-JV-1", "account": "5000 - Lohn Test - bic", "debit": 1000, "credit": 0},
            {"voucher_no": "ACC-JV-1", "account": "1020 - Bank Test - bic", "debit": 0, "credit": 1000},
            {"voucher_no": "ACC-JV-5", "account": "1020 - Bank Test - bic", "debit": 300, "credit": 0},
            {"voucher_no": "ACC-JV-5", "account": "5000 - Lohn Test - bic", "debit": 0, "credit": 300},
        ]

    def test_posted_and_matching_lines_are_no_difference(self):
        with contextlib.redirect_stdout(io.StringIO()):
            differs = ipr.check(self.lines, self.accounts, self.erp(self.gl()), private=self.private)
        self.assertEqual(differs, 0)
        with open(os.path.join(self.private, ipr.IDS_FILE), encoding="utf-8") as f:
            self.assertEqual([t.split()[1] for t in f], ["1", "5"])

    def test_a_missing_line_and_a_differing_line_are_listed(self):
        gl = [row for row in self.gl() if row["voucher_no"] == "ACC-JV-1"]
        gl[1]["credit"] = 999
        with contextlib.redirect_stdout(io.StringIO()):
            differs = ipr.check(self.lines, self.accounts, self.erp(gl, entries=[{"name": "ACC-JV-1", "bexio_id": "journal-1"}]), private=self.private)
        self.assertEqual(differs, 2)
        with open(os.path.join(self.private, ipr.DIFFERENCES_FILE), encoding="utf-8") as f:
            self.assertEqual([t.strip() for t in f], ["journal 1: differs", "journal 5: missing"])


class MainTest(unittest.TestCase):
    def test_exactly_one_of_dry_run_and_write(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                ipr.main([])
            with self.assertRaises(SystemExit):
                ipr.main(["--dry-run", "--write", "x.json"])


if __name__ == "__main__":
    unittest.main()
