"""Offline tests for the PayPal feed: the HTTP calls (a fake opener, no network), the transaction rows, and the window limit.

Invented data only: made-up ids, amounts and dates, no real transaction, balance or credential. The client has no frappe
import, so it runs without the image:

    cd finance/apps/bi_finance && python3 -m unittest -v bi_finance.test_paypal

The frappe side (paypal.py) runs in the image, as test_wise is run (see finance/docs/erpnext-setup.md).
"""

import base64
import datetime
import io
import json
import unittest
import urllib.error
import urllib.parse

from bi_finance import paypal_client

CLIENT_ID = "invented-client-id"
SECRET = "invented-secret-0000"
TOKEN = "invented-token-0000"


class FakeOpener:
    """Stands in for urlopen: records each request and answers with a JSON body, one per call, or raises the error."""

    def __init__(self, *answers, error=None):
        self.answers = list(answers)
        self.error = error
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.answers.pop(0)).encode("utf-8")).__enter__()


def _detail(tid, value, currency="EUR", fee=None, event="T0006", date="2026-03-04T10:00:00+0000", status="S"):
    info = {
        "transaction_id": tid,
        "transaction_event_code": event,
        "transaction_initiation_date": date,
        "transaction_status": status,
        "transaction_amount": {"currency_code": currency, "value": value},
    }
    if fee is not None:
        info["fee_amount"] = {"currency_code": currency, "value": fee}
    return {"transaction_info": info, "payer_info": {"email_address": "invented@example.test"}}


class Token(unittest.TestCase):
    def test_token_is_asked_with_the_secret_in_the_basic_header_only(self):
        opener = FakeOpener({"access_token": TOKEN})
        self.assertEqual(paypal_client.access_token(CLIENT_ID, SECRET, opener=opener), TOKEN)
        request = opener.requests[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, paypal_client.BASE_URL + "/v1/oauth2/token")
        expected = "Basic " + base64.b64encode("{}:{}".format(CLIENT_ID, SECRET).encode("utf-8")).decode("ascii")
        self.assertEqual(request.get_header("Authorization"), expected)
        self.assertEqual(urllib.parse.parse_qs(request.data.decode("ascii")), {"grant_type": ["client_credentials"]})
        self.assertNotIn(SECRET, request.full_url)

    def test_an_answer_without_a_token_is_an_error(self):
        with self.assertRaises(paypal_client.PayPalError):
            paypal_client.access_token(CLIENT_ID, SECRET, opener=FakeOpener({"error": "invalid_client"}))

    def test_http_error_names_status_and_path_only(self):
        error = urllib.error.HTTPError(paypal_client.BASE_URL, 401, "Unauthorized", {}, io.BytesIO(b"client body"))
        with self.assertRaises(paypal_client.PayPalError) as caught:
            paypal_client.access_token(CLIENT_ID, SECRET, opener=FakeOpener(error=error))
        message = str(caught.exception)
        self.assertIn("HTTP 401", message)
        self.assertIn("/v1/oauth2/token", message)
        self.assertNotIn("client body", message)
        self.assertNotIn(SECRET, message)

    def test_network_failure_is_a_paypal_error(self):
        with self.assertRaises(paypal_client.PayPalError):
            paypal_client.balances(TOKEN, opener=FakeOpener(error=urllib.error.URLError("invented")))


class Transactions(unittest.TestCase):
    def test_every_page_is_read(self):
        first = {"transaction_details": [_detail("T1", "10.00")], "total_pages": 2, "page": 1}
        second = {"transaction_details": [_detail("T2", "-4.00")], "total_pages": 2, "page": 2}
        opener = FakeOpener(first, second)
        start = datetime.datetime(2026, 1, 1)
        records = paypal_client.transactions(TOKEN, start, datetime.datetime(2026, 1, 31), opener=opener)
        self.assertEqual([r["transaction_info"]["transaction_id"] for r in records], ["T1", "T2"])
        pages = [urllib.parse.parse_qs(urllib.parse.urlparse(r.full_url).query)["page"] for r in opener.requests]
        self.assertEqual(pages, [["1"], ["2"]])

    def test_request_asks_for_transaction_info_and_balance_records_in_utc(self):
        opener = FakeOpener({"transaction_details": [], "total_pages": 1})
        paypal_client.transactions(TOKEN, datetime.datetime(2026, 1, 1), datetime.datetime(2026, 1, 31), opener=opener)
        request = opener.requests[0]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
        self.assertEqual(query["start_date"], ["2026-01-01T00:00:00Z"])
        self.assertEqual(query["end_date"], ["2026-01-31T00:00:00Z"])
        self.assertEqual(query["fields"], ["transaction_info"])
        self.assertEqual(query["balance_affecting_records_only"], ["Y"])
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)

    def test_no_transactions_is_an_empty_list(self):
        opener = FakeOpener({"total_pages": 0})
        self.assertEqual(paypal_client.transactions(TOKEN, datetime.datetime(2026, 1, 1), datetime.datetime(2026, 1, 2), opener=opener), [])


