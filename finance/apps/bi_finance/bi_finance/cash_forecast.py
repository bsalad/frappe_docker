"""The 13-week cash forecast without the site: what the lines are, which week each falls in, the totals.

report/cash_flow_forecast reads the books and the open documents and hands the lines here; this module
decides the dates and the sums, so it can be tested with invented data. Pure Python, no frappe.

Weeks run from the as-of date: week 1 is the as-of day and the six after it, week 13 the last seven days of
the horizon. A line due before the as-of date is overdue and falls in week 1: it is still owed.

Kinds of line: receipt (a customer pays an open Sales Invoice), bill (an open Purchase Invoice), recurring
(a cost that repeats per supplier), payroll (the monthly salary run), vat (the VAT the books owe, or a refund).
Only receipts come in; the others go out. A refund is a vat line with a negative amount, so it comes in.
"""

import calendar
import datetime
import statistics

WEEKS = 13
DAYS_PER_WEEK = 7
# The salary run is on this day of the month, or the Friday before when it falls on a weekend.
PAYROLL_DAY = 25
# The VAT return and payment for a quarter are due at the end of the quarter's second month after it ends.
# A refund is paid this many days after that due date.
VAT_REFUND_DAYS = 30
# A recurring cost whose occurrence falls this close to an open bill of the same supplier is that bill, not a second one.
OPEN_BILL_MATCH_DAYS = 15
# Days between one bill and the next, as the median, for each period; the periods in months, below.
PERIODS = {
    "monthly": (1, (25, 35)),
    "quarterly": (3, (80, 100)),
    "yearly": (12, (340, 390)),
}
RECEIPT = "receipt"
OUTFLOW_KINDS = ("bill", "recurring", "payroll", "vat")
# The VAT accounts by number: the liabilities (Umsatzsteuer, the transitory tax) and the input tax.
VAT_LIABILITY_ACCOUNTS = ("2200", "2202")
VAT_INPUT_ACCOUNTS = ("1170", "1171", "1172")


def add_months(day, months):
    """day moved by whole months, the day of month kept or clamped to the month's end."""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return datetime.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def horizon_end(as_of):
    return as_of + datetime.timedelta(days=WEEKS * DAYS_PER_WEEK - 1)


def week_of(day, as_of):
    """The week (1 to 13) a day falls in, 1 for an overdue day, None after the horizon."""
    days = (day - as_of).days
    if days < 0:
        return 1
    week = days // DAYS_PER_WEEK + 1
    return week if week <= WEEKS else None


def week_bounds(week, as_of):
    start = as_of + datetime.timedelta(days=(week - 1) * DAYS_PER_WEEK)
    return start, start + datetime.timedelta(days=DAYS_PER_WEEK - 1)


def average_days_late(history):
    """history: (due, paid) of one customer's paid invoices. The mean days paid after the due date, rounded;
    a customer who pays early has negative days. No history: 0, the invoice is due when it is due."""
    if not history:
        return 0
    return round(statistics.mean((paid - due).days for due, paid in history))


def detect_period(dates, amounts):
    """The period a supplier's bills repeat on, from the median gap between them, as (period, median amount),
    or None when they have no fixed period. Needs two bills (one gap): a yearly cost has two in a look-back of a year."""
    if len(dates) < 2:
        return None
    gaps = [later - earlier for earlier, later in zip(dates, dates[1:])]
    median_gap = statistics.median(day.days for day in gaps)
    for period, (_, (low, high)) in PERIODS.items():
        if low <= median_gap <= high:
            return period, statistics.median(amounts)
    return None


def recurring_costs(bills, as_of):
    """bills: (supplier, posting date, amount, bill name) of the look-back window, in the company currency.
    One dict per supplier with a fixed period: supplier, period, amount, last_date, last_bill (the name of its
    last bill, as its source). A supplier whose last bill is older than two periods has stopped and is left out."""
    by_supplier = {}
    for supplier, day, amount, name in bills:
        by_supplier.setdefault(supplier, []).append((day, amount, name))
    found = []
    for supplier, rows in sorted(by_supplier.items()):
        rows.sort()
        detected = detect_period([day for day, _, _ in rows], [amount for _, amount, _ in rows])
        if detected is None:
            continue
        period, amount = detected
        months = PERIODS[period][0]
        last_date, _, last_bill = rows[-1]
        if add_months(last_date, 2 * months) < as_of:
            continue
        found.append({
            "supplier": supplier, "period": period, "amount": round(amount, 2),
            "last_date": last_date, "last_bill": last_bill,
        })
    return found


def recurring_dates(item, as_of, end):
    """The days from as_of to end on which the recurring cost falls, counted on from its last bill."""
    months = PERIODS[item["period"]][0]
    n = 1
    while True:
        day = add_months(item["last_date"], months * n)
        n += 1
        if day > end:
            return
        if day >= as_of:
            yield day


