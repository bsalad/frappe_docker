"""The bank feed shared by the bank APIs (Wise and PayPal): feed rows written as submitted Bank Transactions.

A row is the dict wise_client.statement_rows gives: transaction_id, date, deposit, withdrawal, currency, description,
reference_number. Its transaction_id is the external id. A Bank Transaction with that id is left alone on every later
run, so a second run writes nothing.

A period already imported by bexio has no transaction_id, so a row is matched to it by account, date and amount instead.
Each such Bank Transaction takes one feed row, so the feed does not write a movement twice. The count matters: two
movements of the same amount on the same day are both kept. The reference is not compared, because bexio's lines have
none.

The matching is in new_rows, which reads nothing, so it is tested without a database. plan and write are the frappe side.

The rates the conversion to CHF needs are in load_rates: ERPNext's configured source (frankfurter.dev v2), asked once
per currency and range. fetch_rates, rate_days, carry_forward and fetch_start are pure and tested without a database.
"""

import collections
import datetime
import json
import urllib.error
import urllib.parse
import urllib.request

import frappe
from frappe import _

RATE_URL = "https://api.frankfurter.dev/v2/rates"

RATE_TIMEOUT_SECONDS = 60

ONE_DAY = datetime.timedelta(days=1)


class RateError(Exception):
    """The rate source gave no usable answer. The message names the status or the error class, never the body."""


def new_rows(rows, fed_ids, imported):
    """The rows that are not yet in ERPNext: (create, summary). Pure: fed_ids and imported are read by the caller.

    fed_ids: the transaction_ids already on the account. imported: a Counter of (date, deposit, withdrawal) of the
    account's Bank Transactions that no feed wrote (bexio's). It is consumed here, so the caller's copy is not reused.
    """
    create = []
    summary = {"rows": len(rows), "already_fed": 0, "already_imported": 0}
    seen = set()
    for row in rows:
        # a movement on the edge of two statement windows comes back in both; the first one counts, as write() would
        if row["transaction_id"] in seen:
            summary["rows"] -= 1
            continue
        seen.add(row["transaction_id"])
        if row["transaction_id"] in fed_ids:
            summary["already_fed"] += 1
            continue
        key = _key(row)
        if imported[key] > 0:
            imported[key] -= 1
            summary["already_imported"] += 1
            continue
        create.append(row)
    summary["create"] = len(create)
    return create, summary


def plan(bank_account, rows):
    """What a run would write to bank_account for these rows: (create, summary). Reads ERPNext, writes nothing.

    An account that does not exist yet has no transactions, so every row is new.
    """
    if not frappe.db.exists("Bank Account", bank_account):
        return rows, {"rows": len(rows), "already_fed": 0, "already_imported": 0, "create": len(rows)}
    existing = frappe.get_all(
        "Bank Transaction",
        filters={"bank_account": bank_account, "docstatus": ["<", 2]},
        fields=["transaction_id", "date", "deposit", "withdrawal"],
    )
    fed_ids = {r["transaction_id"] for r in existing if r["transaction_id"]}
    imported = collections.Counter(_key(r) for r in existing if not r["transaction_id"])
    return new_rows(rows, fed_ids, imported)


def bank_account_name(prefix, currency, bank):
    """The name ERPNext gives a bank's Bank Account of a currency: "<prefix> <currency> - <bank>"."""
    return "{} {} - {}".format(prefix, currency, bank)


def open_bank_account(prefix, bank, currency, parent, company, numbers):
    """The Bank Account of a currency at a bank, with its GL account under parent, made when missing.

    The GL account is "<prefix> Kontokorrent" (with the currency after it unless CHF) and takes the first number in numbers
    that no account of the company has. An existing Bank Account is left as it is. Returns its name.
    """
    name = bank_account_name(prefix, currency, bank)
    if frappe.db.exists("Bank Account", name):
        return name
    if not frappe.db.exists("Bank", bank):
        frappe.get_doc({"doctype": "Bank", "bank_name": bank}).insert()
    gl_name = "{} Kontokorrent".format(prefix) + ("" if currency == "CHF" else " " + currency)
    account = frappe.get_doc(
        {
            "doctype": "Account",
            "account_number": _free_number(company, numbers),
            "account_name": gl_name,
            "parent_account": parent,
            "company": company,
            "account_currency": currency,
            "account_type": "Bank",
            "is_group": 0,
        }
    ).insert()
    frappe.get_doc(
        {
            "doctype": "Bank Account",
            "account_name": "{} {}".format(prefix, currency),
            "bank": bank,
            "account": account.name,
            "company": company,
            "is_company_account": 1,
        }
    ).insert()
    return name


