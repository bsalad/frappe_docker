"""Offline tests of the Salary Structure that misses a component, with a stand-in for frappe and invented figures:

    python3 -m unittest bi_payroll.test_structure_gap

from this app's directory. They cover what the throwaway-site check (erp-5t6q) found: a structure made before the
Quellensteuer row existed stops a slip that has a tax, and ensure_structure logs the gap instead of leaving it silent.
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
    def __init__(self, structure_has_row, employee=("ZG", "B2N")):
        self.structure_has_row = structure_has_row
        self.employee = employee

    def get_value(self, doctype, name, fields=None, **kwargs):
        if doctype == "Employee":
            return self.employee
        return "SS-1" if doctype == "Salary Structure" else None

    def exists(self, doctype, filters=None):
        return self.structure_has_row if doctype == "Salary Detail" else True


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
    utils = types.ModuleType("frappe.utils")
    utils.flt = lambda value, precision=None: float(value or 0)
    utils.getdate = lambda value: datetime.date.fromisoformat(str(value))
    frappe.utils = utils
    return frappe, utils


class StructureGap(unittest.TestCase):
    def load(self, structure_has_row):
        frappe, utils = fake_frappe(FakeDb(structure_has_row))
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
        rows = lambda names: [types.SimpleNamespace(salary_component=n) for n in names]
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
        rows = lambda names: [types.SimpleNamespace(salary_component=n) for n in names]
        frappe.get_doc = mock.Mock(
            return_value=types.SimpleNamespace(earnings=rows(STRUCTURE_EARNINGS), deductions=rows(STRUCTURE_DEDUCTIONS))
        )
        module.ensure_structure()
        frappe.log_error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
