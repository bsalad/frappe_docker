"""Offline tests for the pure parts of swiss-setup.py: no site, no network.

Run with: python3 -m unittest discover -s finance/scripts -p 'test_*.py'
"""

import importlib.util
import json
import os
import sys
import unittest
from unittest import mock

# swiss-setup.py imports frappe, which only exists in the backend container.
# The pure helpers under test never touch it.
sys.modules.setdefault("frappe", mock.MagicMock())
sys.modules.setdefault("frappe.utils", mock.MagicMock())

_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "swiss-setup.py")
_spec = importlib.util.spec_from_file_location("swiss_setup", _path)
swiss_setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(swiss_setup)


class DeleteRevokesTest(unittest.TestCase):
    def test_revokes_every_role_but_the_kept_one(self):
        perms = [
            ("Accounts Manager", 0, 1),
            ("Accounts User", 0, 1),
            ("System Manager", 0, 1),
        ]
        self.assertEqual(
            swiss_setup.delete_revokes(perms, keep=["Accounts Manager"]),
            [("Accounts User", 0), ("System Manager", 0)],
        )

    def test_roles_without_delete_are_left_alone(self):
        perms = [("Accounts User", 0, 0), ("Accounts Manager", 0, 1)]
        self.assertEqual(swiss_setup.delete_revokes(perms, keep=["Accounts Manager"]), [])

    def test_empty_when_nothing_may_delete(self):
        # A doctype where no role but the kept one can delete: nothing to revoke.
        self.assertEqual(swiss_setup.delete_revokes([("Sales User", 0, 0)], keep=["Accounts Manager"]), [])

    def test_revokes_on_every_permlevel(self):
        # The kept role is matched by name, whatever the permlevel.
        perms = [("Accounts User", 1, 1)]
        self.assertEqual(swiss_setup.delete_revokes(perms, keep=["Accounts Manager"]), [("Accounts User", 1)])


class ParseFreezeTest(unittest.TestCase):
    def test_date_alone_is_a_dry_run(self):
        self.assertEqual(swiss_setup.parse_freeze(["2025-12-31"]), ("2025-12-31", False))

    def test_apply_flag_after_the_date(self):
        self.assertEqual(swiss_setup.parse_freeze(["2025-12-31", "--apply"]), ("2025-12-31", True))

    def test_apply_flag_before_the_date(self):
        self.assertEqual(swiss_setup.parse_freeze(["--apply", "2025-12-31"]), ("2025-12-31", True))

    def test_rejects_a_missing_date(self):
        with self.assertRaises(SystemExit):
            swiss_setup.parse_freeze([])

    def test_rejects_only_the_flag(self):
        with self.assertRaises(SystemExit):
            swiss_setup.parse_freeze(["--apply"])

    def test_rejects_two_dates(self):
        with self.assertRaises(SystemExit):
            swiss_setup.parse_freeze(["2025-12-31", "2026-12-31"])

    def test_rejects_a_repeated_flag(self):
        with self.assertRaises(SystemExit):
            swiss_setup.parse_freeze(["2025-12-31", "--apply", "--apply"])

    def test_rejects_something_that_is_not_a_date(self):
        with self.assertRaises(SystemExit):
            swiss_setup.parse_freeze(["31.12.2025"])

    def test_rejects_an_impossible_date(self):
        with self.assertRaises(SystemExit):
            swiss_setup.parse_freeze(["2025-02-30"])


class StepsTest(unittest.TestCase):
    def test_gebuev_is_a_step_and_freeze_is_not(self):
        self.assertIn("gebuev", swiss_setup.STEPS)
        self.assertNotIn("freeze", swiss_setup.STEPS)

    def test_treasury_is_not_a_step_so_no_run_writes_it_unseen(self):
        self.assertNotIn("treasury", swiss_setup.STEPS)


