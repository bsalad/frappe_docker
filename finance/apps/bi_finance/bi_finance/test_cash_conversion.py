"""Offline tests for cash_conversion.py: the months, the account groups, DSO/DPO/DIO/CCC and the days to pay.

Invented numbers and invented accounts only; no site, no network, no frappe:

    python3 -m unittest -v bi_finance.test_cash_conversion        (from finance/apps/bi_finance)
"""

import datetime
import unittest
from types import SimpleNamespace

from bi_finance import cash_conversion as cc

D = datetime.date


def account(name, root, kind, number=""):
    return SimpleNamespace(name=name, root_type=root, account_type=kind, account_number=number)


def flat(value, count):
    return [value] * count


class Months(unittest.TestCase):
    def test_month_ends_from_the_first_month_up_to_the_last_full_month(self):
        self.assertEqual(
            cc.month_ends(D(2019, 1, 1), D(2019, 3, 15)),
            [D(2019, 1, 31), D(2019, 2, 28)],
        )

    def test_leap_february_has_29_days(self):
        self.assertEqual(cc.month_ends(D(2020, 2, 1), D(2020, 2, 29))[-1].day, 29)

    def test_month_index_counts_from_the_first_month(self):
        self.assertEqual(cc.month_index(D(2019, 1, 31), D(2019, 1, 1)), 0)
        self.assertEqual(cc.month_index(D(2020, 1, 5), D(2019, 1, 1)), 12)
        self.assertEqual(cc.month_index(D(2018, 12, 31), D(2019, 1, 1)), -1)

    def test_postings_before_the_history_are_the_opening_total(self):
        before, per_month = cc.month_totals(
            [(D(2018, 12, 31), 500.0), (D(2019, 1, 10), 100.0), (D(2019, 2, 3), -40.0), (D(2030, 1, 1), 9.0)],
            D(2019, 1, 1),
            2,
        )
        self.assertEqual(before, 500.0)
        self.assertEqual(per_month, [100.0, -40.0])  # the posting after the last month is left out

    def test_running_balance_starts_from_the_opening_total(self):
        self.assertEqual(cc.running(500.0, [100.0, -40.0, 0.0]), [600.0, 560.0, 560.0])


class Groups(unittest.TestCase):
    def setUp(self):
        self.accounts = [
            account("3200 - Ertrag", "Income", "Income Account", "3200"),
            account("1100 - Debitoren", "Asset", "Receivable", "1100"),
            account("2000 - Kreditoren", "Liability", "Payable", "2000"),
            account("2002 - Lohn", "Liability", "Payable", "2002"),
            account("1200 - Handelswaren", "Asset", "Stock", "1200"),
            account("4200 - Handelswaren", "Expense", "Cost of Goods Sold", "4200"),
            account("4400 - Fremdleistungen", "Expense", "Expense Account", "4400"),
            account("5000 - Lohnaufwand", "Expense", "Expense Account", "5000"),
            account("6820 - Abschreibung", "Expense", "Depreciation", "6820"),
            account("6940 - Rundung", "Expense", "Round Off", "6940"),
            account("6900 - Zinsen", "Expense", "Expense Account", "6900"),
            account("6000 - Miete", "Expense", "Expense Account", "6000"),
        ]
        self.groups = cc.membership(self.accounts, {"2002 - Lohn"})

    def test_revenue_is_the_income_accounts(self):
        self.assertEqual([n for n, g in self.groups.items() if "revenue" in g], ["3200 - Ertrag"])

    def test_receivables_and_payables_leave_out_the_payroll_payable(self):
        self.assertEqual([n for n, g in self.groups.items() if "receivables" in g], ["1100 - Debitoren"])
        self.assertEqual([n for n, g in self.groups.items() if "payables" in g], ["2000 - Kreditoren"])

    def test_cost_of_goods_sold_is_also_purchases(self):
        self.assertEqual(self.groups["4200 - Handelswaren"], ["cogs", "purchases"])

    def test_purchases_are_the_4xxx_and_6xxx_expenses_without_depreciation_round_off_or_the_69x_result(self):
        purchases = sorted(n for n, g in self.groups.items() if "purchases" in g)
        self.assertEqual(purchases, ["4200 - Handelswaren", "4400 - Fremdleistungen", "6000 - Miete"])

    def test_personnel_costs_are_not_purchases(self):
        self.assertNotIn("purchases", self.groups.get("5000 - Lohnaufwand", []))

    def test_stock_is_the_stock_accounts(self):
        self.assertEqual([n for n, g in self.groups.items() if "inventory" in g], ["1200 - Handelswaren"])


class Ratios(unittest.TestCase):
    def test_a_balance_as_days_of_a_flow(self):
        # 3100 owed against 10000 of revenue in a 31-day month: 9.61 days
        self.assertEqual(cc.ratio(3100.0, 10000.0, 31), 9.6)

    def test_no_flow_gives_no_ratio(self):
        self.assertIsNone(cc.ratio(3100.0, 0.0, 31))
        self.assertIsNone(cc.ratio(3100.0, -5.0, 31))
        self.assertIsNone(cc.ratio(None, 10.0, 31))

    def test_no_stock_is_zero_days_whatever_the_cost_of_goods_sold(self):
        self.assertEqual(cc.dio_days(0.0, 0.0, 31), 0.0)
        self.assertEqual(cc.dio_days(0.0, 800.0, 31), 0.0)

    def test_stock_without_cost_of_goods_sold_has_no_days(self):
        self.assertIsNone(cc.dio_days(900.0, 0.0, 31))

    def test_ccc_adds_dio_and_takes_dpo_away_and_needs_all_three(self):
        self.assertEqual(cc.cash_conversion(20.0, 5.0, 12.5), 12.5)
        self.assertIsNone(cc.cash_conversion(20.0, None, 12.5))