def payroll_from_postings(postings):
    """postings: (posting date, amount) of the salary accounts (group 5) over the look-back window.
    The payroll is the average of the last three months with postings. None when there are no postings."""
    monthly = {}
    for day, amount in postings:
        monthly.setdefault((day.year, day.month), 0.0)
        monthly[(day.year, day.month)] += amount
    if not monthly:
        return None
    latest = sorted(monthly)[-3:]
    return round(statistics.mean(monthly[key] for key in latest), 2)


def payroll_run(year, month):
    """The salary run of a month: the 25th, or the Friday before when the 25th is a Saturday or a Sunday."""
    day = datetime.date(year, month, PAYROLL_DAY)
    return day - datetime.timedelta(days=max(day.weekday() - 4, 0))


def payroll_dates(as_of, end):
    """Each monthly salary run from as_of to end."""
    year, month = as_of.year, as_of.month
    while True:
        run = payroll_run(year, month)
        if run > end:
            return
        if run >= as_of:
            yield run
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def vat_owed(balances):
    """balances: the debit minus credit of each VAT account by number, up to the as-of date. The VAT the company
    owes the tax office: the liabilities less the input tax. Negative when the input tax is the larger."""
    liabilities = -sum(balances.get(number, 0.0) for number in VAT_LIABILITY_ACCOUNTS)
    input_tax = sum(balances.get(number, 0.0) for number in VAT_INPUT_ACCOUNTS)
    return round(liabilities - input_tax, 2)


def quarter_start(day):
    """The first day of the calendar quarter day falls in."""
    return datetime.date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


def quarter_end(day):
    """The last day of the calendar quarter day falls in."""
    start = quarter_start(day)
    last_month = start.month + 2
    return datetime.date(start.year, last_month, calendar.monthrange(start.year, last_month)[1])


def vat_due_date(end):
    """The VAT return and payment for the quarter ending on end are due at the end of its second month after it:
    Q1 by 31 May, Q2 by 31 August, Q3 by 30 November, Q4 by the end of February."""
    later = add_months(end.replace(day=1), 2)
    return datetime.date(later.year, later.month, calendar.monthrange(later.year, later.month)[1])


def vat_lines(as_of, owed_closed, owed_this_quarter):
    """The VAT cash from the books, as (day, amount, quarter end): a payment is positive, a refund negative.
    owed_closed: the VAT the books owed at the start of the current quarter, which is the VAT of the quarter closed
    last; it is paid on that quarter's due date. owed_this_quarter: the VAT booked since the current quarter
    started, paid on the current quarter's due date (it may fall beyond the horizon). The open invoices' VAT is
    already in the books, since a submitted invoice is booked to VAT, so it is not added again.
    A refund (input tax above output tax) comes in VAT_REFUND_DAYS after the due date."""
    closed_end = quarter_start(as_of) - datetime.timedelta(days=1)
    lines = []
    for end, owed in ((closed_end, owed_closed), (quarter_end(as_of), owed_this_quarter)):
        amount = round(owed, 2)
        if amount == 0:
            continue
        due = vat_due_date(end)
        if amount < 0:
            due += datetime.timedelta(days=VAT_REFUND_DAYS)
        lines.append((due, amount, end))
    return lines


def expected_receipt(due, late_days):
    """The day a customer's open invoice is expected to be paid: its due date moved by the customer's days late."""
    return due + datetime.timedelta(days=late_days)


def forecast(as_of, opening, lines):
    """lines: dicts with kind, day, amount (positive; negative for a vat refund), party, doctype, name, note.
    Places each line in its week and runs the balance from the opening cash.
    Returns (weeks, lowest, beyond): weeks is 13 dicts (week, start, end, the amount per kind, net, closing);
    lowest is the week with the lowest closing balance, the earliest on a tie; beyond counts the lines after the horizon."""
    weeks = []
    for week in range(1, WEEKS + 1):
        start, end = week_bounds(week, as_of)
        weeks.append({"week": week, "start": start, "end": end, "receipt": 0.0, "bill": 0.0,
                      "recurring": 0.0, "payroll": 0.0, "vat": 0.0})
    beyond = 0
    for line in lines:
        week = week_of(line["day"], as_of)
        if week is None:
            beyond += 1
            continue
        line["week"] = week
        weeks[week - 1][line["kind"]] += line["amount"]
    balance = opening
    for row in weeks:
        for kind in OUTFLOW_KINDS:
            row[kind] = round(row[kind], 2)
        row["receipt"] = round(row["receipt"], 2)
        row["net"] = round(row["receipt"] - sum(row[kind] for kind in OUTFLOW_KINDS), 2)
        balance = round(balance + row["net"], 2)
        row["closing"] = balance
    lowest = min(weeks, key=lambda row: row["closing"])["week"]
    return weeks, lowest, beyond
