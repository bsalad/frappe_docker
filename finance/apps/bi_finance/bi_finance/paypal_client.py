"""PayPal Transaction Search, read only: the token, the transactions and the balances the feed reads, and the rows they give.

Standard library only, no frappe import: the tests run without the image. The client secret goes in the Authorization
header of the token request and nowhere else; the access token goes in the Authorization header of each call. An error
names the HTTP status and the path, never a response body (it holds account data) and never a credential.

Only GET calls, and the one token call. Nothing here creates a payment, a payout or a refund.
"""

import base64
import datetime
import json
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api-m.paypal.com"

# a transactions request covers at most 31 days (PayPal's limit); 30 leaves a margin for the window's ends
TRANSACTION_WINDOW_DAYS = 30

# PayPal keeps three years of transactions; an older start is refused, so the feed starts no earlier
HISTORY_DAYS = 3 * 365

PAGE_SIZE = 500

TIMEOUT_SECONDS = 60

# statuses that move no money: a voided or denied payment is listed, and the feed leaves it out
SKIPPED_STATUSES = ("V", "D")


class PayPalError(Exception):
    """A failed call. The message names the status and the path, never a credential or a response body."""


def _request(path, opener, headers, data=None, params=None):
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, data=data, headers=headers, method="GET" if data is None else "POST")
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        raise PayPalError("HTTP {} from PayPal on {}".format(err.code, path)) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
        # a network failure or a body that is not JSON; the reason is a class name, the text may carry the URL
        raise PayPalError("no usable answer from PayPal on {} ({})".format(path, type(err).__name__)) from None


def _bearer(token):
    return {"Authorization": "Bearer " + token, "Accept": "application/json"}


def access_token(client_id, client_secret, opener=urllib.request.urlopen):
    """An access token for the REST app, by the client credentials grant. The secret is sent once, in this call only."""
    basic = base64.b64encode("{}:{}".format(client_id, client_secret).encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": "Basic " + basic,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("ascii")
    answer = _request("/v1/oauth2/token", opener, headers, data=body)
    token = answer.get("access_token") if isinstance(answer, dict) else None
    if not token:
        raise PayPalError("PayPal gave no access token")
    return token


def transactions(token, start, end, opener=urllib.request.urlopen):
    """Every transaction_details record between two datetimes (UTC), over all pages. start and end are at most a window apart."""
    records = []
    page = 1
    while True:
        answer = _request(
            "/v1/reporting/transactions",
            opener,
            _bearer(token),
            params={
                "start_date": _rfc3339(start),
                "end_date": _rfc3339(end),
                "fields": "transaction_info",
                # the records that move money, as a bank statement shows them
                "balance_affecting_records_only": "Y",
                "page_size": PAGE_SIZE,
                "page": page,
            },
        )
        records += answer.get("transaction_details") or []
        if page >= int(answer.get("total_pages") or 1):
            return records
        page += 1


def balances(token, opener=urllib.request.urlopen):
    """The total balance of each currency the account holds now: {currency: amount}."""
    answer = _request("/v1/reporting/balances", opener, _bearer(token))
    found = {}
    for item in answer.get("balances") or []:
        total = item.get("total_balance") or {}
        currency = item.get("currency") or total.get("currency_code")
        if currency:
            found[currency] = round(float(total.get("value") or 0), 2)
    return found


def windows(start, end):
    """The (start, end) pairs of consecutive transaction requests that cover start to end, each within the limit."""
    step = datetime.timedelta(days=TRANSACTION_WINDOW_DAYS)
    out = []
    current = start
    while current < end:
        stop = min(current + step, end)
        out.append((current, stop))
        current = stop
    return out


def feed_rows(details):
    """The feed rows of the transaction_details records: a dict per movement and per fee, keyed by PayPal's own ids.

    A row is {"transaction_id", "date", "deposit", "withdrawal", "currency", "description", "reference_number"}, the fields
    the bank feed writes to a Bank Transaction. The key is the transaction id with its event code and signed amount, so a
    record repeated across windows gives the same key and a second record of one transaction does not collide with the
    first. A fee is its own row, as Wise's are. The description is the event code only: payer details stay in PayPal.
    """
    rows = []
    for detail in details:
        info = detail.get("transaction_info") or {}
        if info.get("transaction_status") in SKIPPED_STATUSES:
            continue
        amount = info.get("transaction_amount") or {}
        tid = info.get("transaction_id")
        currency = amount.get("currency_code")
        if not tid or not currency or amount.get("value") is None:
            raise PayPalError("a transaction has no id, currency or amount")
        event = info.get("transaction_event_code") or ""
        date = (info.get("transaction_initiation_date") or "")[:10]
        key = "paypal:{}:{}:{:.2f}".format(tid, event, float(amount["value"]))
        gross = float(amount["value"])
        if gross:
            rows.append(_row(key, date, gross, currency, "PayPal " + event, tid))
        fee = info.get("fee_amount") or {}
        if fee.get("value") and float(fee["value"]):
            rows.append(_row(key + ":fee", date, float(fee["value"]), currency, "PayPal fee " + event, tid))
    return rows


def _row(transaction_id, date, value, currency, description, reference):
    """A feed row for a signed amount: positive is money in (deposit), negative money out (withdrawal)."""
    return {
        "transaction_id": transaction_id,
        "date": date,
        "deposit": round(value, 2) if value > 0 else 0.0,
        "withdrawal": round(-value, 2) if value < 0 else 0.0,
        "currency": currency,
        "description": description,
        "reference_number": reference,
    }


def _rfc3339(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
