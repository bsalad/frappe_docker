"""Offline tests for mwst_report.py. Invented documents only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import ast
import contextlib
import datetime
import io
import os
import shutil
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

import mwst_report as mr

D = Decimal

# the ERPNext templates by name, as templates_by_name() gives them; the names carry the code, the ids are bexio's
TEMPLATE_NAMES = {
    "UN81 8.1% Normalsatz": "28",
    "UR26 2.6% Reduzierter Satz": "29",
    "UEX 0% Export, steuerbefreit": "3",
    "UO81 8.1% optiert": "31",
    "VM81 8.1% Normalsatz Material/DL": "35",
    "VB26 2.6% Reduzierter Satz Invest./Aufwand": "37",
    "VES 8.1% Vorsteuerkorrektur nachträglich": "40",
    "BZM81 8.1% Bezugsteuer Material/DL": "33",
    "BZB81 8.1% Bezugsteuer Invest./Aufwand": "32",
    "VM77 7.7% Normalsatz Material/DL": "22",
}


def tax(name, amount, add_deduct=None):
    row = {"description": name, "base_tax_amount": amount, "rate": 0}
    if add_deduct:
        row["add_deduct_tax"] = add_deduct
    return row


def doc(day, net, taxes):
    return {"posting_date": day, "base_net_total": net, "taxes": taxes}


class Periods(unittest.TestCase):
    def test_quarter(self):
        self.assertEqual(mr.period_bounds("2026Q3"), (datetime.date(2026, 7, 1), datetime.date(2026, 9, 30)))

    def test_fourth_quarter_ends_in_december(self):
        self.assertEqual(mr.period_bounds("2026Q4"), (datetime.date(2026, 10, 1), datetime.date(2026, 12, 31)))

    def test_half_years(self):
        self.assertEqual(mr.period_bounds("2026H1"), (datetime.date(2026, 1, 1), datetime.date(2026, 6, 30)))
        self.assertEqual(mr.period_bounds("2026H2"), (datetime.date(2026, 7, 1), datetime.date(2026, 12, 31)))

    def test_other_period_is_refused(self):
        with self.assertRaises(ValueError):
            mr.period_bounds("2026M3")


class Table(unittest.TestCase):
    """The table must follow swiss-setup.py's BEXIO_TAXES: same 42 ids, codes, rates and kinds."""

    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__), "..", "scripts", "swiss-setup.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "BEXIO_TAXES" for t in node.targets):
                cls.taxes = ast.literal_eval(node.value)

    def test_all_42_codes_are_in_the_table(self):
        self.assertEqual(len(self.taxes), 42)
        self.assertEqual(set(mr.TEMPLATES), {row[1] for row in self.taxes})

    def test_code_and_rate_match_the_setup(self):
        for kind, bexio_id, code, _name, rate, _valid, _active, _account, _deduct in self.taxes:
            with self.subTest(bexio_id=bexio_id):
                self.assertEqual(mr.TEMPLATES[bexio_id][:2], (code, rate))

    def test_kind_matches_the_setup(self):
        for kind, bexio_id, _code, _name, _rate, _valid, _active, _account, deduct in self.taxes:
            with self.subTest(bexio_id=bexio_id):
                table_kind = mr.TEMPLATES[bexio_id][2]
                if kind == "S":
                    self.assertEqual(table_kind, "S")
                elif deduct:
                    self.assertEqual(table_kind, "BZ")
                else:
                    self.assertEqual(table_kind, "P")

    def test_form_rows_are_the_confirmed_ones(self):
        rows = {entry[3] for entry in mr.TEMPLATES.values() if entry[3]}
        self.assertEqual(rows, {"302", "303", "312", "313", "342", "343", "400", "405", "415"})


class Rows(unittest.TestCase):
    def test_owed_bezugsteuer_row_changes_with_the_year(self):
        self.assertEqual(mr.owed_row(datetime.date(2023, 12, 31)), "382")
        self.assertEqual(mr.owed_row(datetime.date(2024, 1, 1)), "383")

    def test_base_is_derived_from_tax_and_rate(self):
        self.assertEqual(mr.derived_base(D("8.10"), 8.1), D("100.00"))

    def test_zero_rate_has_no_derived_base(self):
        self.assertIsNone(mr.derived_base(D("0"), 0))