class TreasuryDocsTest(unittest.TestCase):
    # Invented account names, as the site would hold them.
    ACCOUNTS = ["1000 - Test Cash - bic", "1020 - Test Bank - bic"]

    def docs(self, accounts=ACCOUNTS):
        return swiss_setup.treasury_docs(accounts)

    def by_doctype(self, docs, doctype):
        return [d for d in docs if d["doctype"] == doctype]

    def test_one_number_card_per_account_after_the_total(self):
        cards = self.by_doctype(self.docs(), "Number Card")
        self.assertEqual([c["name"] for c in cards], [
            swiss_setup.TREASURY_TOTAL,
            "Cash Position 1000 - Test Cash - bic",
            "Cash Position 1020 - Test Bank - bic",
        ])

    def test_account_card_filters_on_its_account_and_the_total_does_not(self):
        cards = {c["name"]: json.loads(c["filters_json"]) for c in self.by_doctype(self.docs(), "Number Card")}
        self.assertNotIn("account", cards[swiss_setup.TREASURY_TOTAL])
        self.assertEqual(cards["Cash Position 1020 - Test Bank - bic"]["account"], "1020 - Test Bank - bic")

    def test_cards_sum_the_chf_column_of_the_report(self):
        for card in self.by_doctype(self.docs(), "Number Card"):
            self.assertEqual(card["type"], "Report")
            self.assertEqual(card["report_name"], "Cash Position")
            self.assertEqual(card["report_field"], "balance_chf")
            self.assertEqual(card["function"], "Sum")
            self.assertEqual(card["report_function"], "Sum")

    def test_the_chart_is_the_report_chart(self):
        (chart,) = self.by_doctype(self.docs(), "Dashboard Chart")
        self.assertEqual(chart["name"], chart["chart_name"])
        self.assertEqual(chart["chart_type"], "Report")
        self.assertEqual(chart["report_name"], "Cash Position")
        self.assertEqual(chart["use_report_chart"], 1)

    def test_workspace_layout_has_a_block_for_each_card_and_the_chart(self):
        (ws,) = self.by_doctype(self.docs(), "Workspace")
        blocks = json.loads(ws["content"])
        cards = [b["data"]["number_card_name"] for b in blocks if b["type"] == "number_card"]
        charts = [b["data"]["chart_name"] for b in blocks if b["type"] == "chart"]
        self.assertEqual(cards, [c["name"] for c in self.by_doctype(self.docs(), "Number Card")])
        self.assertEqual(charts, [swiss_setup.TREASURY_CHART])
        self.assertEqual([r["number_card_name"] for r in ws["number_cards"]], cards)
        self.assertEqual([r["chart_name"] for r in ws["charts"]], charts)

    def test_block_ids_are_unique_and_the_same_on_every_run(self):
        first = json.loads(self.by_doctype(self.docs(), "Workspace")[0]["content"])
        second = json.loads(self.by_doctype(self.docs(), "Workspace")[0]["content"])
        self.assertEqual(first, second)
        ids = [b["id"] for b in first]
        self.assertEqual(len(ids), len(set(ids)))

    def test_no_accounts_leaves_the_total_card_and_the_chart(self):
        docs = self.docs(accounts=[])
        self.assertEqual([c["name"] for c in self.by_doctype(docs, "Number Card")], [swiss_setup.TREASURY_TOTAL])
        self.assertEqual(len(json.loads(self.by_doctype(docs, "Workspace")[0]["content"])), 2)

    def test_the_chart_covers_every_account_so_its_filter_names_none(self):
        (chart,) = self.by_doctype(self.docs(), "Dashboard Chart")
        self.assertEqual(json.loads(chart["filters_json"]), {"company": swiss_setup.COMPANY})


