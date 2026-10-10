"""Wise Platform API, read only: the calls the feed makes, and the feed rows they give.

Standard library only, no frappe import: the tests run without the image. The token goes in the Authorization header
of each request and nowhere else: it is not in a URL, an error or a log. An error names the HTTP status and the path,
never the response body, which holds account data.

Only GET calls. Nothing here creates a transfer, a quote or a recipient, so a token that can only read is enough.

The feed reads what works with a personal token: the balances (the end-balance check), the activities (card spend and
deposits), the transfers (payouts) and each payout's quote (its fee). Statements are not read: their endpoint answers 403
for this business profile even with SCA keys, so no statement and no SCA signing is built.
"""

import collections
import datetime
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = "https://api.wise.com"

TIMEOUT_SECONDS = 60

# the page size of the activities and the transfers; a short page is the last one
PAGE_SIZE = 100

# the activity types whose money the feed writes; the others are counted and left out (see feed_rows).
# A bank details order is a debit of its own, paid from the balance: left out, the account's start balance does not
# agree with the ledger's (the balance check shows it).
ACTIVITY_TYPES = ("CARD_PAYMENT", "BALANCE_DEPOSIT", "TRANSFER", "BANK_DETAILS_ORDER")
OUTGOING_ACTIVITY_TYPES = ("CARD_PAYMENT", "TRANSFER", "BANK_DETAILS_ORDER")
COMPLETED_ACTIVITY = "COMPLETED"
COMPLETED_TRANSFER = "outgoing_payment_sent"

# an amount as Wise writes it in activities: "-5,405 USD", "+12.50 CHF" or "12 EUR"
MONEY = re.compile(r"^\s*([+-]?)\s*(\d[\d,]*(?:\.\d+)?)\s*([A-Z]{3})\s*$")


class WiseError(Exception):
    """A failed call. The message names the status and the path, never the token or a response body."""


def _get(token, path, params=None, opener=urllib.request.urlopen):
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, method="GET")
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        raise WiseError("HTTP {} from Wise on {}".format(err.code, path)) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
        # a network failure or a body that is not JSON; the reason is a class name, the text may carry the URL
        raise WiseError("no usable answer from Wise on {} ({})".format(path, type(err).__name__)) from None


def profiles(token, opener=urllib.request.urlopen):
    """The profiles the token belongs to: a list of dicts with id and type (PERSONAL or BUSINESS)."""
    return _get(token, "/v2/profiles", opener=opener)


def balances(token, profile_id, opener=urllib.request.urlopen):
    """The standard and savings balances of a profile: a list of dicts with id, currency and amount."""
    return _get(token, "/v4/profiles/{}/balances".format(profile_id), {"types": "STANDARD,SAVINGS"}, opener=opener)


def activities(token, profile_id, start, opener=urllib.request.urlopen):
    """The profile's activities created at or after start (a datetime, UTC), newest first, paged by nextCursor."""
    path = "/v1/profiles/{}/activities".format(profile_id)
    out = []
    cursor = None
    while True:
        params = {"size": PAGE_SIZE}
        if cursor:
            params["nextCursor"] = cursor
        body = _get(token, path, params, opener=opener)
        items = body.get("activities") or []
        out += items
        cursor = body.get("nextCursor") or (body.get("cursor") or {}).get("nextCursor")
        # newest first: once a page reaches before start, the older pages are not wanted
        if not cursor or not items or _created(items[-1]) < start:
            break
    return [item for item in out if _created(item) >= start]


def transfers(token, profile_id, start, end, opener=urllib.request.urlopen):
    """The profile's transfers created between start and end (datetimes, UTC): a list, paged by offset."""
    path = "/v1/transfers"
    out = []
    offset = 0
    while True:
        params = {
            "profile": profile_id,
            "createdDateStart": _utc(start),
            "createdDateEnd": _utc(end),
            "limit": PAGE_SIZE,
            "offset": offset,
        }
        page = _get(token, path, params, opener=opener)
        if not isinstance(page, list):
            raise WiseError("unexpected answer from Wise on {}".format(path))
        out += page
        if len(page) < PAGE_SIZE:
            return out
        offset += PAGE_SIZE


def quote(token, profile_id, quote_uuid, opener=urllib.request.urlopen):
    """One quote of a transfer: the JSON with its payment options, the fee among them."""
    return _get(token, "/v3/profiles/{}/quotes/{}".format(profile_id, quote_uuid), opener=opener)


def recipient(token, account_id, opener=urllib.request.urlopen):
    """One recipient account of a payout: the JSON, with its ownedByCustomer flag. Its other details are not kept."""
    return _get(token, "/v1/accounts/{}".format(account_id), opener=opener)