class Sales(unittest.TestCase):
    def test_two_rates_add_up_to_owed_tax(self):
        t = mr.plan([doc(datetime.date(2026, 8, 3), D("1000.00"), [
            tax("UN81 8.1% Normalsatz", "81.00"),
            tax("UR26 2.6% Reduzierter Satz", "26.00"),
        ])], [], TEMPLATE_NAMES)
        rows, saldo = mr.form_totals(t)
        self.assertEqual(rows["200"][0], D("1000.00"))
        self.assertEqual(rows["303"], [D("1000.00"), D("81.00")])
        self.assertEqual(rows["313"], [D("1000.00"), D("26.00")])
        self.assertEqual(rows["399"][1], D("107.00"))
        self.assertEqual(saldo, D("107.00"))

    def test_exempt_export_is_in_the_net_but_has_no_tax_row(self):
        t = mr.plan([doc(datetime.date(2026, 8, 3), D("500.00"), [
            tax("UEX 0% Export, steuerbefreit", "0"),
        ])], [], TEMPLATE_NAMES)
        self.assertEqual(mr.form_totals(t)[0]["200"][0], D("500.00"))
        self.assertEqual(t.unchecked_tax, D("0"))

    def test_optiert_sale_is_owed_at_the_normal_rate(self):
        t = mr.plan([doc(datetime.date(2026, 8, 3), D("100.00"), [
            tax("UO81 8.1% optiert", "8.10"),
        ])], [], TEMPLATE_NAMES)
        self.assertEqual(mr.form_totals(t)[0]["303"], [D("100.00"), D("8.10")])
        self.assertEqual(mr.form_totals(t)[0]["399"][1], D("8.10"))
        self.assertEqual(t.unchecked_tax, D("0"))

    def test_tax_on_a_template_without_a_ziffer_is_named_not_put_in_a_ziffer(self):
        t = mr.plan([], [doc(datetime.date(2026, 8, 3), D("100.00"), [
            tax("Vorsteuer Import", "8.10", "Add"),
        ])], {"Vorsteuer Import": "7"})
        self.assertEqual(t.unchecked_tax, D("8.10"))
        self.assertEqual(mr.form_totals(t)[0]["420"][1], D("0"))

    def test_credit_note_is_negative_and_reduces_the_row(self):
        t = mr.plan([
            doc(datetime.date(2026, 8, 3), D("1000.00"), [tax("UN81 8.1% Normalsatz", "81.00")]),
            doc(datetime.date(2026, 8, 20), D("-200.00"), [tax("UN81 8.1% Normalsatz", "-16.20")]),
        ], [], TEMPLATE_NAMES)
        self.assertEqual(mr.form_totals(t)[0]["303"], [D("800.00"), D("64.80")])


class Purchases(unittest.TestCase):
    def test_material_and_investment_vorsteuer_and_the_total(self):
        t = mr.plan([], [doc(datetime.date(2026, 8, 3), D("500"), [
            tax("VM81 8.1% Normalsatz Material/DL", "40.50", "Add"),
            tax("VB26 2.6% Reduzierter Satz Invest./Aufwand", "13.00", "Add"),
        ])], TEMPLATE_NAMES)
        rows, saldo = mr.form_totals(t)
        self.assertEqual(rows["400"][1], D("40.50"))
        self.assertEqual(rows["405"][1], D("13.00"))
        self.assertEqual(rows["420"][1], D("53.50"))
        self.assertEqual(saldo, D("-53.50"))

    def test_reverse_charge_is_vorsteuer_and_owed_in_the_same_amount(self):
        t = mr.plan([], [doc(datetime.date(2026, 8, 3), D("1000"), [
            tax("BZM81 8.1% Bezugsteuer Material/DL", "81.00", "Add"),
            tax("BZM81 8.1% Bezugsteuer Material/DL", "81.00", "Deduct"),
        ])], TEMPLATE_NAMES)
        rows, saldo = mr.form_totals(t)
        self.assertEqual(rows["400"][1], D("81.00"))
        self.assertEqual(rows["383"][1], D("81.00"))
        self.assertEqual(saldo, D("0.00"))

    def test_bezugsteuer_before_2024_is_in_382(self):
        t = mr.plan([], [doc(datetime.date(2023, 5, 2), D("1000"), [
            tax("BZB81 8.1% Bezugsteuer Invest./Aufwand", "81.00", "Deduct"),
        ])], TEMPLATE_NAMES)
        self.assertEqual(mr.form_totals(t)[0]["382"][1], D("81.00"))

    def test_correction_goes_to_415(self):
        t = mr.plan([], [doc(datetime.date(2026, 8, 3), D("0"), [
            tax("VES 8.1% Vorsteuerkorrektur nachträglich", "-4.00", "Add"),
        ])], TEMPLATE_NAMES)
        self.assertEqual(mr.form_totals(t)[0]["415"][1], D("-4.00"))

    def test_deduct_on_a_plain_purchase_is_unmapped(self):
        t = mr.plan([], [doc(datetime.date(2026, 8, 3), D("100"), [
            tax("VM81 8.1% Normalsatz Material/DL", "8.10", "Deduct"),
        ])], TEMPLATE_NAMES)
        self.assertEqual(t.unmapped, ["Deduct row on VM81"])


