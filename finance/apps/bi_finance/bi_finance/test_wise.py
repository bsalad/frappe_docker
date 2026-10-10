"""Offline tests for the Wise feed: the HTTP calls (a fake opener, no network), the statement rows, and the dedupe.

Invented data only: made-up ids, amounts and descriptions, no real statement or token. The module imports frappe, so
run it in the image, as test_qrbill is run (see finance/docs/erpnext-setup.md):

    docker run --rm -v "$PWD/finance/apps/bi_finance:/home/frappe/bi_finance_src:ro" \
        frappe-finance-custom:v16.50.0-swiss-bi6 \
        sh -c 'cd /home/frappe/bi_finance_src && ../frappe-bench/env/bin/python -m unittest -v bi_finance.test_wise'
"""

import collections
import datetime
import io
import json
import types
import unittest
import urllib.error

from bi_finance import bank_feed, wise, wise_client

TOKEN = "invented-token-0000"


class FakeOpener:
    """Stands in for urlopen: records each request and answers with a JSON body, or raises the given error."""

    def __init__(self, answer=None, error=None):
        self.answer = answer
        self.error = error
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.answer).encode("utf-8")).__enter__()


def _statement(*items):
    return {"transactions": list(items)}


def _item(reference, direction, value, date="2026-03-04T10:00:00.000Z", fee=None, description="Invoice 77"):
    item = {
        "referenceNumber": reference,
        "type": direction,
        "date": date,
        "amount": {"value": value, "currency": "CHF"},
        "details": {"description": description},
    }
    if fee is not None:
        item["totalFees"] = {"value": fee, "currency": "CHF"}
    return item


class Client(unittest.TestCase):
    def test_profiles_sends_bearer_token_and_no_token_in_url(self):
        opener = FakeOpener(answer=[{"id": 1, "type": "BUSINESS"}])
        self.assertEqual(wise_client.profiles(TOKEN, opener=opener), [{"id": 1, "type": "BUSINESS"}])
        request = opener.requests[0]
        # the path carries its version: Wise answers 404 on an unversioned /profiles
        self.assertEqual(request.full_url, wise_client.BASE_URL + "/v1/profiles")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, request.full_url)

    def test_balances_asks_for_standard_balances(self):
        opener = FakeOpener(answer=[])
        wise_client.balances(TOKEN, 42, opener=opener)
        self.assertEqual(opener.requests[0].full_url, wise_client.BASE_URL + "/v4/profiles/42/balances?types=STANDARD")

    def test_statement_asks_for_flat_statement_in_utc(self):
        opener = FakeOpener(answer={"transactions": []})
        start = datetime.datetime(2026, 1, 1)
        end = datetime.datetime(2026, 2, 1)
        wise_client.statement(TOKEN, 42, 7, "EUR", start, end, opener=opener)
        url = opener.requests[0].full_url
        self.assertIn("/v1/profiles/42/balance-statements/7/statement.json?", url)
        self.assertIn("currency=EUR", url)
        self.assertIn("intervalStart=2026-01-01T00%3A00%3A00.000Z", url)
        self.assertIn("intervalEnd=2026-02-01T00%3A00%3A00.000Z", url)
        self.assertIn("type=FLAT", url)

    def test_sca_challenge_is_its_own_error(self):
        headers = {"x-2fa-approval": "invented-approval"}
        opener = FakeOpener(error=urllib.error.HTTPError(wise_client.BASE_URL, 403, "Forbidden", headers, None))
        with self.assertRaises(wise_client.ScaRequired):
            wise_client.statement(TOKEN, 42, 7, "CHF", datetime.datetime(2026, 1, 1), datetime.datetime(2026, 2, 1), opener=opener)

    def test_http_error_names_status_and_path_only(self):
        error = urllib.error.HTTPError(wise_client.BASE_URL, 500, "Server Error", {}, io.BytesIO(b"account body"))
        with self.assertRaises(wise_client.WiseError) as caught:
            wise_client.profiles(TOKEN, opener=FakeOpener(error=error))
        message = str(caught.exception)
        self.assertIn("HTTP 500", message)
        self.assertIn("/profiles", message)
        self.assertNotIn("account body", message)
        self.assertNotIn(TOKEN, message)
        self.assertFalse(isinstance(caught.exception, wise_client.ScaRequired))

    def test_network_failure_is_a_wise_error(self):
        with self.assertRaises(wise_client.WiseError):
            wise_client.profiles(TOKEN, opener=FakeOpener(error=urllib.error.URLError("invented")))


