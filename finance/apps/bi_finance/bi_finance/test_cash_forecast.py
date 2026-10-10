"""Offline tests of cash_forecast.py: the weeks, the recurring costs, the payroll, the VAT and the running balance.

Invented dates, suppliers and amounts only; no site, no network, no frappe. Run from this app's directory:

    python3 -m unittest -v bi_finance.test_cash_forecast
"""

import datetime
import unittest

from bi_finance import cash_forecast as cf

D = datetime.date
AS_OF = D(2026, 10, 10)


class Dates(unittest.TestCase):
    def test_add_months_clamps_to_the_short_month(self):
        self.assertEqual(cf.add_months(D(2026, 1, 31), 1), D(2026, 2, 28))
        self.assertEqual(cf.add_months(D(2028, 2, 29), 12), D(2029, 2, 28))

    def test_add_months_wraps_the_year(self):
        self.assertEqual(cf.add_months(D(2026, 11, 30), 3), D(2027, 2, 28))

    def test_horizon_is_thirteen_weeks_less_a_day(self):
        self.assertEqual(cf.horizon_end(AS_OF) - AS_OF, datetime.timedelta(days=90))

    def test_week_of_the_first_seven_days_are_week_one(self):
        self.assertEqual(cf.week_of(AS_OF, AS_OF), 1)
        self.assertEqual(cf.week_of(AS_OF + datetime.timedelta(days=6), AS_OF), 1)
        self.assertEqual(cf.week_of(AS_OF + datetime.timedelta(days=7), AS_OF), 2)

    def test_week_of_the_last_day_of_the_horizon_is_week_thirteen(self):
        self.assertEqual(cf.week_of(cf.horizon_end(AS_OF), AS_OF), 13)
        self.assertIsNone(cf.week_of(cf.horizon_end(AS_OF) + datetime.timedelta(days=1), AS_OF))

    def test_an_overdue_day_is_week_one(self):
        self.assertEqual(cf.week_of(AS_OF - datetime.timedelta(days=40), AS_OF), 1)

    def test_week_bounds(self):
        self.assertEqual(cf.week_bounds(1, AS_OF), (AS_OF, AS_OF + datetime.timedelta(days=6)))
        self.assertEqual(cf.week_bounds(13, AS_OF)[1], cf.horizon_end(AS_OF))


class CustomerDaysLate(unittest.TestCase):
    def test_no_history_is_no_delay(self):
        self.assertEqual(cf.average_days_late([]), 0)

    def test_the_mean_of_the_paid_invoices(self):
        history = [(D(2026, 1, 1), D(2026, 1, 11)), (D(2026, 2, 1), D(2026, 2, 5))]  # 10 and 4 days late
        self.assertEqual(cf.average_days_late(history), 7)

    def test_an_early_payer_has_negative_days(self):
        self.assertEqual(cf.average_days_late([(D(2026, 1, 20), D(2026, 1, 17))]), -3)

    def test_the_expected_receipt_is_the_due_date_moved(self):
        self.assertEqual(cf.expected_receipt(D(2026, 10, 1), 10), D(2026, 10, 11))