class Unmapped_(unittest.TestCase):
    def test_template_without_bexio_id_is_not_guessed_from_the_rate(self):
        # ERPNext's own template for 8.1 % has no bexio_id, so it is not in the lookup
        t = mr.plan([doc(datetime.date(2026, 8, 3), D("100"), [
            tax("MWST 8.1% Standard", "8.10"),
        ])], [], TEMPLATE_NAMES)
        self.assertEqual(t.unmapped, ["MWST 8.1% Standard"])
        self.assertEqual(t.rows, {"200": [D("100"), D("0")]})

    def test_bexio_id_outside_the_table_is_unmapped(self):
        t = mr.plan([doc(datetime.date(2026, 8, 3), D("100"), [tax("X", "1.00")])], [], {"X": "999"})
        self.assertEqual(t.unmapped, ["X"])


class Output(unittest.TestCase):
    def test_summary_names_no_document_and_no_template(self):
        t = mr.plan([doc(datetime.date(2026, 8, 3), D("1000.00"), [
            tax("UN81 8.1% Normalsatz", "81.00"),
        ])], [], TEMPLATE_NAMES)
        text = "\n".join(mr.summary_lines(t))
        self.assertIn("303", text)
        self.assertNotIn("UN81", text)
        self.assertNotIn("Beispiel", text)

    def test_csv_has_the_rows_and_the_codes(self):
        t = mr.plan([doc(datetime.date(2026, 8, 3), D("1000.00"), [
            tax("UN81 8.1% Normalsatz", "81.00"),
        ])], [], TEMPLATE_NAMES)
        folder = tempfile.mkdtemp()
        try:
            path = os.path.join(folder, "mwst.csv")
            mr.write_csv(path, t)
            with open(path, encoding="utf-8") as f:
                text = f.read()
        finally:
            shutil.rmtree(folder)
        self.assertIn("303,Normalsatz 8.1 %,1000.00,81.00", text)
        self.assertIn("UN81,81.00", text)


def _match(row, flt):
    """One ERPNext list filter against a fake row: the operators the report uses."""
    field, op, value = flt
    got = row.get(field)
    if op == "=":
        return got == value
    if op == "between":
        return str(value[0]) <= str(got) <= str(value[1])
    if op == "is":
        return bool(got)
    raise ValueError(op)


class FakeErp:
    """Stands in for import_master.Erp: rows per doctype for list(), full documents for get()."""

    def __init__(self, rows, docs):
        self.rows = rows
        self.docs = docs

    def list(self, doctype, filters=None, fields=("name",)):
        return [r for r in self.rows.get(doctype, []) if all(_match(r, f) for f in filters or [])]

    def get(self, doctype, name):
        return self.docs[(doctype, name)]


def invoice(name, doctype, day, net, taxes, grand):
    """An invoice as ERPNext has it: taxes is a list of (template name, tax) pairs."""
    return {"name": name, "doctype": doctype, "posting_date": day, "base_net_total": net, "grand_total": grand,
            "taxes": [tax(template, amount, "Add" if doctype == "Purchase Invoice" else None) for template, amount in taxes]}


def pay(day, references):
    """A Payment Entry of the period as payment_documents() takes it: references are (doctype, name, allocated)."""
    return {"posting_date": day, "references": [
        {"reference_doctype": d, "reference_name": n, "allocated_amount": a} for d, n, a in references]}


