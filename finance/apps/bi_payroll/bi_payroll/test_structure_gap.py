"""Offline tests of the Salary Structure that misses a component, with a stand-in for frappe and invented figures:

    python3 -m unittest bi_payroll.test_structure_gap

from this app's directory. They cover what the throwaway-site check (erp-5t6q) found: a structure made before the
Quellensteuer row existed stops a slip that has a tax, and ensure_structure logs the gap instead of leaving it silent.
They also cover the replacement of an unused structure that is defective (erp-d74t): replaced, left alone when in use.
"""

import importlib
import datetime
import sys
import types
import unittest
from unittest import mock

from bi_payroll.swiss_rates import STRUCTURE_DEDUCTIONS, STRUCTURE_EARNINGS

QST = "Quellensteuer Employee"
# an invented bracket: 4950 up to 5050, 4.00 percent, no minimum tax
BRACKET = {
    "canton": "ZG", "tariff_code": "B2N", "valid_from": datetime.date(2026, 1, 1),
    "income_from": 4950.0, "step": 100.0, "minimum_tax": 0.0, "rate": 4.0,
}


class Thrown(Exception):
    pass


class FakeDb:
    # in_use: a slip or an assignment names the structure (True keeps the older tests' meaning, a structure in use)
    def __init__(self, structure_has_row, employee=("ZG", "B2N"), in_use=True):
        self.structure_has_row = structure_has_row
        self.employee = employee
        self.in_use = in_use

    def get_value(self, doctype, name, fields=None, as_dict=False, **kwargs):
        if doctype == "Employee":
            return self.employee
        if doctype == "Salary Component":
            # every component has a formula, named by it, so a row's formula can be checked against it;
            # statistical_component 0: none of these components is statistical (the employer shares are
            # test_swiss_payroll's subject), and structure_rows copies the flag onto every row
            formula = f"FORMULA-{name}"
            return types.SimpleNamespace(
                formula=formula, amount_based_on_formula=1, statistical_component=0
            ) if as_dict else formula
        return "SS-1" if doctype == "Salary Structure" else None

    def exists(self, doctype, filters=None):
        if doctype == "Salary Detail":
            return self.structure_has_row
        if doctype in ("Salary Slip", "Salary Structure Assignment"):
            return self.in_use
        return True


class FakeSlip(dict):
    def __init__(self, gross):
        super().__init__()
        self.update(employee="EMP-TEST-001", gross_pay=gross, end_date="2026-03-31", salary_structure="SS-1")
        self.swiss_qst_amount = 0
        self.calculated = 0

    def get(self, key, default=None):
        return getattr(self, key, None) if key == "swiss_qst_amount" else super().get(key, default)

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)

    def calculate_net_pay(self):
        self.calculated += 1


def fake_frappe(db):
    frappe = types.ModuleType("frappe")
    frappe.db = db

    def throw(message):
        raise Thrown(message)

    frappe.throw = throw
    frappe.get_all = lambda doctype, **kwargs: (
        ["Beispiel Test AG"] if doctype == "Company" else [dict(BRACKET)]
    )
    frappe.log_error = mock.Mock()
    frappe.get_cached_value = lambda doctype, name, field: "EUR"
    frappe.delete_doc = mock.Mock()
    utils = types.ModuleType("frappe.utils")
    utils.flt = lambda value, precision=None: float(value or 0)
    utils.getdate = lambda value: datetime.date.fromisoformat(str(value))
    frappe.utils = utils
    return frappe, utils


def rows(names, formulas=True):
    # structure rows as the database gives them: each with its component's formula, or without one
    return [types.SimpleNamespace(salary_component=n, formula=f"FORMULA-{n}" if formulas else "") for n in names]


