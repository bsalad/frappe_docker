"""Offline tests for the Wise client: the HTTP calls (a fake opener, no network), the amounts, and the feed rows.

Invented data only: made-up ids, amounts, references and titles, no real activity, transfer, quote or token. The module
imports no frappe, so this runs on the host: it is part of the depot's light gate (.yardr/check). The frappe side
(the sync, the balances, the rates) is in test_wise.py.
"""

import datetime
import io
import json
import unittest
import urllib.error

from bi_finance import wise_client

TOKEN = "invented-token-0000"


class FakeOpener:
    """Stands in for urlopen: answers the calls in turn with the given JSON bodies (the last one again), or raises the error."""

    def __init__(self, *answers, error=None):
        self.answers = list(answers)
        self.error = error
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.error:
            raise self.error
        answer = self.answers[min(len(self.requests), len(self.answers)) - 1]
        return io.BytesIO(json.dumps(answer).encode("utf-8"))


def _activity(item_id, kind, value, status="COMPLETED", created="2026-03-04T10:00:00.000Z", title="Invented Shop",
              secondary=None, resource=None):
    item = {"id": item_id, "type": kind, "status": status, "createdOn": created, "primaryAmount": value, "title": title}
    if secondary is not None:
        item["secondaryAmount"] = secondary
    if resource is not None:
        item["resource"] = {"type": "TRANSFER", "id": resource}
    return item


def _transfer(transfer_id, source="EUR", target="EUR", value=100.0, status="outgoing_payment_sent", quote="Q-1",
              reference="Invoice 77"):
    return {
        "id": transfer_id,
        "sourceCurrency": source,
        "targetCurrency": target,
        "sourceValue": value,
        "status": status,
        "quoteUuid": quote,
        "created": "2026-03-04 10:00:00",
        "reference": reference,
    }


class Client(unittest.TestCase):
    def test_profiles_sends_bearer_token_and_no_token_in_url(self):
        opener = FakeOpener([{"id": 1, "type": "BUSINESS"}])
        self.assertEqual(wise_client.profiles(TOKEN, opener=opener), [{"id": 1, "type": "BUSINESS"}])
        request = opener.requests[0]
        self.assertEqual(request.full_url, wise_client.BASE_URL + "/v2/profiles")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, request.full_url)

    def test_balances_asks_for_standard_and_savings(self):
        opener = FakeOpener([])
        wise_client.balances(TOKEN, 42, opener=opener)
        self.assertEqual(opener.requests[0].full_url, wise_client.BASE_URL + "/v4/profiles/42/balances?types=STANDARD%2CSAVINGS")

    def test_http_error_names_status_and_path_only(self):
        error = urllib.error.HTTPError(wise_client.BASE_URL, 500, "Server Error", {}, io.BytesIO(b"account body"))
        with self.assertRaises(wise_client.WiseError) as caught:
            wise_client.profiles(TOKEN, opener=FakeOpener(error=error))
        message = str(caught.exception)
        self.assertIn("HTTP 500", message)
        self.assertIn("/v2/profiles", message)
        self.assertNotIn("account body", message)
        self.assertNotIn(TOKEN, message)

    def test_network_failure_is_a_wise_error(self):
        with self.assertRaises(wise_client.WiseError):
            wise_client.profiles(TOKEN, opener=FakeOpener(error=urllib.error.URLError("invented")))


class Activities(unittest.TestCase):
    start = datetime.datetime(2026, 3, 1)

    def test_pages_follow_the_next_cursor(self):
        first = {
            "activities": [_activity("A3", "CARD_PAYMENT", "-5 EUR", created="2026-03-06T08:00:00.000Z"),
                           _activity("A2", "CARD_PAYMENT", "-6 EUR", created="2026-03-05T08:00:00.000Z")],
            "cursor": {"nextCursor": "C1"},
        }
        second = {"activities": [_activity("A1", "CARD_PAYMENT", "-7 EUR", created="2026-02-28T08:00:00.000Z")], "cursor": {}}
        opener = FakeOpener(first, second)
        items = wise_client.activities(TOKEN, 42, self.start, opener=opener)
        self.assertEqual([i["id"] for i in items], ["A3", "A2"])
        self.assertEqual(len(opener.requests), 2)
        self.assertIn("/v1/profiles/42/activities?size=100", opener.requests[0].full_url)
        self.assertIn("nextCursor=C1", opener.requests[1].full_url)

    def test_stops_paging_once_a_page_reaches_before_the_start(self):
        page = {
            "activities": [_activity("A2", "CARD_PAYMENT", "-6 EUR", created="2026-03-05T08:00:00.000Z"),
                           _activity("A1", "CARD_PAYMENT", "-7 EUR", created="2026-02-20T08:00:00.000Z")],
            "nextCursor": "C1",
        }
        opener = FakeOpener(page)
        items = wise_client.activities(TOKEN, 42, self.start, opener=opener)
        self.assertEqual([i["id"] for i in items], ["A2"])
        self.assertEqual(len(opener.requests), 1)