class DaysToPay(unittest.TestCase):
    def test_weighted_by_amount(self):
        payments = [(D(2025, 3, 10), D(2025, 2, 8), 100.0), (D(2025, 3, 30), D(2025, 2, 8), 300.0)]
        # 30 days on 100, 50 days on 300: (3000 + 15000) / 400 = 45
        self.assertEqual(cc.weighted_days(payments), 45.0)

    def test_nothing_paid_is_no_days(self):
        self.assertIsNone(cc.weighted_days([]))


def history(months):
    """Invented history of `months` month ends from January 2019: a flat business, 10000 of revenue a month."""
    count = len(months)
    flows = {
        "revenue": flat(10000.0, count),
        "purchases": flat(4000.0, count),
        "cogs": flat(0.0, count),
    }
    balances = {
        "receivables": flat(3000.0, count),
        "payables": flat(2000.0, count),
        "inventory": flat(0.0, count),
    }
    return flows, balances


class Build(unittest.TestCase):
    def setUp(self):
        self.months = cc.month_ends(D(2019, 1, 1), D(2020, 12, 31))  # 24 months
        self.flows, self.balances = history(self.months)

    def build(self, customers=(), suppliers=()):
        return cc.build(self.months, self.flows, self.balances, list(customers), list(suppliers))

    def test_one_row_per_month_since_the_first_month(self):
        rows = self.build()
        self.assertEqual(len(rows), 24)
        self.assertEqual(rows[0]["month"], D(2019, 1, 31))
        self.assertEqual(rows[-1]["month"], D(2020, 12, 31))

    def test_monthly_values_use_the_days_of_that_month(self):
        row = self.build()[0]  # January: 31 days
        self.assertEqual(row["dso"], 9.3)  # 3000 / 10000 x 31
        self.assertEqual(row["dpo"], 15.5)  # 2000 / 4000 x 31
        self.assertEqual(row["dio"], 0.0)  # no stock
        self.assertEqual(row["ccc"], -6.2)  # 9.3 + 0 - 15.5

    def test_february_2020_uses_29_days(self):
        row = self.build()[13]
        self.assertEqual(row["month"], D(2020, 2, 29))
        self.assertEqual(row["dso"], 8.7)  # 3000 / 10000 x 29

    def test_no_stock_is_zero_dio_in_every_month(self):
        self.assertTrue(all(row["dio"] == 0.0 for row in self.build()))

    def test_rolling_values_need_twelve_months_of_history(self):
        rows = self.build()
        self.assertIsNone(rows[10]["ccc_12"])
        self.assertIsNotNone(rows[11]["ccc_12"])

    def test_rolling_values_use_the_days_of_the_twelve_months(self):
        # Window Jan 2019 to Dec 2019: 365 days, revenue 120000, purchases 48000, receivables at Dec 3000
        row = self.build()[11]
        self.assertEqual(row["dso_12"], round(3000.0 / 120000.0 * 365, 1))
        self.assertEqual(row["dpo_12"], round(2000.0 / 48000.0 * 365, 1))
        self.assertEqual(row["ccc_12"], round(row["dso_12"] + 0.0 - row["dpo_12"], 1))

    def test_rolling_window_of_a_leap_year_has_366_days(self):
        # Mar 2019 to Feb 2020 has 366 days (Feb 2020 is leap)
        row = self.build()[13]
        window_days = sum(m.day for m in self.months[2:14])
        self.assertEqual(window_days, 366)
        self.assertEqual(row["dpo_12"], round(2000.0 / (4000.0 * 12) * 366, 1))

    def test_change_of_the_rolling_ccc_starts_when_a_year_back_exists(self):
        rows = self.build()
        self.assertIsNone(rows[22]["ccc_12_change"])
        self.assertIsNone(rows[12]["ccc_12_change"])
        # the same flat business a year on: the change is only the leap day in the window
        self.assertEqual(rows[23]["ccc_12_change"], round(rows[23]["ccc_12"] - rows[11]["ccc_12"], 1))
        self.assertLess(abs(rows[23]["ccc_12_change"]), 1.0)

    def test_days_to_pay_by_the_month_of_the_payment(self):
        customers = [(D(2019, 2, 10), D(2019, 1, 11), 100.0), (D(2019, 2, 20), D(2019, 1, 1), 300.0)]
        rows = self.build(customers=customers)
        self.assertIsNone(rows[0]["paid_days_customers"])
        # 30 days on 100, 50 days on 300: 45 days, paid in February
        self.assertEqual(rows[1]["paid_days_customers"], 45.0)
        self.assertEqual(rows[1]["paid_days_customers_12"], None)  # no full year yet
        self.assertEqual(rows[12]["paid_days_customers_12"], 45.0)  # January 2020: the window still holds February 2019
        self.assertEqual(rows[11]["paid_days_customers_12"], 45.0)  # the first full window, January to December 2019

    def test_days_to_pay_of_suppliers_are_their_own(self):
        suppliers = [(D(2019, 3, 5), D(2019, 2, 3), 200.0)]
        rows = self.build(suppliers=suppliers)
        self.assertEqual(rows[2]["paid_days_suppliers"], 30.0)
        self.assertIsNone(rows[2]["paid_days_customers"])


if __name__ == "__main__":
    unittest.main()