class TreasuryChangesTest(unittest.TestCase):
    class Doc(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    def test_no_change_when_the_saved_doc_matches(self):
        want = {"doctype": "Number Card", "name": "n", "label": "L", "filters_json": "{}", "rows": [{"a": 1}]}
        doc = self.Doc(label="L", filters_json="{}", rows=[{"a": 1, "extra": 9}])
        self.assertEqual(swiss_setup.treasury_changes(doc, want), [])

    def test_an_extra_saved_row_is_a_change(self):
        want = {"doctype": "Workspace", "name": "w", "charts": [{"chart_name": "c"}]}
        doc = self.Doc(charts=[{"chart_name": "c"}, {"chart_name": "old"}])
        self.assertEqual(swiss_setup.treasury_changes(doc, want), ["charts"])

    def test_reports_each_field_and_child_table_that_differs(self):
        want = {"doctype": "Workspace", "name": "w", "content": "[1]", "charts": [{"chart_name": "c"}]}
        doc = self.Doc(content="[]", charts=[{"chart_name": "other"}])
        self.assertEqual(swiss_setup.treasury_changes(doc, want), ["content", "charts"])


# Invented IBANs and account names only: the test data is not company data.
TEST_IBAN = "CH0000000000000000001"
TEST_IBAN_WISE = "CH0000000000000000002"
UBS_BIC = "UBSWCHZH80A"


class PaymentFixesTest(unittest.TestCase):
    def test_copies_iban_to_gl_account_and_sets_public_bic(self):
        # The live case: Bank Account with an IBAN, empty GL Account and an empty Bank.
        bank_accounts = [{"name": "UBS KK", "account": "1020 UBS", "iban": TEST_IBAN, "bank": "UBS Switzerland AG"}]
        fixes = swiss_setup.payment_fixes(bank_accounts, {"1020 UBS": {"iban": None, "bic": None}}, {"UBS Switzerland AG": None})
        self.assertEqual(fixes, [
            ("Account", "1020 UBS", "iban", TEST_IBAN),
            ("Bank", "UBS Switzerland AG", "swift_number", UBS_BIC),
            ("Account", "1020 UBS", "bic", UBS_BIC),
        ])

    def test_second_run_changes_nothing(self):
        # Once the fixes are applied, the next run finds every field set.
        bank_accounts = [{"name": "UBS KK", "account": "1020 UBS", "iban": TEST_IBAN, "bank": "UBS Switzerland AG"}]
        gl = {"1020 UBS": {"iban": TEST_IBAN, "bic": UBS_BIC}}
        self.assertEqual(swiss_setup.payment_fixes(bank_accounts, gl, {"UBS Switzerland AG": UBS_BIC}), [])

    def test_a_value_entered_by_hand_is_not_overwritten(self):
        bank_accounts = [{"name": "UBS KK", "account": "1020 UBS", "iban": TEST_IBAN, "bank": "UBS Switzerland AG"}]
        gl = {"1020 UBS": {"iban": "CH0000000000000000009", "bic": "OTHERBIC"}}
        self.assertEqual(swiss_setup.payment_fixes(bank_accounts, gl, {"UBS Switzerland AG": "OTHERBIC"}), [])

    def test_bank_swift_number_kept_and_copied_to_gl_bic(self):
        # A Bank that already has a swift number keeps it; the GL Account takes it.
        bank_accounts = [{"name": "UBS KK", "account": "1020 UBS", "iban": TEST_IBAN, "bank": "UBS Switzerland AG"}]
        gl = {"1020 UBS": {"iban": TEST_IBAN, "bic": None}}
        self.assertEqual(swiss_setup.payment_fixes(bank_accounts, gl, {"UBS Switzerland AG": UBS_BIC}),
                         [("Account", "1020 UBS", "bic", UBS_BIC)])

    def test_bank_without_public_bic_gets_only_the_iban(self):
        # Wise has no BIC in the table: its IBAN is copied, nothing else is set.
        bank_accounts = [{"name": "Wise", "account": "1030 Wise", "iban": TEST_IBAN_WISE, "bank": "WISE PAYMENTS LIMITED"}]
        gl = {"1030 Wise": {"iban": None, "bic": None}}
        self.assertEqual(swiss_setup.payment_fixes(bank_accounts, gl, {"WISE PAYMENTS LIMITED": None}),
                         [("Account", "1030 Wise", "iban", TEST_IBAN_WISE)])

    def test_bank_account_without_iban_or_gl_account_is_skipped(self):
        bank_accounts = [
            {"name": "no iban", "account": "1040 X", "iban": None, "bank": "UBS Switzerland AG"},
            {"name": "no gl", "account": None, "iban": TEST_IBAN, "bank": "UBS Switzerland AG"},
        ]
        self.assertEqual(swiss_setup.payment_fixes(bank_accounts, {}, {"UBS Switzerland AG": None}), [])

    def test_two_bank_accounts_on_one_gl_account_give_one_fix(self):
        bank_accounts = [
            {"name": "a", "account": "1020 UBS", "iban": TEST_IBAN, "bank": "UBS Switzerland AG"},
            {"name": "b", "account": "1020 UBS", "iban": TEST_IBAN, "bank": "UBS Switzerland AG"},
        ]
        fixes = swiss_setup.payment_fixes(bank_accounts, {"1020 UBS": {"iban": None, "bic": None}}, {"UBS Switzerland AG": None})
        self.assertEqual(len(fixes), len(set(fixes)))
        self.assertEqual(len(fixes), 3)


if __name__ == "__main__":
    unittest.main()