class StructureGap(unittest.TestCase):
    def load(self, structure_has_row, in_use=True):
        frappe, utils = fake_frappe(FakeDb(structure_has_row, in_use=in_use))
        patcher = mock.patch.dict(sys.modules, {"frappe": frappe, "frappe.utils": utils})
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("bi_payroll.swiss_payroll", None)
        self.addCleanup(sys.modules.pop, "bi_payroll.swiss_payroll", None)
        return importlib.import_module("bi_payroll.swiss_payroll"), frappe

    def test_a_slip_with_a_tax_stops_when_the_structure_has_no_row(self):
        module, _ = self.load(structure_has_row=False)
        slip = FakeSlip(gross=5000)
        with self.assertRaises(Thrown) as caught:
            module.withhold_qst(slip)
        self.assertIn("has no Quellensteuer Employee row", str(caught.exception))
        self.assertIn("SS-1", str(caught.exception))
        self.assertEqual(slip.calculated, 0)

    def test_a_slip_with_a_tax_is_computed_again_when_the_structure_has_the_row(self):
        module, _ = self.load(structure_has_row=True)
        slip = FakeSlip(gross=5000)
        module.withhold_qst(slip)
        self.assertEqual(slip.swiss_qst_amount, 200.0)  # 5000 x 4.00 percent
        self.assertEqual(slip.calculated, 1)
        module.withhold_qst(slip)  # saved again: nothing changes, no second calculation
        self.assertEqual(slip.swiss_qst_amount, 200.0)
        self.assertEqual(slip.calculated, 1)

    def test_a_slip_without_a_tax_is_not_stopped_by_a_structure_without_the_row(self):
        module, frappe = self.load(structure_has_row=False)
        frappe.db.employee = (None, None)
        slip = FakeSlip(gross=5000)
        module.withhold_qst(slip)
        self.assertEqual(slip.swiss_qst_amount, 0)

    def test_ensure_structure_logs_a_structure_that_lacks_the_row_and_leaves_it(self):
        module, frappe = self.load(structure_has_row=False)
        structure = types.SimpleNamespace(
            earnings=rows(STRUCTURE_EARNINGS), deductions=rows([c for c in STRUCTURE_DEDUCTIONS if c != QST]),
        )
        frappe.get_doc = mock.Mock(return_value=structure)
        module.ensure_structure()
        frappe.log_error.assert_called_once()
        self.assertIn(QST, frappe.log_error.call_args.kwargs["message"])
        frappe.get_doc.assert_called_once_with("Salary Structure", "SS-1")  # read, never made or amended

    def test_ensure_structure_is_silent_for_a_complete_structure(self):
        module, frappe = self.load(structure_has_row=True)
        frappe.get_doc = mock.Mock(
            return_value=types.SimpleNamespace(earnings=rows(STRUCTURE_EARNINGS), deductions=rows(STRUCTURE_DEDUCTIONS))
        )
        module.ensure_structure()
        frappe.log_error.assert_not_called()