class Start(unittest.TestCase):
    def test_first_sync_starts_at_backfill_from_read_as_stored_text(self):
        # a Date single comes back as "2015-01-01", not a date: the first run must still start at that midnight
        settings = types.SimpleNamespace(last_sync=None, backfill_from="2015-01-01")
        self.assertEqual(wise._start(settings, datetime.datetime(2026, 10, 10)), datetime.datetime(2015, 1, 1))

    def test_later_sync_starts_a_week_before_the_last_one(self):
        settings = types.SimpleNamespace(last_sync=datetime.datetime(2026, 10, 1, 12), backfill_from="2015-01-01")
        self.assertEqual(wise._start(settings, datetime.datetime(2026, 10, 10)), datetime.datetime(2026, 9, 24, 12))


class Windows(unittest.TestCase):
    def test_windows_cover_the_range_each_within_the_limit(self):
        start = datetime.datetime(2015, 1, 1)
        end = datetime.datetime(2026, 10, 10)
        spans = wise_client.windows(start, end)
        self.assertEqual(spans[0][0], start)
        self.assertEqual(spans[-1][1], end)
        for (_, stop), (begin, _) in zip(spans, spans[1:]):
            self.assertEqual(stop, begin)
        for begin, stop in spans:
            self.assertLessEqual((stop - begin).days, 469)

    def test_no_window_for_an_empty_range(self):
        moment = datetime.datetime(2026, 1, 1)
        self.assertEqual(wise_client.windows(moment, moment), [])


class StatementRows(unittest.TestCase):
    def test_credit_goes_to_deposit_and_debit_to_withdrawal(self):
        rows = wise_client.statement_rows(_statement(_item("R1", "CREDIT", 120.5), _item("R2", "DEBIT", 30)), 9, "CHF")
        self.assertEqual([r["transaction_id"] for r in rows], ["wise:9:R1", "wise:9:R2"])
        self.assertEqual((rows[0]["deposit"], rows[0]["withdrawal"]), (120.5, 0.0))
        self.assertEqual((rows[1]["deposit"], rows[1]["withdrawal"]), (0.0, 30.0))
        self.assertEqual(rows[0]["date"], "2026-03-04")
        self.assertEqual(rows[0]["currency"], "CHF")

    def test_a_fee_is_its_own_row(self):
        rows = wise_client.statement_rows(_statement(_item("R1", "DEBIT", 100, fee=1.25)), 9, "EUR")
        self.assertEqual([r["transaction_id"] for r in rows], ["wise:9:R1", "wise:9:R1:fee"])
        self.assertEqual(rows[1]["withdrawal"], 1.25)
        self.assertEqual(rows[1]["description"], "Wise fee: Invoice 77")

    def test_zero_fee_adds_no_row(self):
        rows = wise_client.statement_rows(_statement(_item("R1", "CREDIT", 10, fee=0)), 9, "CHF")
        self.assertEqual(len(rows), 1)

    def test_a_missing_reference_is_keyed_by_position(self):
        rows = wise_client.statement_rows(_statement(_item(None, "CREDIT", 5)), 9, "CHF")
        self.assertEqual(rows[0]["transaction_id"], "wise:9:pos0")

    def test_an_unusable_transaction_stops_the_run(self):
        with self.assertRaises(wise_client.WiseError):
            wise_client.statement_rows(_statement(_item("R1", "SIDEWAYS", 5)), 9, "CHF")


