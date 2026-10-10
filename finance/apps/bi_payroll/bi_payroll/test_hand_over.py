"""Offline tests of the payroll hand-over rows, with an invented employee. No frappe needed:

    python3 -m unittest bi_payroll.test_hand_over

from this app's directory. The slips and their Salary Detail lines are made here, the way the report reads them.
"""

import unittest

from bi_payroll.hand_over import AMOUNTS, COMPONENTS, LOHNAUSWEIS, hand_over, lohnausweis


def slip(name, month, gross, net, employee="EMP-TEST-001"):
    return {
        "name": name, "employee": employee, "employee_name": "Test Employee", "ahv_number": "756.0000.0000.00",
        "month": month, "gross_pay": gross, "net_pay": net,
    }


# An invented month: gross 8000, the employee's deductions 424 AHV, 88 ALV, 300 BVG, 40 NBU, 35 KTG, so the net
# is 7113. The employer's shares are not in the net (the components are not in the slip's total).
LINES = {
    "SAL-1": {
        "Basic Salary": 8000,
        "AHV/IV/EO Employee": 424, "ALV Employee": 88, "BVG Employee": 300, "NBU Employee": 40, "KTG Employee": 35,
        "AHV/IV/EO Employer": 424, "ALV Employer": 88, "BVG Employer": 300, "UVG Employer": 30, "KTG Employer": 35,
        "FAK Employer": 60,
    },
}
MONTH_1 = slip("SAL-1", 1, 8000, 7113)


class OneRowPerEmployee(unittest.TestCase):
    def test_slips_of_the_year_add_up_to_one_row(self):
        slips = [slip("SAL-1", 1, 8000, 7113), slip("SAL-2", 2, 8000, 7113)]
        lines = {"SAL-1": LINES["SAL-1"], "SAL-2": LINES["SAL-1"]}
        (row,) = hand_over(slips, lines, by_month=False)
        self.assertEqual(row["slips"], 2)
        self.assertEqual(row["gross"], 16000)
        self.assertEqual(row["net"], 14226)
        self.assertEqual(row["ahv_employee"], 848)
        self.assertIsNone(row["month"])

    def test_each_employee_has_a_row(self):
        slips = [slip("SAL-1", 1, 8000, 7113), slip("SAL-3", 1, 5000, 4400, employee="EMP-TEST-002")]
        lines = {"SAL-1": LINES["SAL-1"], "SAL-3": {}}
        rows = hand_over(slips, lines, by_month=False)
        self.assertEqual([r["employee"] for r in rows], ["EMP-TEST-001", "EMP-TEST-002"])
        self.assertEqual(rows[1]["ahv_employee"], 0)


class OneRowPerMonth(unittest.TestCase):
    def test_each_month_of_an_employee_has_its_own_row(self):
        slips = [slip("SAL-1", 1, 8000, 7113), slip("SAL-2", 2, 8100, 7200)]
        lines = {"SAL-1": LINES["SAL-1"], "SAL-2": LINES["SAL-1"]}
        rows = hand_over(slips, lines, by_month=True)
        self.assertEqual([r["month"] for r in rows], [1, 2])
        self.assertEqual([r["gross"] for r in rows], [8000, 8100])