def _free_number(company, numbers):
    used = {str(n) for n in frappe.get_all("Account", filters={"company": company}, pluck="account_number") if n}
    for number in numbers:
        if str(number) not in used:
            return str(number)
    frappe.throw(_("No free account number in {0} to {1} for a new bank account.").format(numbers[0], numbers[-1]))


def write(bank_account, rows):
    """Insert and submit the rows as Bank Transactions, as the bexio loader does (a submitted one posts no GL). Returns the count."""
    company = frappe.db.get_value("Bank Account", bank_account, "company")
    written = 0
    for row in rows:
        # checked again per row: the hourly job and a Sync now can run at once
        if frappe.db.exists("Bank Transaction", {"transaction_id": row["transaction_id"]}):
            continue
        doc = frappe.get_doc(
            {
                "doctype": "Bank Transaction",
                "company": company,
                "bank_account": bank_account,
                "date": row["date"],
                "deposit": row["deposit"],
                "withdrawal": row["withdrawal"],
                "currency": row["currency"],
                "description": row["description"],
                "reference_number": row["reference_number"],
                "transaction_id": row["transaction_id"],
            }
        )
        doc.insert()
        doc.submit()
        written += 1
    return written


def _key(row):
    return (str(row["date"]), round(float(row["deposit"] or 0), 2), round(float(row["withdrawal"] or 0), 2))


def load_rates(currency, start, end, dry_run, opener=urllib.request.urlopen):
    """The CHF rate of a currency for each day from start to end, as {(currency, day): rate} for the run's conversion.

    One request for the range, from the latest stored day or start, whichever is later. A live run stores each day it
    did not have as a Currency Exchange, named as ERPNext names them, so ERPNext's own lookup finds it later. A dry run
    stores nothing and converts with the fetched rates. A weekend or a holiday takes the latest earlier rate.
    """
    _check_source()
    stored = frappe.get_all("Currency Exchange", filters={"from_currency": currency, "to_currency": "CHF"}, pluck="date")
    first = fetch_start(start, max(stored, default=None))
    if first > end:
        return {}
    days = fetch_rates(currency, first, end, opener=opener)
    if not dry_run:
        known = {str(day) for day in stored}
        for day, rate in days.items():
            if day not in known:
                _store_rate(currency, day, rate)
    return {(currency, day): rate for day, rate in carry_forward(days, first, end).items()}


def fetch_start(start, latest):
    """The first day to ask the source for: start, or the latest stored day when that is later (its row seeds the carry)."""
    if latest and latest > start:
        return latest
    return start


def fetch_rates(currency, start, end, opener=urllib.request.urlopen):
    """The CHF rate the source gives for each day from start to end: {day: rate}, days as YYYY-MM-DD. One request."""
    params = {"from": str(start), "to": str(end), "base": currency, "quotes": "CHF"}
    request = urllib.request.Request(
        RATE_URL + "?" + urllib.parse.urlencode(params), headers={"Accept": "application/json"}, method="GET"
    )
    try:
        with opener(request, timeout=RATE_TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as err:
        raise RateError("HTTP {} from the rate source".format(err.code)) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
        raise RateError("no usable answer from the rate source ({})".format(type(err).__name__)) from None
    return rate_days(payload, currency)


def rate_days(payload, currency):
    """The CHF rates in the source's answer, a list of {date, base, quote, rate}: {day: rate}. Other pairs and zero rates are left out."""
    if not isinstance(payload, list):
        raise RateError("unexpected answer from the rate source")
    days = {}
    for item in payload:
        if not isinstance(item, dict):
            raise RateError("unexpected answer from the rate source")
        if item.get("base") == currency and item.get("quote") == "CHF" and item.get("rate"):
            days[item["date"]] = float(item["rate"])
    return days


def carry_forward(days, start, end):
    """A rate for each day from start to end: the day's own, or the latest earlier one. Days before the first rate have none.

    days: {day: rate}, days as YYYY-MM-DD. Returns {day: rate} for the days that have one.
    """
    out = {}
    last = None
    day = start
    while day <= end:
        last = days.get(str(day), last)
        if last is not None:
            out[str(day)] = last
        day += ONE_DAY
    return out


def _check_source():
    settings = frappe.get_single("Currency Exchange Settings")
    if settings.disabled or settings.service_provider != "frankfurter.dev - v2":
        frappe.throw(_("Enable Currency Exchange Settings with frankfurter.dev - v2 first: the Wise rates come from it."))


def _store_rate(currency, day, rate):
    """A Currency Exchange for buying and for selling, as ERPNext's own rates are stored."""
    try:
        frappe.get_doc(
            {
                "doctype": "Currency Exchange",
                "date": day,
                "from_currency": currency,
                "to_currency": "CHF",
                "exchange_rate": rate,
                "for_buying": 1,
                "for_selling": 1,
            }
        ).insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        # the hourly job and a Sync now can store the same day at once
        pass
