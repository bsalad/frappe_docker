"""Offline tests for the Wise sync that need frappe: the dedupe, the CHF values, the balances, the rates and the start.

Invented data only: made-up ids, amounts and rates. The module imports frappe, so run it in the image, as test_qrbill is
run (see finance/docs/erpnext-setup.md). The client's own tests (HTTP, amounts, feed rows) run on the host in
test_wise_client.py:

    docker run --rm -v "$PWD/finance/apps/bi_finance:/home/frappe/bi_finance_src:ro" \
        frappe-finance-custom:v16.50.0-swiss-bi11 \
        sh -c 'cd /home/frappe/bi_finance_src && ../frappe-bench/env/bin/python -m unittest -v bi_finance.test_wise'
"""

import collections
import datetime
import io
import json
import types
import unittest
import urllib.error

from bi_finance import bank_feed, wise

BANK_ROW = {"date": "2026-03-04", "deposit": 0.0, "withdrawal": 50.0}


class Start(unittest.TestCase):
    def test_first_sync_starts_at_backfill_from_read_as_stored_text(self):
        # a Date single comes back as "2015-01-01", not a date: the first run must still start at that midnight
        settings = types.SimpleNamespace(last_sync=None, backfill_from="2015-01-01")
        self.assertEqual(wise._start(settings, datetime.datetime(2026, 10, 10)), datetime.datetime(2015, 1, 1))

    def test_later_sync_starts_a_week_before_the_last_one(self):
        settings = types.SimpleNamespace(last_sync=datetime.datetime(2026, 10, 1, 12), backfill_from="2015-01-01")
        self.assertEqual(wise._start(settings, datetime.datetime(2026, 10, 10)), datetime.datetime(2026, 9, 24, 12))


class Balances(unittest.TestCase):
    def test_the_standard_and_savings_amounts_of_a_currency_add_up(self):
        items = [
            {"currency": "CHF", "amount": {"value": 10.0}},
            {"currency": "CHF", "amount": {"value": 5.5}},
            {"currency": "EUR", "amount": {"value": 2}},
        ]
        self.assertEqual(wise._balances(items), {"CHF": 15.5, "EUR": 2.0})


class Dedupe(unittest.TestCase):
    def row(self, tid, date="2026-03-04", deposit=0.0, withdrawal=50.0):
        return {"transaction_id": tid, "date": date, "deposit": deposit, "withdrawal": withdrawal}

    def imported(self, *keys):
        return collections.Counter(bank_feed._key(row) for row in keys)

    def test_a_fed_id_is_not_written_again(self):
        create, summary = bank_feed.new_rows([self.row("wise:activity:9")], {"wise:activity:9"}, self.imported())
        self.assertEqual(create, [])
        self.assertEqual(summary["already_fed"], 1)

    def test_a_row_matching_an_imported_line_is_skipped(self):
        create, summary = bank_feed.new_rows([self.row("wise:activity:9")], set(), self.imported(self.row("")))
        self.assertEqual(create, [])
        self.assertEqual(summary["already_imported"], 1)

    def test_two_same_amount_movements_one_imported_keeps_one(self):
        rows = [self.row("wise:activity:9"), self.row("wise:activity:10")]
        create, summary = bank_feed.new_rows(rows, set(), self.imported(self.row("")))
        self.assertEqual([r["transaction_id"] for r in create], ["wise:activity:10"])
        self.assertEqual(summary["already_imported"], 1)

    def test_a_row_seen_twice_in_one_run_counts_once(self):
        rows = [self.row("wise:transfer:5"), self.row("wise:transfer:5"), self.row("wise:activity:9")]
        create, summary = bank_feed.new_rows(rows, set(), self.imported())
        self.assertEqual([r["transaction_id"] for r in create], ["wise:transfer:5", "wise:activity:9"])
        self.assertEqual((summary["rows"], summary["create"]), (2, 2))

    def test_second_run_creates_nothing(self):
        rows = [self.row("wise:activity:9"), self.row("wise:activity:10", deposit=10.0, withdrawal=0.0)]
        first, _ = bank_feed.new_rows(rows, set(), self.imported())
        self.assertEqual(len(first), 2)
        fed = {r["transaction_id"] for r in first}
        second, summary = bank_feed.new_rows(rows, fed, self.imported())
        self.assertEqual(second, [])
        self.assertEqual(summary["already_fed"], 2)