class Replacing(unittest.TestCase):
    def setUp(self):
        self.made = []  # the new structures, each as the dict ensure_structure gives frappe.get_doc

    def run_ensure(self, earnings, deductions, in_use=False, docstatus=0):
        # one run of ensure_structure on the company's structure, with the given rows and use
        structure = types.SimpleNamespace(
            name="SS-1", docstatus=docstatus, earnings=earnings, deductions=deductions, cancel=mock.Mock(),
        )
        frappe = self.install(in_use)
        frappe.get_doc = mock.Mock(side_effect=lambda arg, *args: structure if arg == "Salary Structure" else self.new(arg))
        self.module.ensure_structure()
        return structure, frappe

    def install(self, in_use):
        frappe, utils = fake_frappe(FakeDb(structure_has_row=True, in_use=in_use))
        patcher = mock.patch.dict(sys.modules, {"frappe": frappe, "frappe.utils": utils})
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("bi_payroll.swiss_payroll", None)
        self.addCleanup(sys.modules.pop, "bi_payroll.swiss_payroll", None)
        self.module = importlib.import_module("bi_payroll.swiss_payroll")
        return frappe

    def new(self, spec):
        self.made.append(spec)
        return mock.Mock()

    def test_an_unused_structure_without_formulas_is_replaced_with_them(self):
        structure, frappe = self.run_ensure(
            rows(STRUCTURE_EARNINGS, formulas=False), rows(STRUCTURE_DEDUCTIONS, formulas=False),
        )
        frappe.delete_doc.assert_called_once_with("Salary Structure", "SS-1")
        self.assertEqual(len(self.made), 1)
        new = self.made[0]
        self.assertEqual(new["name"], "Swiss Monthly Beispiel Test AG")
        self.assertEqual([r["formula"] for r in new["deductions"]], [f"FORMULA-{c}" for c in STRUCTURE_DEDUCTIONS])
        self.assertEqual([r["formula"] for r in new["earnings"]], [f"FORMULA-{c}" for c in STRUCTURE_EARNINGS])
        self.assertEqual(new["deductions"][-1]["salary_component"], QST)
        frappe.log_error.assert_called_once()
        self.assertEqual(frappe.log_error.call_args.kwargs["title"], "Swiss Salary Structure replaced")
        self.assertIn("0 missing components and 14 rows without a formula", frappe.log_error.call_args.kwargs["message"])

    def test_an_unused_structure_without_a_component_is_replaced_with_it(self):
        structure, frappe = self.run_ensure(
            rows(STRUCTURE_EARNINGS), rows([c for c in STRUCTURE_DEDUCTIONS if c != QST]),
        )
        frappe.delete_doc.assert_called_once_with("Salary Structure", "SS-1")
        self.assertEqual(self.made[0]["deductions"][-1]["salary_component"], QST)
        self.assertEqual(frappe.log_error.call_args.kwargs["title"], "Swiss Salary Structure replaced")
        self.assertIn("1 missing components and 0 rows without a formula", frappe.log_error.call_args.kwargs["message"])

    def test_a_submitted_unused_structure_is_cancelled_before_it_is_deleted(self):
        structure, frappe = self.run_ensure(
            rows(STRUCTURE_EARNINGS, formulas=False), rows(STRUCTURE_DEDUCTIONS), docstatus=1,
        )
        structure.cancel.assert_called_once_with()
        frappe.delete_doc.assert_called_once_with("Salary Structure", "SS-1")

    def test_a_structure_in_use_is_logged_and_not_replaced(self):
        structure, frappe = self.run_ensure(
            rows(STRUCTURE_EARNINGS), rows([c for c in STRUCTURE_DEDUCTIONS if c != QST]), in_use=True,
        )
        frappe.delete_doc.assert_not_called()
        self.assertEqual(self.made, [])
        structure.cancel.assert_not_called()
        frappe.log_error.assert_called_once()
        self.assertEqual(frappe.log_error.call_args.kwargs["title"], "Swiss Salary Structure lacks components")
        self.assertIn(QST, frappe.log_error.call_args.kwargs["message"])

    def test_a_structure_in_use_without_formulas_is_logged_and_not_replaced(self):
        structure, frappe = self.run_ensure(
            rows(STRUCTURE_EARNINGS, formulas=False), rows(STRUCTURE_DEDUCTIONS, formulas=False), in_use=True,
        )
        frappe.delete_doc.assert_not_called()
        self.assertEqual(self.made, [])
        frappe.log_error.assert_called_once()
        self.assertIn("has 14 rows without their component's formula", frappe.log_error.call_args.kwargs["message"])

    def test_a_complete_structure_with_formulas_is_untouched(self):
        structure, frappe = self.run_ensure(rows(STRUCTURE_EARNINGS), rows(STRUCTURE_DEDUCTIONS))
        frappe.delete_doc.assert_not_called()
        self.assertEqual(self.made, [])
        frappe.log_error.assert_not_called()

    def test_a_replaced_structure_is_untouched_when_it_runs_again(self):
        self.run_ensure(rows(STRUCTURE_EARNINGS, formulas=False), rows(STRUCTURE_DEDUCTIONS, formulas=False))
        self.assertEqual(len(self.made), 1)
        # the structure the first run made, as the database gives it back
        made = self.made[0]
        replaced = types.SimpleNamespace(
            name="SS-1", docstatus=0,
            earnings=[types.SimpleNamespace(**r) for r in made["earnings"]],
            deductions=[types.SimpleNamespace(**r) for r in made["deductions"]],
            cancel=mock.Mock(),
        )
        frappe = self.install(in_use=False)
        frappe.get_doc = mock.Mock(side_effect=lambda arg, *args: replaced if arg == "Salary Structure" else self.new(arg))
        self.module.ensure_structure()
        frappe.delete_doc.assert_not_called()
        frappe.log_error.assert_not_called()
        self.assertEqual(len(self.made), 1)  # no second structure


if __name__ == "__main__":
    unittest.main()
