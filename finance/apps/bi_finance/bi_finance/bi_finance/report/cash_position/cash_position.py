"""Cash Position: what every Bank and Cash account holds, in its own currency and in CHF.

The balances come from the GL, not from Bank Transactions: the books are the source, so a
feed that is behind or not yet reconciled does not change the figure. A balance in another
currency is turned into CHF at the Currency Exchange rate of the as-of date, or the latest one
before it. ERPNext fills that table from its rate source (Currency Exchange Settings,
frankfurter.dev); this report only reads it. A missing rate leaves that account's CHF value
out and says so in the message.

The chart shows the same balances at each month end from January 2019, at the rate of that day.
"""

import calendar
import datetime
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import escape_html, flt, getdate, nowdate

BASE = "CHF"
CASH_ACCOUNT_TYPES = ["Bank", "Cash"]
HISTORY_FROM = datetime.date(2019, 1, 1)

def columns():
    return [
        {"label": _("Account"), "fieldname": "account", "fieldtype": "Link", "options": "Account", "width": 300},
        {"label": _("Currency"), "fieldname": "currency", "fieldtype": "Link", "options": "Currency", "width": 90},
        {"label": _("Balance"), "fieldname": "balance", "fieldtype": "Float", "precision": 2, "width": 140},
        {"label": _("Rate to CHF"), "fieldname": "rate", "fieldtype": "Float", "precision": 6, "width": 120},
        {"label": _("Rate date"), "fieldname": "rate_date", "fieldtype": "Date", "width": 110},
        {"label": _("Balance CHF"), "fieldname": "balance_chf", "fieldtype": "Float", "precision": 2, "width": 140},
    ]


def month_ends(first, last):
    """The month ends from the month of `first` to `last`, each one on or before `last`."""
    ends = []
    year, month = first.year, first.month
    while True:
        end = datetime.date(year, month, calendar.monthrange(year, month)[1])
        if end > last:
            return ends
        ends.append(end)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def rate_on(rates, day):
    """rates: (date, rate) pairs sorted by date. The latest one on or before `day`, as (date, rate);
    (None, None) when there is none."""
    found = (None, None)
    for rate_date, rate in rates:
        if rate_date > day:
            break
        found = (rate_date, rate)
    return found


def balance_on(movements, day):
    """movements: (posting date, amount) pairs of one account. Its balance at the close of `day`."""
    return sum(amount for posted, amount in movements if posted <= day)


def chf(amount, rate):
    return None if rate is None else round(amount * rate, 2)


def build(accounts, movements, rates, as_of, months, total_label="Total CHF"):
    """The report from data already loaded, so it can be tested without a site (no translation here).

    accounts: (name, currency) of each account, in the order they show.
    movements: {name: [(posting date, amount in the account's currency)]}, dated on or before as_of.
    rates: {currency: [(date, CHF per unit)]}, sorted by date. CHF itself is 1.
    months: the month ends of the chart, on or before as_of.
    Returns (rows, chart, total, missing): missing lists (name, currency) of the accounts without a rate.
    """
    rows, missing = [], []
    for name, currency in accounts:
        balance = round(balance_on(movements.get(name, []), as_of), 2)
        rate_date, rate = rate_on(rates.get(currency, []), as_of)
        value = chf(balance, rate)
        if value is None:
            missing.append((name, currency))
        rows.append({
            "account": name,
            "currency": currency,
            "balance": balance,
            "rate": rate,
            # CHF is its own base: no rate date to show
            "rate_date": rate_date if currency != BASE else None,
            "balance_chf": value,
        })
    total = round(sum(row["balance_chf"] for row in rows if row["balance_chf"] is not None), 2)

    datasets = []
    totals = [0.0] * len(months)
    for name, currency in accounts:
        values = []
        for i, day in enumerate(months):
            value = chf(round(balance_on(movements.get(name, []), day), 2), rate_on(rates.get(currency, []), day)[1])
            values.append(value)
            if value is not None:
                totals[i] += value
        datasets.append({"name": name, "values": values})
    datasets.append({"name": total_label, "values": [round(t, 2) for t in totals]})
    chart = {"data": {"labels": [day.isoformat() for day in months], "datasets": datasets}, "type": "line"}
    return rows, chart, total, missing


def execute(filters=None):
    filters = frappe._dict(filters or {})
    company = filters.company or frappe.defaults.get_user_default("company")
    as_of = getdate(filters.as_of_date or nowdate())

    # The Treasury cards select one account by its chart number (the card files carry no names).
    account_filter = {"company": company, "account_type": ["in", CASH_ACCOUNT_TYPES], "is_group": 0}
    if filters.account:
        account_filter["name"] = filters.account
    if filters.account_number:
        account_filter["account_number"] = filters.account_number
    accounts = frappe.get_all(
        "Account", filters=account_filter, fields=["name", "account_currency"], order_by="account_number, name"
    )
    accounts = [(a.name, a.account_currency) for a in accounts]

    movements = defaultdict(list)
    if accounts:
        entries = frappe.db.sql(
            """select account, posting_date, sum(debit_in_account_currency - credit_in_account_currency)
            from `tabGL Entry`
            where company = %s and is_cancelled = 0 and posting_date <= %s and account in %s
            group by account, posting_date""",
            (company, as_of, tuple(a[0] for a in accounts)),
        )
        for account, posted, amount in entries:
            movements[account].append((posted, flt(amount)))

    rates = {BASE: [(datetime.date.min, 1.0)]}
    for currency in {a[1] for a in accounts} - {BASE}:
        found = frappe.get_all(
            "Currency Exchange",
            filters={"from_currency": currency, "to_currency": BASE, "date": ["<=", as_of]},
            fields=["date", "exchange_rate"],
            order_by="date asc",
        )
        rates[currency] = [(r.date, flt(r.exchange_rate)) for r in found]

    total_label = _("Total CHF")
    rows, chart, total, missing = build(
        accounts, movements, rates, as_of, month_ends(HISTORY_FROM, as_of), total_label
    )
    summary = [{"value": total, "label": total_label, "datatype": "Currency", "currency": BASE, "indicator": "Blue"}]
    messages = [
        _("No {0} rate on or before {1} for {2}; its CHF value is left out.").format(currency, as_of, name)
        for name, currency in missing
    ]
    message = "<br>".join(escape_html(m) for m in messages) or None
    return columns(), rows, message, chart, summary
