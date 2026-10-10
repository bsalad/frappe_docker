"""Wise Platform API, read only: the three calls the feed makes, and the statement rows they give.

Standard library only, no frappe import: the tests run without the image. The token goes in the Authorization header
of each request and nowhere else: it is not in a URL, an error or a log. An error names the HTTP status and the path,
never the response body, which holds account data.

Only GET calls. Nothing here creates a transfer, a quote or a recipient, so a token that can only read is enough.
"""

import datetime
import json
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.wise.com"

# a statement request covers at most 469 days (Wise's limit); 460 leaves a margin for the window's ends
STATEMENT_WINDOW_DAYS = 460

TIMEOUT_SECONDS = 60


class WiseError(Exception):
    """A failed call. The message names the status and the path, never the token or a response body."""


class ScaRequired(WiseError):
    """Wise asks for strong customer authentication on this call. The feed cannot approve it; nothing is written."""


def _get(token, path, params=None, opener=urllib.request.urlopen):
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, method="GET")
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        # a 403 with an approval header is Wise's SCA challenge; the approval flow is not built, so it stops here
        if err.code == 403 and err.headers.get("x-2fa-approval"):
            raise ScaRequired("Wise requires SCA approval for {}".format(path)) from None
        raise WiseError("HTTP {} from Wise on {}".format(err.code, path)) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
        # a network failure or a body that is not JSON; the reason is a class name, the text may carry the URL
        raise WiseError("no usable answer from Wise on {} ({})".format(path, type(err).__name__)) from None


def profiles(token, opener=urllib.request.urlopen):
    """The profiles the token belongs to: a list of dicts with id and type (PERSONAL or BUSINESS)."""
    return _get(token, "/v1/profiles", opener=opener)


def balances(token, profile_id, opener=urllib.request.urlopen):
    """The standard balances of a profile, one per currency: a list of dicts with id and currency."""
    return _get(token, "/v4/profiles/{}/balances".format(profile_id), {"types": "STANDARD"}, opener=opener)


def statement(token, profile_id, balance_id, currency, start, end, opener=urllib.request.urlopen):
    """The statement of one balance between two datetimes (UTC): the JSON with its transactions."""
    path = "/v1/profiles/{}/balance-statements/{}/statement.json".format(profile_id, balance_id)
    params = {
        "currency": currency,
        "intervalStart": _utc(start),
        "intervalEnd": _utc(end),
        "type": "FLAT",
    }
    return _get(token, path, params, opener=opener)


def windows(start, end):
    """The (start, end) pairs of consecutive statement requests that cover start to end, each within the limit."""
    step = datetime.timedelta(days=STATEMENT_WINDOW_DAYS)
    out = []
    current = start
    while current < end:
        stop = min(current + step, end)
        out.append((current, stop))
        current = stop
    return out


def _utc(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def statement_rows(statement_json, balance_id, currency):
    """The feed rows of one statement: a dict per transaction and per fee, keyed by Wise's own ids.

    A row is {"transaction_id", "date", "deposit", "withdrawal", "currency", "description", "reference_number"}, the
    fields the bank feed writes to a Bank Transaction. A transaction with a fee gets a second row for the fee, so the fee
    is its own line in ERPNext. Amounts are unsigned in Wise's JSON; the type (DEBIT or CREDIT) gives the direction.
    """
    rows = []
    for index, item in enumerate(statement_json.get("transactions", [])):
        reference = item.get("referenceNumber") or ""
        # the reference is Wise's id for the movement; without one, the position in the statement is the id, which
        # is stable only while the statement is unchanged, so the feed does not rely on it for a closed period
        key = "wise:{}:{}".format(balance_id, reference or "pos{}".format(index))
        amount = _money(item.get("amount"))
        direction = item.get("type")
        if amount is None or direction not in ("CREDIT", "DEBIT"):
            raise WiseError("a statement transaction has no usable amount or type")
        description = ((item.get("details") or {}).get("description")) or ""
        rows.append(_row(key, item, amount, direction, currency, description, reference))
        fee = _money(item.get("totalFees"))
        if fee:
            rows.append(_row(key + ":fee", item, fee, "DEBIT", currency, "Wise fee: " + description, reference))
    return rows


def _row(transaction_id, item, amount, direction, currency, description, reference):
    deposit = amount if direction == "CREDIT" else 0.0
    withdrawal = amount if direction == "DEBIT" else 0.0
    return {
        "transaction_id": transaction_id,
        "date": (item.get("date") or "")[:10],
        "deposit": deposit,
        "withdrawal": withdrawal,
        "currency": currency,
        "description": description,
        "reference_number": reference,
    }


def _money(value):
    """The amount of a Wise money object as a positive float, or None when it is absent or zero."""
    if not value:
        return None
    number = round(abs(float(value.get("value") or 0)), 2)
    return number or None
