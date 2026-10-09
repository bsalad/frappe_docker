"""Offline tests for mwst_report.py. Invented documents only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import ast
import datetime
import os
import shutil
import tempfile
import unittest
from decimal import Decimal

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



if __name__ == "__main__":
    unittest.main()