class Transfers(unittest.TestCase):
    def test_pages_by_offset_until_a_short_page(self):
        full = [_transfer(i) for i in range(wise_client.PAGE_SIZE)]
        opener = FakeOpener(full, [_transfer(500)])
        items = wise_client.transfers(TOKEN, 42, datetime.datetime(2026, 3, 1), datetime.datetime(2026, 3, 10), opener=opener)
        self.assertEqual(len(items), wise_client.PAGE_SIZE + 1)
        self.assertIn("offset=0", opener.requests[0].full_url)
        self.assertIn("offset=100", opener.requests[1].full_url)
        self.assertIn("profile=42", opener.requests[0].full_url)
        self.assertIn("createdDateStart=2026-03-01T00%3A00%3A00.000Z", opener.requests[0].full_url)

    def test_an_answer_that_is_not_a_list_is_an_error(self):
        with self.assertRaises(wise_client.WiseError):
            wise_client.transfers(TOKEN, 42, datetime.datetime(2026, 3, 1), datetime.datetime(2026, 3, 10), opener=FakeOpener({"x": 1}))


class Quotes(unittest.TestCase):
    def test_the_fee_is_the_balance_pay_in_price_total(self):
        quote = {"paymentOptions": [
            {"payIn": "BANK_TRANSFER", "price": {"total": {"value": 9.0, "currency": "EUR"}}},
            {"payIn": "BALANCE", "price": {"total": {"value": 1.25, "currency": "CHF"}}},
        ]}
        self.assertEqual(wise_client.quote_fee(quote), 1.25)

    def test_a_price_total_given_as_text_is_read(self):
        quote = {"paymentOptions": [{"payIn": "BALANCE", "price": {"total": "2.50 CHF"}}]}
        self.assertEqual(wise_client.quote_fee(quote), 2.5)

    def test_a_zero_fee_is_zero_not_absent(self):
        quote = {"paymentOptions": [{"payIn": "BALANCE", "price": {"total": {"value": 0}}}]}
        self.assertEqual(wise_client.quote_fee(quote), 0.0)

    def test_no_balance_option_gives_none(self):
        quote = {"paymentOptions": [{"payIn": "BANK_TRANSFER", "price": {"total": {"value": 9.0}}}]}
        self.assertIsNone(wise_client.quote_fee(quote))

    def test_a_price_total_with_its_amount_nested_under_value_is_read(self):
        # the regression: the money object's amount sits one level down, under value, and read as zero before
        quote = {"paymentOptions": [{"payIn": "BALANCE", "price": {"total": {"value": {"amount": 1.25, "currency": "CHF"}}}}]}
        self.assertEqual(wise_client.quote_fee(quote), 1.25)

    def test_the_fee_total_is_read_before_the_price_total(self):
        quote = {"paymentOptions": [{"payIn": "BALANCE", "fee": {"total": 0.8}, "price": {"total": {"value": {"amount": 2.0}}}}]}
        self.assertEqual(wise_client.quote_fee(quote), 0.8)

    def test_a_zero_fee_total_falls_through_to_the_price_total(self):
        quote = {"paymentOptions": [{"payIn": "BALANCE", "fee": {"total": {"value": 0}}, "price": {"total": {"value": {"amount": 0.5}}}}]}
        self.assertEqual(wise_client.quote_fee(quote), 0.5)