class Dedupe(unittest.TestCase):
    def row(self, tid, date="2026-03-04", deposit=0.0, withdrawal=50.0):
        return {"transaction_id": tid, "date": date, "deposit": deposit, "withdrawal": withdrawal}

    def imported(self, *keys):
        return collections.Counter(bank_feed._key(row) for row in keys)

    def test_a_fed_id_is_not_written_again(self):
        create, summary = bank_feed.new_rows([self.row("wise:9:R1")], {"wise:9:R1"}, self.imported())
        self.assertEqual(create, [])
        self.assertEqual(summary["already_fed"], 1)

    def test_a_row_matching_an_imported_line_is_skipped(self):
        create, summary = bank_feed.new_rows([self.row("wise:9:R1")], set(), self.imported(self.row("")))
        self.assertEqual(create, [])
        self.assertEqual(summary["already_imported"], 1)

    def test_two_same_amount_movements_one_imported_keeps_one(self):
        rows = [self.row("wise:9:R1"), self.row("wise:9:R2")]
        create, summary = bank_feed.new_rows(rows, set(), self.imported(self.row("")))
        self.assertEqual([r["transaction_id"] for r in create], ["wise:9:R2"])
        self.assertEqual(summary["already_imported"], 1)

    def test_a_different_amount_or_day_is_new(self):
        rows = [self.row("wise:9:R1", withdrawal=51.0), self.row("wise:9:R2", date="2026-03-05")]
        create, _ = bank_feed.new_rows(rows, set(), self.imported(self.row("")))
        self.assertEqual(len(create), 2)

    def test_a_row_repeated_across_statement_windows_counts_once(self):
        rows = [self.row("wise:9:R1"), self.row("wise:9:R1"), self.row("wise:9:R2")]
        create, summary = bank_feed.new_rows(rows, set(), self.imported(self.row("")))
        self.assertEqual([r["transaction_id"] for r in create], ["wise:9:R2"])
        self.assertEqual((summary["rows"], summary["already_imported"]), (2, 1))

    def test_second_run_creates_nothing(self):
        rows = [self.row("wise:9:R1"), self.row("wise:9:R2", deposit=10.0, withdrawal=0.0)]
        first, _ = bank_feed.new_rows(rows, set(), self.imported())
        self.assertEqual(len(first), 2)
        fed = {r["transaction_id"] for r in first}
        second, summary = bank_feed.new_rows(rows, fed, self.imported())
        self.assertEqual(second, [])
        self.assertEqual(summary["already_fed"], 2)


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
        rates = {("CHF", "2026-03-04"): 1.0}
        self.assertEqual(wise._chf_sum({"2026-03-04": 12.5}, "CHF", rates), (12.5, 0))

    def test_net_by_day_adds_deposits_and_takes_withdrawals(self):
        rows = [
            {"date": "2026-03-04", "deposit": 10.0, "withdrawal": 0.0},
            {"date": "2026-03-04", "deposit": 0.0, "withdrawal": 3.5},
            {"date": "2026-03-05", "deposit": 0.0, "withdrawal": 1.0},
        ]
        self.assertEqual(dict(wise._net_by_day(rows)), {"2026-03-04": 6.5, "2026-03-05": -1.0})

    def test_counts_per_month(self):
        rows = [{"date": "2026-02-27"}, {"date": "2026-03-01"}, {"date": "2026-03-31"}]
        self.assertEqual(wise._by_month(rows), {"2026-02": 1, "2026-03": 2})

    def test_total_leaves_out_a_balance_with_no_rate(self):
        accounts = [
            {"wise_balance_chf": 100.0, "erp_net_chf": 90.0, "create_chf": 5.0, "no_rate_days": 0},
            {"wise_balance_chf": None, "erp_net_chf": 2.0, "create_chf": 1.0, "no_rate_days": 1},
        ]
        self.assertEqual(
            wise._total(accounts),
            {"wise_balance_chf": 100.0, "erp_net_chf": 92.0, "create_chf": 6.0, "no_rate_days": 1},
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
        opener = FakeOpener(answer=self.answer({"date": "2026-03-04", "base": "EUR", "quote": "CHF", "rate": 0.5}))
        days = bank_feed.fetch_rates("EUR", datetime.date(2026, 3, 2), datetime.date(2026, 3, 6), opener=opener)
        self.assertEqual(days, {"2026-03-04": 0.5})
        self.assertEqual(len(opener.requests), 1)
        url = opener.requests[0].full_url
        self.assertTrue(url.startswith(bank_feed.RATE_URL + "?"))
        self.assertIn("from=2026-03-02", url)
        self.assertIn("to=2026-03-06", url)
        self.assertIn("base=EUR", url)
        self.assertIn("quotes=CHF", url)

    def test_fetch_rates_http_error_names_status_only(self):
        error = urllib.error.HTTPError(bank_feed.RATE_URL, 503, "Unavailable", {}, io.BytesIO(b"rate body"))
        with self.assertRaises(bank_feed.RateError) as caught:
            bank_feed.fetch_rates("EUR", datetime.date(2026, 3, 2), datetime.date(2026, 3, 6), opener=FakeOpener(error=error))
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


if __name__ == "__main__":
    unittest.main()