class Components(unittest.TestCase):
    def test_each_swiss_component_lands_in_its_own_column(self):
        (row,) = hand_over([MONTH_1], LINES, by_month=False)
        self.assertEqual(row["ahv_employee"], 424)
        self.assertEqual(row["alv_employee"], 88)
        self.assertEqual(row["bvg_employee"], 300)
        self.assertEqual(row["nbu_employee"], 40)
        self.assertEqual(row["ktg_employee"], 35)
        self.assertEqual(row["uvg_employer"], 30)
        self.assertEqual(row["fak_employer"], 60)

    def test_the_basic_salary_is_an_amount_of_its_own(self):
        (row,) = hand_over([MONTH_1], LINES, by_month=False)
        self.assertEqual(row["basic"], 8000)

    def test_quellensteuer_is_zero_on_a_slip_without_its_row(self):
        (row,) = hand_over([MONTH_1], LINES, by_month=False)
        self.assertEqual(row["quellensteuer"], 0)

    def test_quellensteuer_lands_in_its_column_from_the_fixture_component(self):
        lines = {"SAL-1": dict(LINES["SAL-1"], **{"Quellensteuer Employee": 250})}
        (row,) = hand_over([MONTH_1], lines, by_month=False)
        self.assertEqual(row["quellensteuer"], 250)

    def test_basic_salary_and_unknown_components_are_not_columns(self):
        (row,) = hand_over([MONTH_1], LINES, by_month=False)
        self.assertNotIn("Basic Salary", row)
        self.assertNotIn("basic_salary", row)

    def test_employer_total_is_the_employer_shares_only(self):
        (row,) = hand_over([MONTH_1], LINES, by_month=False)
        self.assertEqual(row["employer_total"], 424 + 88 + 300 + 30 + 35 + 60)

    def test_the_components_are_the_fixture_names(self):
        self.assertEqual(COMPONENTS["ahv_employee"], "AHV/IV/EO Employee")
        self.assertEqual(COMPONENTS["fak_employer"], "FAK Employer")
        self.assertEqual(COMPONENTS["quellensteuer"], "Quellensteuer Employee")


class Lohnausweis(unittest.TestCase):
    def line(self, lines=LINES):
        (row,) = hand_over([MONTH_1], lines, by_month=False)
        (line,) = lohnausweis([row])
        return line

    def test_line_1_is_the_basic_salary(self):
        self.assertEqual(self.line()["line_1"], 8000)

    def test_line_8_and_11_are_the_slips_gross_and_net(self):
        line = self.line()
        self.assertEqual(line["line_8"], 8000)
        self.assertEqual(line["line_11"], 7113)

    def test_line_9_is_the_employee_ahv_alv_and_nbu(self):
        self.assertEqual(self.line()["line_9"], 424 + 88 + 40)

    def test_line_10_1_is_the_employee_bvg(self):
        self.assertEqual(self.line()["line_10_1"], 300)

    def test_line_12_is_the_quellensteuer_employee_row(self):
        self.assertEqual(self.line()["line_12"], 0)
        lines = {"SAL-1": dict(LINES["SAL-1"], **{"Quellensteuer Employee": 250})}
        self.assertEqual(self.line(lines)["line_12"], 250)

    def test_ktg_employee_keeps_a_column_until_it_has_a_line(self):
        self.assertEqual(self.line()["ktg_employee"], 35)

    def test_the_rows_keep_the_employee_and_the_slip_count(self):
        line = self.line()
        self.assertEqual((line["employee"], line["slips"], line["month"]), ("EMP-TEST-001", 1, None))

    def test_one_row_per_hand_over_row(self):
        slips = [slip("SAL-1", 1, 8000, 7113), slip("SAL-2", 2, 8100, 7200)]
        lines = {"SAL-1": LINES["SAL-1"], "SAL-2": LINES["SAL-1"]}
        rows = lohnausweis(hand_over(slips, lines, by_month=True))
        self.assertEqual([r["month"] for r in rows], [1, 2])
        self.assertEqual([r["line_8"] for r in rows], [8000, 8100])

    def test_every_column_sums_amounts_the_rows_carry(self):
        for column, _, keys in LOHNAUSWEIS:
            with self.subTest(column=column):
                self.assertTrue(set(keys) <= set(AMOUNTS))


class Payslips(unittest.TestCase):
    def test_net_is_gross_less_the_employee_deductions(self):
        (row,) = hand_over([MONTH_1], LINES, by_month=False)
        employee = sum(row[k] for k in ["ahv_employee", "alv_employee", "bvg_employee", "nbu_employee", "ktg_employee"])
        self.assertEqual(row["gross"] - employee, row["net"])

    def test_the_slips_net_is_taken_as_it_is(self):
        # the net is the slip's own, not recomputed here: a change of the slip shows in the report
        (row,) = hand_over([slip("SAL-1", 1, 8000, 7000)], LINES, by_month=False)
        self.assertEqual(row["net"], 7000)

    def test_amounts_are_rounded_to_centimes(self):
        lines = {"SAL-1": {"AHV/IV/EO Employee": 0.1 + 0.2}}
        (row,) = hand_over([MONTH_1], lines, by_month=False)
        self.assertEqual(row["ahv_employee"], 0.3)


if __name__ == "__main__":
    unittest.main()