def payout_recipients(token, transfer_items, opener=urllib.request.urlopen):
    """How the completed payouts to another currency go, by their recipient's ownedByCustomer: the counts.

    Only the flag is counted; the recipient's details are not kept. A payout to an account the customer owns still leaves
    the CHF balance, so its CHF outflow is written; the deposit on the account in its own currency is not, as no foreign
    balance is held. A recipient that cannot be read is counted as unknown, so the report shows it.
    """
    counts = collections.Counter()
    owned = {}
    for item in transfer_items:
        target = item.get("targetAccount")
        if item.get("status") != COMPLETED_TRANSFER or item.get("sourceCurrency") == item.get("targetCurrency"):
            continue
        if target not in owned:
            try:
                owned[target] = bool(recipient(token, target, opener=opener).get("ownedByCustomer"))
            except WiseError:
                owned[target] = None
        if owned[target] is None:
            counts["unknown"] += 1
        else:
            counts["owned" if owned[target] else "not_owned"] += 1
    return dict(counts)


def quote_fee(quote_json):
    """The fee of a quote paid from the balance, as a positive number, or None when the quote has no balance pay-in option.

    The fee is the BALANCE pay-in option's fee.total, else its price.total, the first of the two that is not zero: the
    price.total is a money object (its amount sits under value), which read as zero for every payout of the first dry run.
    The feed books it in CHF as its own line.
    """
    for option in quote_json.get("paymentOptions") or []:
        if option.get("payIn") == "BALANCE":
            fee = _number((option.get("fee") or {}).get("total"))
            return abs(fee or _number((option.get("price") or {}).get("total")))
    return None


def feed_rows(activity_items, transfer_items, fees):
    """The feed rows of the activities and the transfers, and what was left out: (rows, skipped).

    A row is {"transaction_id", "date", "deposit", "withdrawal", "currency", "description", "reference_number", "chf",
    "kind"}. chf is the CHF value the source gave (the activity's secondaryAmount, or the fee itself), or None: the day's
    rate is taken later. kind is the activity type, TRANSFER for a payout, or FEE for its fee line.

    transaction_id is 'wise:activity:<id>' for an activity and 'wise:transfer:<id>' for a payout (its fee has ':fee'
    after): a payout seen both in the activities and in the transfers gives one id, so it is written once and a rerun
    writes nothing. fees: {transfer id: the CHF fee} for the payouts whose quote was read.

    skipped counts what is left out, by reason: a status that is not completed, a type the feed does not write, a
    payout that is the mirror of a balance deposit. The caller reports it, so the types are seen in a dry run.
    """
    skipped = collections.Counter()
    activities = [_activity(item) for item in activity_items]
    transfer_ids = {str(t["id"]) for t in transfer_items}
    # a balance deposit can come from a payout of the same id: the deposit is kept, the payout is its mirror
    deposit_ids = {a["resource_id"] for a in activities if a["type"] == "BALANCE_DEPOSIT" and a["resource_type"] == "TRANSFER"}
    rows = []
    for a in activities:
        if a["status"] != COMPLETED_ACTIVITY:
            skipped["activity status {}".format(a["status"])] += 1
            continue
        if a["type"] not in ACTIVITY_TYPES:
            skipped["activity type {}".format(a["type"])] += 1
            continue
        if a["type"] == "TRANSFER":
            # a payout the transfers answer for is written from there, with its fee; this is the same payout
            if a["resource_id"] in transfer_ids:
                skipped["payout also in the transfers"] += 1
                continue
            # without a resource id the activity's own id keys it, so two such payouts do not share one id
            transaction_id = "wise:transfer:{}".format(a["resource_id"]) if a["resource_id"] else "wise:activity:{}".format(a["id"])
        else:
            transaction_id = "wise:activity:{}".format(a["id"])
        if a["secondary_unread"]:
            # without the debited balance's amount the row would land on the primary currency's account, which the feed
            # writes only for a balance Wise held: so it is left out and counted, its shape shown
            skipped["activity secondary amount not read: {} ({})".format(a["type"], amount_shape(a["raw_secondary"]))] += 1
            continue
        if a["amount"] is None or a["amount"][0] == 0:
            skipped["activity without an amount: {} ({})".format(a["type"], amount_shape(a["raw_amount"]))] += 1
            continue
        value, currency = a["amount"]
        rows.append(_row(transaction_id, a["date"], value, currency, a["description"], a["id"], a["chf"], a["type"]))
    for t in transfer_items:
        transfer_id = str(t["id"])
        if t.get("status") != COMPLETED_TRANSFER:
            skipped["transfer status {}".format(t.get("status"))] += 1
            continue
        if transfer_id in deposit_ids and t.get("sourceCurrency") == t.get("targetCurrency"):
            skipped["payout mirrors a balance deposit"] += 1
            continue
        date = _day(t.get("created"))
        reference = strip_html(t.get("reference")) or "Wise transfer"
        principal = float(t.get("sourceValue") or 0)
        if not principal:
            skipped["transfer without an amount"] += 1
            continue
        rows.append(_row("wise:transfer:" + transfer_id, date, -principal, t["sourceCurrency"], reference, transfer_id, None, "TRANSFER"))
        fee = fees.get(transfer_id)
        if fee:
            rows.append(_row("wise:transfer:{}:fee".format(transfer_id), date, -fee, "CHF", "Wise fee: " + reference, transfer_id, fee, "FEE"))
    return rows, skipped