class Balances(unittest.TestCase):
    def test_each_currency_total_is_read(self):
        answer = {
            "balances": [
                {"currency": "EUR", "primary": True, "total_balance": {"currency_code": "EUR", "value": "123.45"}},
                {"currency": "USD", "primary": False, "total_balance": {"currency_code": "USD", "value": "0.00"}},
            ]
        }
        self.assertEqual(paypal_client.balances(TOKEN, opener=FakeOpener(answer)), {"EUR": 123.45, "USD": 0.0})


class Windows(unittest.TestCase):
    def test_windows_cover_the_range_each_within_the_limit(self):
        start = datetime.datetime(2023, 10, 10)
        end = datetime.datetime(2026, 10, 10)
        spans = paypal_client.windows(start, end)
        self.assertEqual(spans[0][0], start)
        self.assertEqual(spans[-1][1], end)
        for (_, stop), (begin, _) in zip(spans, spans[1:]):
            self.assertEqual(stop, begin)
        for begin, stop in spans:
            self.assertLessEqual((stop - begin).days, 31)

    def test_no_window_for_an_empty_range(self):
        moment = datetime.datetime(2026, 1, 1)
        self.assertEqual(paypal_client.windows(moment, moment), [])


class FeedRows(unittest.TestCase):
    def test_a_payment_is_a_deposit_and_a_payout_a_withdrawal(self):
        rows = paypal_client.feed_rows([_detail("T1", "100.00", fee="-3.20"), _detail("T2", "-40.00", event="T0100")])
        self.assertEqual([r["transaction_id"] for r in rows[:2]], ["paypal:T1:T0006:100.00", "paypal:T1:T0006:100.00:fee"])
        self.assertEqual((rows[0]["deposit"], rows[0]["withdrawal"]), (100.0, 0.0))
        self.assertEqual((rows[2]["deposit"], rows[2]["withdrawal"]), (0.0, 40.0))
        self.assertEqual(rows[0]["date"], "2026-03-04")
        self.assertEqual(rows[0]["currency"], "EUR")

    def test_a_fee_is_its_own_row(self):
        rows = paypal_client.feed_rows([_detail("T1", "100.00", fee="-3.20")])
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[1]["deposit"], rows[1]["withdrawal"]), (0.0, 3.2))
        self.assertEqual(rows[1]["description"], "PayPal fee T0006")

    def test_a_refunded_fee_is_a_deposit(self):
        rows = paypal_client.feed_rows([_detail("T1", "-10.00", fee="0.30")])
        self.assertEqual((rows[1]["deposit"], rows[1]["withdrawal"]), (0.3, 0.0))

    def test_zero_fee_adds_no_row(self):
        self.assertEqual(len(paypal_client.feed_rows([_detail("T1", "5.00", fee="0.00")])), 1)

    def test_voided_and_denied_records_are_left_out(self):
        rows = paypal_client.feed_rows([_detail("T1", "5.00", status="V"), _detail("T2", "5.00", status="D"), _detail("T3", "5.00", status="P")])
        self.assertEqual([r["reference_number"] for r in rows], ["T3"])

    def test_the_same_record_in_two_windows_gives_the_same_key(self):
        first = paypal_client.feed_rows([_detail("T1", "5.00")])
        second = paypal_client.feed_rows([_detail("T1", "5.00")])
        self.assertEqual(first[0]["transaction_id"], second[0]["transaction_id"])

    def test_two_records_of_one_transaction_do_not_collide(self):
        rows = paypal_client.feed_rows([_detail("T1", "5.00", event="T0006"), _detail("T1", "-5.00", event="T1107")])
        self.assertEqual(len({r["transaction_id"] for r in rows}), 2)

    def test_payer_details_stay_out_of_the_row(self):
        rows = paypal_client.feed_rows([_detail("T1", "5.00")])
        self.assertNotIn("invented@example.test", json.dumps(rows))

    def test_a_record_without_an_amount_stops_the_run(self):
        broken = {"transaction_info": {"transaction_id": "T9", "transaction_status": "S"}}
        with self.assertRaises(paypal_client.PayPalError):
            paypal_client.feed_rows([broken])


if __name__ == "__main__":
    unittest.main()