class RecurringCosts(unittest.TestCase):
    def test_monthly_is_detected_with_the_median_amount(self):
        found = cf.detect_period([D(2026, 1, 5), D(2026, 2, 4), D(2026, 3, 6)], [100.0, 120.0, 110.0])
        self.assertEqual(found, ("monthly", 110.0))

    def test_quarterly_and_yearly(self):
        self.assertEqual(cf.detect_period([D(2026, 1, 15), D(2026, 4, 15)], [30.0, 30.0])[0], "quarterly")
        self.assertEqual(cf.detect_period([D(2024, 5, 1), D(2025, 5, 1)], [900.0, 900.0])[0], "yearly")

    def test_irregular_bills_have_no_period(self):
        self.assertIsNone(cf.detect_period([D(2026, 1, 1), D(2026, 1, 11), D(2026, 4, 30)], [10.0, 10.0, 10.0]))

    def test_one_bill_has_no_period(self):
        self.assertIsNone(cf.detect_period([D(2026, 1, 1)], [10.0]))

    def test_a_supplier_that_stopped_is_left_out(self):
        bills = [
            ("Cafe Invented AG", D(2026, 1, 5), 100.0, "PI-1"),
            ("Cafe Invented AG", D(2026, 2, 4), 100.0, "PI-2"),
            ("Cafe Invented AG", D(2026, 3, 6), 100.0, "PI-3"),
            ("Stopped Invented GmbH", D(2025, 6, 1), 50.0, "PI-4"),
            ("Stopped Invented GmbH", D(2025, 7, 1), 50.0, "PI-5"),
        ]
        found = cf.recurring_costs(bills, D(2026, 3, 20))
        self.assertEqual([item["supplier"] for item in found], ["Cafe Invented AG"])
        self.assertEqual(found[0]["last_bill"], "PI-3")

    def test_monthly_occurrences_counted_on_from_the_last_bill(self):
        item = {"period": "monthly", "last_date": D(2026, 3, 6)}
        self.assertEqual(list(cf.recurring_dates(item, D(2026, 3, 20), D(2026, 6, 18))),
                         [D(2026, 4, 6), D(2026, 5, 6), D(2026, 6, 6)])

    def test_quarterly_occurrences_stop_at_the_horizon(self):
        item = {"period": "quarterly", "last_date": D(2026, 1, 15)}
        self.assertEqual(list(cf.recurring_dates(item, D(2026, 3, 1), D(2026, 5, 30))), [D(2026, 4, 15)])


class Payroll(unittest.TestCase):
    def test_the_average_of_the_last_three_months_with_postings(self):
        postings = [
            (D(2026, 6, 20), 5000.0),  # outside the last three months
            (D(2026, 7, 20), 800.0),
            (D(2026, 8, 20), 1000.0),
            (D(2026, 9, 26), 1200.0),
            (D(2026, 9, 30), 300.0),
        ]
        self.assertEqual(cf.payroll_from_postings(postings), 1100.0)

    def test_no_postings_no_payroll(self):
        self.assertIsNone(cf.payroll_from_postings([]))

    def test_the_run_is_on_the_twenty_fifth(self):
        self.assertEqual(cf.payroll_run(2026, 11), D(2026, 11, 25))

    def test_a_twenty_fifth_on_a_sunday_is_the_friday_before(self):
        self.assertEqual(cf.payroll_run(2026, 10), D(2026, 10, 23))

    def test_a_twenty_fifth_on_a_saturday_is_the_friday_before(self):
        self.assertEqual(cf.payroll_run(2026, 7), D(2026, 7, 24))

    def test_the_runs_from_as_of_to_the_horizon(self):
        self.assertEqual(list(cf.payroll_dates(D(2026, 10, 10), D(2026, 12, 31))),
                         [D(2026, 10, 23), D(2026, 11, 25), D(2026, 12, 25)])

    def test_a_run_before_the_as_of_date_is_not_in_the_forecast(self):
        self.assertEqual(list(cf.payroll_dates(D(2026, 10, 24), D(2026, 11, 30))), [D(2026, 11, 25)])