def parse_money(text):
    """A Wise amount string such as '-5,405 USD' as (number, currency), or None when it is not one."""
    if not isinstance(text, str):
        return None
    found = MONEY.match(text)
    if not found:
        return None
    sign, digits, currency = found.groups()
    number = float(digits.replace(",", ""))
    return (-number if sign == "-" else number), currency


def amount_shape(text):
    """The format of an amount string without its value: digits as 9, a currency code as CUR, other odd characters as
    their code point. A dry run reports it, so a format the parser does not read is seen without any amount being printed.
    """
    if text is None:
        return "absent"
    if not isinstance(text, str):
        return "not text: " + type(text).__name__
    shape = re.sub(r"\b[A-Z]{3}\b", "CUR", re.sub(r"\d", "9", text))
    return "".join(ch if ch.isascii() else "<U+{:04X}>".format(ord(ch)) for ch in shape)


def strip_html(text):
    """The text with its tags removed, its entities unescaped and its whitespace collapsed: Wise's titles carry HTML."""
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", text or "")).split())


def _plain(value):
    """An amount or title as plain text when it is text, else as it is (None stays None, so the parse finds nothing)."""
    return strip_html(value) if isinstance(value, str) else value


def _activity(item):
    """The fields the feed reads from one activity, as a dict. Missing parts are None, so the caller counts them.

    The amount is signed: an explicit sign in the text wins, otherwise the type gives the direction (a card payment and
    a payout leave the balance, a deposit comes in), since Wise does not always sign its amounts.

    The amount is the one of the balance Wise debited: the secondaryAmount's currency when Wise gives one (a card paid
    from CHF in another currency), else the primaryAmount's. So a foreign-currency card payment is a CHF movement.
    """
    resource = item.get("resource") or {}
    # Wise wraps an amount in markup, as <positive>+ 5,405 USD</positive>: the tags are taken off before the parse
    primary = _plain(item.get("primaryAmount"))
    secondary_text = _plain(item.get("secondaryAmount"))
    secondary = parse_money(secondary_text)
    amount = parse_money(primary)
    if amount:
        value, currency = amount
        if not primary.lstrip().startswith(("+", "-")):
            value = -abs(value) if item.get("type") in OUTGOING_ACTIVITY_TYPES else abs(value)
        amount = (value, currency)
    if amount and secondary:
        # the sign comes from the primary, the size and the currency from the secondary: the balance that was debited
        value = -abs(secondary[0]) if amount[0] < 0 else abs(secondary[0])
        amount = (value, secondary[1])
    return {
        "id": str(item.get("id")),
        "type": item.get("type"),
        "status": item.get("status"),
        "date": _day(item.get("createdOn")),
        "amount": amount,
        "raw_amount": item.get("primaryAmount"),
        # an empty secondaryAmount is no secondary (a CHF movement has one as ""), a filled one that does not parse is unread
        "secondary_unread": bool(secondary_text and str(secondary_text).strip()) and secondary is None,
        "raw_secondary": item.get("secondaryAmount"),
        "chf": abs(secondary[0]) if secondary and secondary[1] == "CHF" else None,
        "description": strip_html(item.get("title")) or strip_html(item.get("description")) or item.get("type") or "",
        "resource_type": resource.get("type"),
        "resource_id": str(resource["id"]) if resource.get("id") is not None else None,
    }


def _row(transaction_id, date, value, currency, description, reference, chf, kind):
    """A feed row: a value above zero is a deposit, below zero a withdrawal."""
    return {
        "transaction_id": transaction_id,
        "date": date,
        "deposit": value if value > 0 else 0.0,
        "withdrawal": -value if value < 0 else 0.0,
        "currency": currency,
        "description": description,
        "reference_number": reference or "",
        "chf": round(abs(chf), 2) if chf is not None else None,
        "kind": kind,
    }


def _created(item):
    """The creation moment of an activity as a naive UTC datetime; an item without one counts as the earliest."""
    stamp = item.get("createdOn") or ""
    try:
        return datetime.datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return datetime.datetime.min


def _day(stamp):
    """The day of a Wise timestamp, YYYY-MM-DD, from '2026-03-04T10:00:00.000Z' or '2026-03-04 10:00:00'."""
    return (stamp or "")[:10]


def _number(value):
    """A money value as a number: a money object's value (nested objects too), a number, or an amount string; 0.0 when absent."""
    while isinstance(value, dict):
        value = value.get("value", value.get("amount"))
    if isinstance(value, (int, float)):
        return float(value)
    parsed = parse_money(value)
    if parsed:
        return parsed[0]
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _utc(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")