class Unmatched(unittest.TestCase):
    # invented: a feed row, a Bank Transaction the feed wrote, and bexio's lines (no transaction_id) of the same date and amount
    FEED = {"transaction_id": "wise:activity:1", "date": "2026-03-04", "deposit": 0.0, "withdrawal": 50.0}

    def bt(self, tid, date="2026-03-04", withdrawal=50.0):
        return {"transaction_id": tid, "date": datetime.date.fromisoformat(date), "deposit": 0.0, "withdrawal": withdrawal}

    def test_a_bank_transaction_the_feed_carries_is_matched(self):
        self.assertEqual(wise.unmatched_by_month([self.bt("wise:activity:1")], [self.FEED]), {})

    def test_a_feed_id_no_row_carries_is_counted_in_its_month(self):
        self.assertEqual(wise.unmatched_by_month([self.bt("wise:activity:9", "2026-02-11")], [self.FEED]), {"2026-02": 1})

    def test_a_bexio_line_of_the_same_day_and_amount_is_matched(self):
        self.assertEqual(wise.unmatched_by_month([self.bt("")], [self.FEED]), {})

    def test_two_bexio_lines_for_one_row_leave_one_unmatched(self):
        self.assertEqual(wise.unmatched_by_month([self.bt(""), self.bt("")], [self.FEED]), {"2026-03": 1})

    def test_a_bexio_line_with_no_row_is_counted_per_month_sorted(self):
        existing = [self.bt("", "2026-07-02"), self.bt("", "2026-06-30")]
        self.assertEqual(wise.unmatched_by_month(existing, []), {"2026-06": 1, "2026-07": 1})


class Chf(unittest.TestCase):
    # the rates are given, so no ERPNext lookup runs: a currency and a day with no entry in rates would ask ERPNext
    def test_each_day_is_converted_at_its_own_rate(self):
        rates = {("EUR", "2026-03-04"): 0.9, ("EUR", "2026-03-05"): 0.5}
        net = collections.Counter({"2026-03-04": 100.0, "2026-03-05": -20.0})
        self.assertEqual(wise._chf_sum(net, "EUR", rates), (80.0, 0))

    def test_a_day_with_no_rate_is_left_out_and_counted(self):
        rates = {("PLN", "2026-03-04"): 0.25, ("PLN", "2026-03-05"): 0.0}
        net = {"2026-03-04": 40.0, "2026-03-05": 99.0}
        self.assertEqual(wise._chf_sum(net, "PLN", rates), (10.0, 1))

    def test_chf_needs_no_rate(self):
        self.assertEqual(wise._chf_sum({"2026-03-04": 12.5}, "CHF", {}), (12.5, 0))

    def test_the_chf_the_source_gave_is_used_and_the_rest_take_the_day_rate(self):
        create = [
            {"date": "2026-03-04", "deposit": 0.0, "withdrawal": 100.0, "chf": 90.0},
            {"date": "2026-03-05", "deposit": 0.0, "withdrawal": 50.0, "chf": None},
        ]
        rates = {("EUR", "2026-03-05"): 0.5}
        self.assertEqual(wise._create_chf(create, "EUR", rates), (-115.0, 0))

    def test_a_deposit_given_in_chf_counts_positive(self):
        create = [{"date": "2026-03-04", "deposit": 30.0, "withdrawal": 0.0, "chf": 30.0}]
        self.assertEqual(wise._create_chf(create, "USD", {}), (30.0, 0))

    def test_net_by_day_adds_deposits_and_takes_withdrawals(self):
        rows = [
            {"date": "2026-03-04", "deposit": 10.0, "withdrawal": 0.0},
            {"date": "2026-03-04", "deposit": 0.0, "withdrawal": 3.5},
            {"date": "2026-03-05", "deposit": 0.0, "withdrawal": 1.0},
        ]
        self.assertEqual(dict(wise._net_by_day(rows)), {"2026-03-04": 6.5, "2026-03-05": -1.0})

    def test_counts_per_month_and_per_kind(self):
        rows = [{"date": "2026-02-27", "kind": "FEE"}, {"date": "2026-03-01", "kind": "TRANSFER"}, {"date": "2026-03-31", "kind": "FEE"}]
        self.assertEqual(wise._by_month(rows), {"2026-02": 1, "2026-03": 2})
        self.assertEqual(wise._by_kind(rows), {"FEE": 2, "TRANSFER": 1})

    def test_by_month_check_is_the_ledger_less_the_feed_per_month(self):
        rows = [{"date": "2026-03-04", "deposit": 0.0, "withdrawal": 100.0}, {"date": "2026-04-01", "deposit": 50.0, "withdrawal": 0.0}]
        erp_by_day = {"2026-03-04": -90.0, "2026-03-20": -5.0, "2026-05-02": 10.0}
        self.assertEqual(
            wise._by_month_check(rows, erp_by_day),
            {
                "2026-03": {"feed": -100.0, "ledger": -95.0, "difference": 5.0},
                "2026-04": {"feed": 50.0, "ledger": 0.0, "difference": -50.0},
                "2026-05": {"feed": 0.0, "ledger": 10.0, "difference": 10.0},
            },
        )

    def test_total_leaves_out_a_balance_with_no_rate(self):
        accounts = [
            {"wise_balance_chf": 100.0, "erp_net_chf": 90.0, "create_chf": 5.0, "no_rate_days": 0, "agrees": True},
            {"wise_balance_chf": None, "erp_net_chf": 2.0, "create_chf": 1.0, "no_rate_days": 1, "agrees": False},
        ]
        self.assertEqual(
            wise._total(accounts),
            {"wise_balance_chf": 100.0, "erp_net_chf": 92.0, "create_chf": 6.0, "no_rate_days": 1, "agrees": False},
        )