class Vat(unittest.TestCase):
    def test_owed_is_the_liabilities_less_the_input_tax(self):
        # credit balances are negative debit-minus-credit
        self.assertEqual(cf.vat_owed({"2200": -800.0, "2202": -200.0, "1170": 300.0}), 700.0)

    def test_more_input_tax_than_owed_is_not_owed(self):
        self.assertEqual(cf.vat_owed({"2200": -100.0, "1171": 500.0}), -400.0)

    def test_the_quarter_of_a_day(self):
        self.assertEqual(cf.quarter_start(AS_OF), D(2026, 10, 1))
        self.assertEqual(cf.quarter_end(AS_OF), D(2026, 12, 31))
        self.assertEqual(cf.quarter_start(D(2026, 9, 30)), D(2026, 7, 1))
        self.assertEqual(cf.quarter_end(D(2026, 2, 1)), D(2026, 3, 31))

    def test_the_return_is_due_at_the_end_of_the_second_month_after_the_quarter(self):
        self.assertEqual(cf.vat_due_date(D(2026, 3, 31)), D(2026, 5, 31))
        self.assertEqual(cf.vat_due_date(D(2026, 6, 30)), D(2026, 8, 31))
        self.assertEqual(cf.vat_due_date(D(2026, 9, 30)), D(2026, 11, 30))
        self.assertEqual(cf.vat_due_date(D(2026, 12, 31)), D(2027, 2, 28))
        self.assertEqual(cf.vat_due_date(D(2027, 12, 31)), D(2028, 2, 29))

    def test_a_closed_quarter_is_paid_on_its_due_date_not_on_the_as_of_date(self):
        self.assertEqual(cf.vat_lines(AS_OF, 800.0, 0.0), [(D(2026, 11, 30), 800.0, D(2026, 9, 30))])

    def test_the_current_quarter_is_projected_on_its_own_due_date(self):
        as_of = D(2026, 12, 20)
        self.assertEqual(cf.vat_lines(as_of, 0.0, 900.0), [(D(2027, 2, 28), 900.0, D(2026, 12, 31))])

    def test_a_refund_comes_in_thirty_days_after_the_due_date(self):
        self.assertEqual(cf.vat_lines(AS_OF, -400.0, 0.0), [(D(2026, 12, 30), -400.0, D(2026, 9, 30))])

    def test_a_closed_quarter_already_paid_is_not_owed_and_not_taken_off_the_current_one(self):
        self.assertEqual(cf.vat_split(1000.0, 1000.0, 300.0), (0.0, 300.0))

    def test_a_closed_quarter_not_yet_paid_leaves_the_rest_to_the_current_one(self):
        self.assertEqual(cf.vat_split(1000.0, 0.0, 1300.0), (1000.0, 300.0))

    def test_a_closed_quarter_part_paid(self):
        self.assertEqual(cf.vat_split(1000.0, 400.0, 900.0), (600.0, 300.0))

    def test_a_refund_due_is_kept_as_the_books_show_it(self):
        self.assertEqual(cf.vat_split(-400.0, 0.0, -300.0), (-400.0, 100.0))

    def test_a_quarter_with_nothing_owed_has_no_line(self):
        self.assertEqual(cf.vat_lines(AS_OF, 0.0, 0.0), [])


class Forecast(unittest.TestCase):
    def line(self, kind, day, amount):
        return {"kind": kind, "day": day, "amount": amount, "party": "", "doctype": "", "name": "", "note": ""}

    def lines(self):
        return [
            self.line("receipt", AS_OF + datetime.timedelta(days=3), 500.0),
            self.line("bill", AS_OF - datetime.timedelta(days=5), 300.0),  # overdue: week one
            self.line("bill", AS_OF + datetime.timedelta(days=10), 200.0),
            self.line("vat", AS_OF + datetime.timedelta(days=91), 50.0),  # after the horizon
        ]

    def test_weeks_run_the_balance_from_the_opening_cash(self):
        weeks, lowest, beyond = cf.forecast(AS_OF, 1000.0, self.lines())
        self.assertEqual(len(weeks), 13)
        self.assertEqual((weeks[0]["receipt"], weeks[0]["bill"], weeks[0]["net"], weeks[0]["closing"]),
                         (500.0, 300.0, 200.0, 1200.0))
        self.assertEqual((weeks[1]["bill"], weeks[1]["closing"]), (200.0, 1000.0))
        self.assertEqual(beyond, 1)

    def test_the_lowest_week_is_the_earliest_on_a_tie(self):
        weeks, lowest, _ = cf.forecast(AS_OF, 1000.0, self.lines())
        self.assertEqual(lowest, 2)

    def test_the_lowest_point_is_where_the_balance_is_lowest(self):
        lines = self.lines() + [self.line("payroll", AS_OF + datetime.timedelta(days=29), 1500.0)]
        weeks, lowest, _ = cf.forecast(AS_OF, 1000.0, lines)
        self.assertEqual(weeks[4]["closing"], -500.0)
        self.assertEqual(lowest, 5)

    def test_a_refund_is_an_inflow_in_its_week(self):
        lines = [self.line("vat", AS_OF + datetime.timedelta(days=20), -300.0)]
        weeks, _, _ = cf.forecast(AS_OF, 1000.0, lines)
        self.assertEqual((weeks[2]["vat"], weeks[2]["net"], weeks[2]["closing"]), (-300.0, 300.0, 1300.0))

    def test_each_line_gets_its_week(self):
        lines = self.lines()
        cf.forecast(AS_OF, 1000.0, lines)
        self.assertEqual([line.get("week") for line in lines], [1, 1, 2, None])


if __name__ == "__main__":
    unittest.main()