class Amounts(unittest.TestCase):
    def test_a_signed_amount_string_gives_its_number_and_currency(self):
        self.assertEqual(wise_client.parse_money("-5,405 USD"), (-5405.0, "USD"))
        self.assertEqual(wise_client.parse_money("+12.50 CHF"), (12.5, "CHF"))

    def test_an_unsigned_amount_is_positive(self):
        self.assertEqual(wise_client.parse_money("12 EUR"), (12.0, "EUR"))

    def test_text_that_is_not_an_amount_gives_none(self):
        self.assertIsNone(wise_client.parse_money("about five dollars"))
        self.assertIsNone(wise_client.parse_money(None))

    def test_html_is_stripped_and_entities_unescaped(self):
        self.assertEqual(wise_client.strip_html("<strong>Coffee &amp; Co</strong>  <em>shop</em>"), "Coffee & Co shop")


class FeedRows(unittest.TestCase):
    def test_a_card_payment_leaves_chf_the_balance_wise_debited_at_its_chf_value(self):
        # paid in USD from the CHF balance: the debit is the CHF secondary, the USD primary is the card's currency only
        rows, skipped = wise_client.feed_rows(
            [_activity("A1", "CARD_PAYMENT", "5,405 USD", secondary="-4,850.10 CHF", title="<b>Invented Shop</b>")], [], {}
        )
        self.assertEqual(skipped, {})
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["transaction_id"], "wise:activity:A1")
        self.assertEqual((row["deposit"], row["withdrawal"], row["currency"]), (0.0, 4850.10, "CHF"))
        self.assertEqual(row["chf"], 4850.10)
        self.assertEqual((row["description"], row["date"], row["kind"]), ("Invented Shop", "2026-03-04", "CARD_PAYMENT"))

    def test_a_foreign_amount_with_no_secondary_stays_on_its_own_currency(self):
        # no secondary: Wise held the balance, so the row is on that currency's account
        rows, _ = wise_client.feed_rows([_activity("E1", "CARD_PAYMENT", "-12 EUR")], [], {})
        self.assertEqual((rows[0]["currency"], rows[0]["withdrawal"], rows[0]["chf"]), ("EUR", 12.0, None))

    def test_a_deposit_with_a_chf_secondary_is_a_chf_deposit_at_the_secondary_value(self):
        rows, _ = wise_client.feed_rows([_activity("D5", "BALANCE_DEPOSIT", "+200 EUR", secondary="+198.00 CHF")], [], {})
        self.assertEqual((rows[0]["currency"], rows[0]["deposit"], rows[0]["chf"]), ("CHF", 198.0, 198.0))

    def test_an_empty_secondary_is_no_secondary_so_a_chf_movement_keeps_its_primary(self):
        # Wise sends secondaryAmount as "" on a movement in the balance's own currency: that is not an unread amount
        rows, skipped = wise_client.feed_rows([_activity("C2", "CARD_PAYMENT", "-7.50 CHF", secondary="")], [], {})
        self.assertEqual(skipped, {})
        self.assertEqual((rows[0]["currency"], rows[0]["withdrawal"], rows[0]["chf"]), ("CHF", 7.5, None))

    def test_a_secondary_the_parser_does_not_read_is_left_out_and_counted_not_put_on_the_primary(self):
        rows, skipped = wise_client.feed_rows([_activity("A9", "CARD_PAYMENT", "-5 USD", secondary="about 4 francs")], [], {})
        self.assertEqual(rows, [])
        self.assertEqual(skipped["activity secondary amount not read: CARD_PAYMENT (about 9 francs)"], 1)

    def test_a_deposit_comes_in_unsigned_or_signed(self):
        rows, _ = wise_client.feed_rows(
            [_activity("D1", "BALANCE_DEPOSIT", "1,000 CHF"), _activity("D2", "BALANCE_DEPOSIT", "+250 CHF")], [], {}
        )
        self.assertEqual([(r["deposit"], r["withdrawal"]) for r in rows], [(1000.0, 0.0), (250.0, 0.0)])

    def test_an_explicit_minus_wins_over_the_type(self):
        rows, _ = wise_client.feed_rows([_activity("D1", "BALANCE_DEPOSIT", "-40 CHF")], [], {})
        self.assertEqual((rows[0]["deposit"], rows[0]["withdrawal"]), (0.0, 40.0))

    def test_a_status_that_is_not_completed_is_left_out_and_counted(self):
        rows, skipped = wise_client.feed_rows([_activity("A1", "CARD_PAYMENT", "-5 CHF", status="PENDING")], [], {})
        self.assertEqual(rows, [])
        self.assertEqual(skipped["activity status PENDING"], 1)

    def test_a_type_the_feed_does_not_write_is_left_out_and_counted(self):
        rows, skipped = wise_client.feed_rows([_activity("C1", "CARD_CHECK", "-1 CHF"), _activity("X1", "CONVERSION", "-5 CHF")], [], {})
        self.assertEqual(rows, [])
        self.assertEqual(skipped["activity type CARD_CHECK"], 1)
        self.assertEqual(skipped["activity type CONVERSION"], 1)

    def test_a_bank_details_order_is_a_chf_debit_and_nets_off_the_deposit_it_sits_beside(self):
        # the regression: left out, the account's start came out one order's amount below the ledger's. Its amount is
        # unsigned in the activities, so the type gives the debit; the deposit beside it is signed and comes in.
        rows, skipped = wise_client.feed_rows([
            _activity("B1", "BANK_DETAILS_ORDER", "20 CHF", created="2026-01-07T14:20:00.000Z"),
            _activity("D1", "BALANCE_DEPOSIT", "<positive>+ 20 CHF</positive>", created="2026-01-07T14:20:00.400Z"),
        ], [], {})
        self.assertEqual(skipped, {})
        self.assertEqual([(r["kind"], r["currency"], r["deposit"], r["withdrawal"]) for r in rows], [
            ("BANK_DETAILS_ORDER", "CHF", 0.0, 20.0),
            ("BALANCE_DEPOSIT", "CHF", 20.0, 0.0),
        ])

    def test_a_payout_mirrored_by_a_balance_deposit_is_left_out_and_the_deposit_kept(self):
        deposit = _activity("D1", "BALANCE_DEPOSIT", "1,000 CHF", resource=500)
        mirror = _transfer(500, source="CHF", target="CHF", value=1000.0)
        rows, skipped = wise_client.feed_rows([deposit], [mirror], {})
        self.assertEqual([r["transaction_id"] for r in rows], ["wise:activity:D1"])
        self.assertEqual(skipped["payout mirrors a balance deposit"], 1)

    def test_a_payout_seen_in_activities_and_transfers_is_written_once(self):
        payout = _activity("T1", "TRANSFER", "-100 EUR", resource=600)
        rows, skipped = wise_client.feed_rows([payout], [_transfer(600, value=100.0)], {})
        self.assertEqual([r["transaction_id"] for r in rows], ["wise:transfer:600"])
        self.assertEqual(rows[0]["withdrawal"], 100.0)
        self.assertEqual(skipped["payout also in the transfers"], 1)

    def test_a_payout_activity_without_a_resource_id_is_keyed_by_its_own_id(self):
        # two such activities must not share one 'wise:transfer:None' id: the second would be dropped as already fed
        rows, _ = wise_client.feed_rows([_activity("T3", "TRANSFER", "-30 EUR"), _activity("T4", "TRANSFER", "-31 EUR")], [], {})
        self.assertEqual([r["transaction_id"] for r in rows], ["wise:activity:T3", "wise:activity:T4"])

    def test_a_payout_only_in_activities_is_kept_under_the_same_id(self):
        rows, _ = wise_client.feed_rows([_activity("T2", "TRANSFER", "-30 EUR", resource=601)], [], {})
        self.assertEqual([r["transaction_id"] for r in rows], ["wise:transfer:601"])

    def test_a_payout_is_its_principal_and_its_fee_is_a_chf_line(self):
        rows, _ = wise_client.feed_rows([], [_transfer(700, value=100.0, reference="Invoice 78")], {"700": 1.25})
        self.assertEqual([r["transaction_id"] for r in rows], ["wise:transfer:700", "wise:transfer:700:fee"])
        principal, fee = rows
        self.assertEqual((principal["currency"], principal["withdrawal"], principal["kind"]), ("EUR", 100.0, "TRANSFER"))
        self.assertEqual((fee["currency"], fee["withdrawal"], fee["chf"], fee["kind"]), ("CHF", 1.25, 1.25, "FEE"))
        self.assertEqual(fee["description"], "Wise fee: Invoice 78")

    def test_a_payout_without_a_fee_has_no_fee_line(self):
        rows, _ = wise_client.feed_rows([], [_transfer(701)], {})
        self.assertEqual([r["transaction_id"] for r in rows], ["wise:transfer:701"])

    def test_a_payout_that_is_not_completed_is_left_out(self):
        rows, skipped = wise_client.feed_rows([], [_transfer(702, status="processing")], {})
        self.assertEqual(rows, [])
        self.assertEqual(skipped["transfer status processing"], 1)

    def test_an_activity_without_an_amount_is_left_out_and_counted(self):
        rows, skipped = wise_client.feed_rows([_activity("Z1", "CARD_PAYMENT", None)], [], {})
        self.assertEqual(rows, [])
        self.assertEqual(skipped["activity without an amount: CARD_PAYMENT (absent)"], 1)

    def test_an_amount_wrapped_in_markup_is_read_as_its_plain_text(self):
        # Wise writes "<positive>+ 1,000 CHF</positive>": the tags go, and the explicit sign still wins
        deposit = _activity("D8", "BALANCE_DEPOSIT", "<positive>+ 1,000 CHF</positive>")
        card = _activity("A8", "CARD_PAYMENT", "<negative>- 5,405 USD</negative>", secondary="<negative>-4,850.10 CHF</negative>")
        rows, skipped = wise_client.feed_rows([deposit, card], [], {})
        self.assertEqual(skipped, {})
        self.assertEqual([(r["deposit"], r["withdrawal"], r["currency"]) for r in rows], [(1000.0, 0.0, "CHF"), (0.0, 4850.10, "CHF")])
        self.assertEqual(rows[1]["chf"], 4850.10)

    def test_an_amount_the_parser_does_not_read_is_reported_by_its_shape_not_its_value(self):
        # a thin space and an apostrophe as the thousands mark: the shape shows them, the digits are 9s
        rows, skipped = wise_client.feed_rows([_activity("D9", "BALANCE_DEPOSIT", "1 000’500 CHF")], [], {})
        self.assertEqual(rows, [])
        self.assertEqual(skipped["activity without an amount: BALANCE_DEPOSIT (9<U+2009>999<U+2019>999 CUR)"], 1)

    def test_the_shape_of_an_amount_keeps_its_format_and_hides_its_value(self):
        self.assertEqual(wise_client.amount_shape("-5,405.10 USD"), "-9,999.99 CUR")
        self.assertEqual(wise_client.amount_shape(None), "absent")
        self.assertEqual(wise_client.amount_shape(12), "not text: int")