class Rates(unittest.TestCase):
    # invented rates; the source's answer is a list of {date, base, quote, rate}, as the v2 range endpoint gives it
    def answer(self, *items):
        return list(items)

    def test_rate_days_keeps_only_the_pair_and_no_zero_rate(self):
        payload = self.answer(
            {"date": "2026-03-04", "base": "EUR", "quote": "CHF", "rate": 0.5},
            {"date": "2026-03-05", "base": "USD", "quote": "CHF", "rate": 0.7},
            {"date": "2026-03-06", "base": "EUR", "quote": "USD", "rate": 1.1},
            {"date": "2026-03-07", "base": "EUR", "quote": "CHF", "rate": 0},
        )
        self.assertEqual(bank_feed.rate_days(payload, "EUR"), {"2026-03-04": 0.5})

    def test_rate_days_refuses_an_answer_that_is_not_a_list(self):
        with self.assertRaises(bank_feed.RateError):
            bank_feed.rate_days({"rates": {}}, "EUR")

    def test_fetch_rates_is_one_request_for_the_range_and_pair(self):
        opener = _Opener(self.answer({"date": "2026-03-04", "base": "EUR", "quote": "CHF", "rate": 0.5}))
        days = bank_feed.fetch_rates("EUR", datetime.date(2026, 3, 2), datetime.date(2026, 3, 6), opener=opener)
        self.assertEqual(days, {"2026-03-04": 0.5})
        self.assertEqual(len(opener.requests), 1)
        url = opener.requests[0].full_url
        self.assertTrue(url.startswith(bank_feed.RATE_URL + "?"))
        self.assertIn("from=2026-03-02", url)
        self.assertIn("to=2026-03-06", url)
        self.assertIn("base=EUR", url)
        self.assertIn("quotes=CHF", url)

    def test_fetch_rates_sends_a_user_agent_the_source_accepts(self):
        # the source answers 403 to urllib's default agent ("Python-urllib/..."), so the request names its own
        opener = _Opener(self.answer())
        bank_feed.fetch_rates("EUR", datetime.date(2026, 3, 2), datetime.date(2026, 3, 6), opener=opener)
        agent = opener.requests[0].get_header("User-agent")
        self.assertTrue(agent)
        self.assertFalse(agent.startswith("Python-urllib"))

    def test_fetch_rates_http_error_names_status_only(self):
        error = urllib.error.HTTPError(bank_feed.RATE_URL, 503, "Unavailable", {}, io.BytesIO(b"rate body"))
        with self.assertRaises(bank_feed.RateError) as caught:
            bank_feed.fetch_rates("EUR", datetime.date(2026, 3, 2), datetime.date(2026, 3, 6), opener=_Opener(error=error))
        self.assertIn("HTTP 503", str(caught.exception))
        self.assertNotIn("rate body", str(caught.exception))

    def test_carry_forward_gives_a_weekend_the_friday_rate(self):
        days = {"2026-03-06": 0.9, "2026-03-09": 0.8}
        out = bank_feed.carry_forward(days, datetime.date(2026, 3, 6), datetime.date(2026, 3, 10))
        self.assertEqual(
            out,
            {"2026-03-06": 0.9, "2026-03-07": 0.9, "2026-03-08": 0.9, "2026-03-09": 0.8, "2026-03-10": 0.8},
        )

    def test_carry_forward_leaves_out_days_before_the_first_rate(self):
        out = bank_feed.carry_forward({"2026-03-04": 0.9}, datetime.date(2026, 3, 2), datetime.date(2026, 3, 5))
        self.assertEqual(out, {"2026-03-04": 0.9, "2026-03-05": 0.9})

    def test_fetch_start_is_the_latest_stored_day_when_that_is_later(self):
        start = datetime.date(2026, 1, 1)
        self.assertEqual(bank_feed.fetch_start(start, datetime.date(2026, 3, 6)), datetime.date(2026, 3, 6))
        self.assertEqual(bank_feed.fetch_start(start, None), start)
        self.assertEqual(bank_feed.fetch_start(datetime.date(2026, 3, 9), datetime.date(2026, 3, 6)), datetime.date(2026, 3, 9))


class _Opener:
    """Stands in for urlopen in the rate tests: records each request and answers with a JSON body, or raises the error."""

    def __init__(self, answer=None, error=None):
        self.answer = answer
        self.error = error
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.answer).encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