# Invented invoices: S1 at 8.1 % (net 1000, tax 81, grand 1081); S2 at 2.6 % (net 500, tax 13, grand 513);
# R1 a credit note of S1's net, 8.1 % (net -200, tax -16.20, grand -216.20); P1 a purchase at 8.1 % (net 500,
# tax 40.50, grand 540.50).
S1 = invoice("S1", "Sales Invoice", datetime.date(2026, 7, 10), D("1000.00"), [("UN81 8.1% Normalsatz", "81.00")], D("1081.00"))
S2 = invoice("S2", "Sales Invoice", datetime.date(2026, 7, 12), D("500.00"), [("UR26 2.6% Reduzierter Satz", "13.00")], D("513.00"))
R1 = invoice("R1", "Sales Invoice", datetime.date(2026, 8, 20), D("-200.00"), [("UN81 8.1% Normalsatz", "-16.20")], D("-216.20"))
P1 = invoice("P1", "Purchase Invoice", datetime.date(2026, 7, 5), D("500.00"), [("VM81 8.1% Normalsatz Material/DL", "40.50")], D("540.50"))
INVOICES = {("Sales Invoice", "S1"): S1, ("Sales Invoice", "S2"): S2, ("Sales Invoice", "R1"): R1,
            ("Purchase Invoice", "P1"): P1}


def paid(payments):
    """The payment basis for the given Payment Entries, as the totals of the form rows."""
    sales, purchases, unallocated = mr.payment_documents(payments, INVOICES)
    return mr.form_totals(mr.plan(sales, purchases, TEMPLATE_NAMES))[0], unallocated


class PaymentBasis(unittest.TestCase):
    def test_receipt_paying_two_rates_is_split_over_them(self):
        rows, unallocated = paid([pay(datetime.date(2026, 8, 3), [
            ("Sales Invoice", "S1", D("1081.00")),
            ("Sales Invoice", "S2", D("513.00")),
        ])])
        self.assertEqual(rows["200"][0], D("1500.00"))
        self.assertEqual(rows["303"], [D("1000.00"), D("81.00")])
        self.assertEqual(rows["313"], [D("500.00"), D("13.00")])
        self.assertEqual(unallocated, 0)

    def test_partial_payment_and_its_rest_fall_in_two_quarters(self):
        q3, _ = paid([pay(datetime.date(2026, 9, 15), [("Sales Invoice", "S1", D("540.50"))])])
        q4, _ = paid([pay(datetime.date(2026, 10, 5), [("Sales Invoice", "S1", D("540.50"))])])
        self.assertEqual(q3["303"], [D("500.00"), D("40.50")])
        self.assertEqual(q4["303"], [D("500.00"), D("40.50")])

    def test_refund_of_a_credit_note_is_negative_and_reduces_the_row(self):
        rows, _ = paid([pay(datetime.date(2026, 8, 25), [("Sales Invoice", "R1", D("-216.20"))])])
        self.assertEqual(rows["303"], [D("-200.00"), D("-16.20")])
        self.assertEqual(rows["399"][1], D("-16.20"))

    def test_partial_refund_is_a_share_of_the_credit_note(self):
        rows, _ = paid([pay(datetime.date(2026, 8, 25), [("Sales Invoice", "R1", D("-108.10"))])])
        self.assertEqual(rows["303"], [D("-100.00"), D("-8.10")])

    def test_purchase_invoice_paid_goes_to_400(self):
        rows, _ = paid([pay(datetime.date(2026, 8, 3), [("Purchase Invoice", "P1", D("540.50"))])])
        self.assertEqual(rows["400"][1], D("40.50"))
        self.assertEqual(rows["420"][1], D("40.50"))

    def test_payment_without_an_invoice_is_counted_apart_and_splits_no_tax(self):
        rows, unallocated = paid([pay(datetime.date(2026, 8, 3), [])])
        self.assertEqual(unallocated, 1)
        self.assertNotIn("200", rows)
        self.assertEqual(rows["399"][1], D("0"))

    def test_zero_grand_total_is_refused_not_divided(self):
        broken = dict(S1, grand_total=D("0"))
        with self.assertRaises(ValueError):
            mr.payment_documents([pay(datetime.date(2026, 8, 3), [("Sales Invoice", "S1", D("10"))])],
                                 {("Sales Invoice", "S1"): broken})


