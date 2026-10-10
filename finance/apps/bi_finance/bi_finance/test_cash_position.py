"""Offline tests for report/cash_position: month ends, the rate in force on a day, balances and CHF.

Invented accounts, amounts and rates only; no site, no network. The module imports frappe, so run
it in the image, as test_qrbill.py does (finance/docs/erpnext-setup.md):

    docker run --rm -v "$PWD/finance/apps/bi_finance:/home/frappe/bi_finance_src:ro" \
        frappe-finance-custom:v16.50.0-swiss-bi6 \
        sh -c 'cd /home/frappe/bi_finance_src && ../frappe-bench/env/bin/python -m unittest -v bi_finance.test_cash_position'
"""

import datetime
import unittest

from bi_finance.bi_finance.report.cash_position import cash_position as cp

D = datetime.date
CHF_ACCOUNT = "1000 - Test Bank CHF"
USD_ACCOUNT = "1100 - Test Bank USD"


class MonthEnds(unittest.TestCase):
    def test_month_ends_up_to_the_last_full_month(self):
        self.assertEqual(
            cp.month_ends(D(2019, 1, 1), D(2019, 3, 15)),
            [D(2019, 1, 31), D(2019, 2, 28)],  # March is not over on the 15th
        )

    def test_leap_february(self):
        self.assertEqual(cp.month_ends(D(2020, 2, 1), D(2020, 2, 29))[-1], D(2020, 2, 29))

    def test_last_day_is_included(self):
        self.assertEqual(cp.month_ends(D(2019, 12, 1), D(2019, 12, 31)), [D(2019, 12, 31)])

    def test_nothing_before_the_first_month_ends(self):
        self.assertEqual(cp.month_ends(D(2019, 1, 1), D(2019, 1, 30)), [])


class RateOn(unittest.TestCase):
    RATES = [(D(2024, 1, 10), 0.9), (D(2024, 6, 1), 0.95)]

    def test_latest_rate_on_or_before_the_day(self):
        self.assertEqual(cp.rate_on(self.RATES, D(2024, 3, 1)), (D(2024, 1, 10), 0.9))
        self.assertEqual(cp.rate_on(self.RATES, D(2024, 6, 1)), (D(2024, 6, 1), 0.95))

    def test_no_rate_before_the_first(self):
        self.assertEqual(cp.rate_on(self.RATES, D(2023, 12, 31)), (None, None))

    def test_no_rates_at_all(self):
        self.assertEqual(cp.rate_on([], D(2024, 1, 1)), (None, None))


class Build(unittest.TestCase):
    def setUp(self):
        self.accounts = [(CHF_ACCOUNT, "CHF"), (USD_ACCOUNT, "USD")]
        self.movements = {
            CHF_ACCOUNT: [(D(2019, 1, 15), 1000.0), (D(2019, 2, 10), -250.25)],
            USD_ACCOUNT: [(D(2019, 1, 20), 100.0), (D(2019, 3, 5), 50.0)],
        }
        self.rates = {"CHF": [(datetime.date.min, 1.0)], "USD": [(D(2019, 1, 1), 0.9), (D(2019, 2, 1), 0.8)]}

    def test_balances_and_chf_at_the_as_of_date(self):
        rows, _chart, total, missing = cp.build(
            self.accounts, self.movements, self.rates, D(2019, 2, 28), [D(2019, 2, 28)]
        )
        by_account = {row["account"]: row for row in rows}
        self.assertEqual(by_account[CHF_ACCOUNT]["balance"], 749.75)
        self.assertEqual(by_account[CHF_ACCOUNT]["balance_chf"], 749.75)
        self.assertIsNone(by_account[CHF_ACCOUNT]["rate_date"])  # CHF is the base: no rate date
        self.assertEqual(by_account[USD_ACCOUNT]["balance"], 100.0)
        self.assertEqual(by_account[USD_ACCOUNT]["rate"], 0.8)
        self.assertEqual(by_account[USD_ACCOUNT]["rate_date"], D(2019, 2, 1))
        self.assertEqual(by_account[USD_ACCOUNT]["balance_chf"], 80.0)
        self.assertEqual(total, 829.75)
        self.assertEqual(missing, [])

    def test_later_movements_are_not_in_an_earlier_balance(self):
        rows, _chart, total, _messages = cp.build(
            self.accounts, self.movements, self.rates, D(2019, 1, 31), [D(2019, 1, 31)]
        )
        self.assertEqual(total, round(1000.0 * 1.0 + 100.0 * 0.9, 2))  # USD rate of 2019-01-01, the 2019-02 rate is not yet in force

    def test_missing_rate_leaves_the_account_out_and_says_so(self):
        rates = {"CHF": self.rates["CHF"]}
        rows, _chart, total, missing = cp.build(self.accounts, self.movements, rates, D(2019, 2, 28), [])
        by_account = {row["account"]: row for row in rows}
        self.assertIsNone(by_account[USD_ACCOUNT]["balance_chf"])
        self.assertEqual(by_account[USD_ACCOUNT]["balance"], 100.0)  # the original currency still shows
        self.assertEqual(total, 749.75)
        self.assertEqual(missing, [(USD_ACCOUNT, "USD")])

    def test_chart_has_month_end_points_and_a_total_per_month(self):
        months = cp.month_ends(D(2019, 1, 1), D(2019, 3, 31))
        _rows, chart, _total, _messages = cp.build(self.accounts, self.movements, self.rates, D(2019, 3, 31), months)
        self.assertEqual(chart["data"]["labels"], ["2019-01-31", "2019-02-28", "2019-03-31"])
        datasets = {d["name"]: d["values"] for d in chart["data"]["datasets"]}
        self.assertEqual(datasets[CHF_ACCOUNT], [1000.0, 749.75, 749.75])
        self.assertEqual(datasets[USD_ACCOUNT], [90.0, 80.0, 120.0])  # 150 at the March rate of 0.8
        self.assertEqual(datasets["Total CHF"], [1090.0, 829.75, 869.75])

    def test_chart_gap_for_a_month_without_a_rate(self):
        rates = {"CHF": self.rates["CHF"], "USD": [(D(2019, 2, 1), 0.8)]}
        _rows, chart, _total, _messages = cp.build(
            self.accounts, self.movements, rates, D(2019, 2, 28), cp.month_ends(D(2019, 1, 1), D(2019, 2, 28))
        )
        datasets = {d["name"]: d["values"] for d in chart["data"]["datasets"]}
        self.assertIsNone(datasets[USD_ACCOUNT][0])  # no rate on 2019-01-31
        self.assertEqual(datasets["Total CHF"][0], 1000.0)

    def test_no_accounts_is_an_empty_table_with_a_zero_total(self):
        rows, chart, total, missing = cp.build([], {}, {}, D(2019, 2, 28), [D(2019, 1, 31)])
        self.assertEqual((rows, total, missing), ([], 0.0, []))
        self.assertEqual(chart["data"]["datasets"], [{"name": "Total CHF", "values": [0.0]}])


if __name__ == "__main__":
    unittest.main()