class PayoutRecipients(unittest.TestCase):
    def _payout(self, transfer_id, target=900, **kwargs):
        item = _transfer(transfer_id, source="CHF", target="EUR", **kwargs)
        item["targetAccount"] = target
        return item

    def test_the_recipient_is_read_by_its_account_id_and_only_the_flag_is_counted(self):
        opener = FakeOpener({"ownedByCustomer": False, "holderName": "Invented Holder"})
        counts = wise_client.payout_recipients(TOKEN, [self._payout(1)], opener=opener)
        self.assertEqual(counts, {"not_owned": 1})
        self.assertTrue(opener.requests[0].full_url.endswith("/v1/accounts/900"))
        self.assertNotIn(TOKEN, opener.requests[0].full_url)

    def test_a_payout_to_an_account_the_customer_owns_is_counted_as_owned(self):
        counts = wise_client.payout_recipients(TOKEN, [self._payout(2)], opener=FakeOpener({"ownedByCustomer": True}))
        self.assertEqual(counts, {"owned": 1})

    def test_the_same_recipient_is_asked_once(self):
        opener = FakeOpener({"ownedByCustomer": False})
        counts = wise_client.payout_recipients(TOKEN, [self._payout(3), self._payout(4)], opener=opener)
        self.assertEqual(counts, {"not_owned": 2})
        self.assertEqual(len(opener.requests), 1)

    def test_a_recipient_that_cannot_be_read_is_counted_as_unknown(self):
        error = urllib.error.HTTPError("https://api.wise.com/v1/accounts/900", 403, "Forbidden", {}, io.BytesIO(b"{}"))
        counts = wise_client.payout_recipients(TOKEN, [self._payout(5)], opener=FakeOpener(error=error))
        self.assertEqual(counts, {"unknown": 1})

    def test_only_completed_payouts_to_another_currency_are_asked(self):
        same = self._payout(6)
        same["targetCurrency"] = "CHF"
        counts = wise_client.payout_recipients(TOKEN, [same, self._payout(7, status="processing")], opener=FakeOpener({}))
        self.assertEqual(counts, {})


if __name__ == "__main__":
    unittest.main()