class PaymentPeriods(unittest.TestCase):
    """fetch_payments() reads the period's Payment Entries: the quarter's first and last day are in, the days either side out."""

    def fake(self):
        payments = [
            dict(name="PE-JUN", posting_date="2026-06-30", company=mr.COMPANY, docstatus=1),
            dict(name="PE-JUL", posting_date="2026-07-01", company=mr.COMPANY, docstatus=1),
            dict(name="PE-SEP", posting_date="2026-09-30", company=mr.COMPANY, docstatus=1),
            dict(name="PE-OCT", posting_date="2026-10-01", company=mr.COMPANY, docstatus=1),
            dict(name="PE-DRAFT", posting_date="2026-08-03", company=mr.COMPANY, docstatus=0),
        ]
        docs = {("Payment Entry", p["name"]): {"posting_date": p["posting_date"], "references": [
            {"reference_doctype": "Sales Invoice", "reference_name": "S1", "allocated_amount": D("100.00")}]}
            for p in payments}
        docs[("Sales Invoice", "S1")] = S1
        return FakeErp({"Payment Entry": payments}, docs)

    def test_quarter_takes_its_first_and_last_day_and_only_submitted_payments(self):
        payments, invoices = mr.fetch_payments(self.fake(), datetime.date(2026, 7, 1), datetime.date(2026, 9, 30))
        self.assertEqual(sorted(p["posting_date"] for p in payments),
                         [datetime.date(2026, 7, 1), datetime.date(2026, 9, 30)])
        self.assertIn(("Sales Invoice", "S1"), invoices)


class ReportFiles(unittest.TestCase):
    def test_posting_keeps_its_file_and_payment_gets_its_own(self):
        self.assertEqual(os.path.basename(mr.default_report("2026Q3", "posting")), "mwst-2026Q3.csv")
        self.assertEqual(os.path.basename(mr.default_report("2026Q3", "payment")), "mwst-2026Q3-payment.csv")


class ReportRun(unittest.TestCase):
    """main() on a fake ERPNext: the posting basis is unchanged by the flag, the payment basis reads the payments."""

    def fake(self):
        rows = {
            "Sales Taxes and Charges Template": [{"name": "UN81 8.1% Normalsatz", "bexio_id": "28"}],
            "Purchase Taxes and Charges Template": [],
            "Sales Invoice": [{"name": "S1", "company": mr.COMPANY, "docstatus": 1, "posting_date": "2026-07-10"}],
            "Purchase Invoice": [],
            "Payment Entry": [{"name": "PE1", "company": mr.COMPANY, "docstatus": 1, "posting_date": "2026-08-03"}],
        }
        docs = {("Sales Invoice", "S1"): S1,
                ("Payment Entry", "PE1"): {"posting_date": "2026-08-03", "references": [
                    {"reference_doctype": "Sales Invoice", "reference_name": "S1", "allocated_amount": D("1081.00")}]}}
        return FakeErp(rows, docs)

    def run_main(self, extra):
        folder = tempfile.mkdtemp()
        try:
            path = os.path.join(folder, "mwst.csv")
            out = io.StringIO()
            with mock.patch.object(mr.im.Erp, "from_file", return_value=self.fake()), contextlib.redirect_stdout(out):
                code = mr.main(["--period", "2026Q3", "--report", path] + extra)
            with open(path, encoding="utf-8") as f:
                report = f.read()
        finally:
            shutil.rmtree(folder)
        return code, out.getvalue().replace(path, "<report>"), report

    def test_posting_basis_is_the_default_and_the_flag_leaves_it_unchanged(self):
        default = self.run_main([])
        explicit = self.run_main(["--basis", "posting"])
        self.assertEqual(default, explicit)
        self.assertEqual(default[0], 0)
        self.assertIn("MWST 2026Q3 (2026-07-01 to 2026-09-30): 1 sales and 0 purchase invoices", default[1])

    def test_payment_basis_counts_the_payment_in_its_quarter(self):
        code, out, report = self.run_main(["--basis", "payment"])
        self.assertEqual(code, 0)
        self.assertIn("MWST 2026Q3 (2026-07-01 to 2026-09-30, payment basis): 1 payments, 0 of them pay no invoice", out)
        self.assertIn("303,Normalsatz 8.1 %,1000.00,81.00", report)



if __name__ == "__main__":
    unittest.main()
