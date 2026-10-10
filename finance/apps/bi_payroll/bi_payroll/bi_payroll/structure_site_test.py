"""The Swiss Salary Structure rows carry their components' formulas (erp-fs9c: a row without one paid 0).

Needs a site with bi_payroll installed (frappe), so it is not a test_*.py: the offline gate's discover skips it.
    bench --site <site> run-tests --module bi_payroll.bi_payroll.structure_site_test
"""

import frappe
from frappe.tests import IntegrationTestCase

from bi_payroll.swiss_payroll import STRUCTURE_DEDUCTIONS, STRUCTURE_EARNINGS, structure_row


class TestStructure(IntegrationTestCase):
    def test_a_row_carries_the_component_formula(self):
        row = structure_row("Basic Salary")
        self.assertEqual(row["formula"], "base")
        self.assertEqual(row["amount_based_on_formula"], 1)
        self.assertEqual(row["formula"], frappe.db.get_value("Salary Component", "Basic Salary", "formula"))

    def test_every_row_of_the_structure_has_a_formula(self):
        for component in STRUCTURE_EARNINGS + STRUCTURE_DEDUCTIONS:
            with self.subTest(component=component):
                self.assertTrue(structure_row(component)["formula"])

    def test_an_employer_share_is_not_a_statistical_row(self):
        # erp-fs9c: a statistical row is kept off the slip, so the hand-over's employer columns were 0
        for component in ["AHV/IV/EO Employer", "ALV Employer", "BVG Employer", "UVG Employer", "KTG Employer", "FAK Employer"]:
            with self.subTest(component=component):
                self.assertEqual(structure_row(component)["statistical_component"], 0)
