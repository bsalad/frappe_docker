"""The 13-week cash forecast without the site: what the lines are, which week each falls in, the totals.

report/cash_flow_forecast reads the books and the open documents and hands the lines here; this module
decides the dates and the sums, so it can be tested with invented data. Pure Python, no frappe.

Weeks run from the as-of date: week 1 is the as-of day and the six after it, week 13 the last seven days of
the horizon. A line due before the as-of date is overdue and falls in week 1: it is still owed.

Kinds of line: receipt (a customer pays an open Sales Invoice), new_sales (the run-rate of receipts from invoices not
issued yet), bill (an open Purchase Invoice), new_purchases (the run-rate of payments of bills not posted yet), recurring
(a cost that repeats per supplier), payroll (the monthly salary run), vat (the VAT the books owe, or a refund).
Only receipts and new sales come in; the others go out. A refund is a vat line with a negative amount, so it comes in.
"""

import calendar
import collections
import datetime
import re
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
# The share of the gaps between a recurring cost's payments that must fall in its period's band: one in five may be
# off (a duplicate, a late run), not more.
REGULAR_SHARE = 0.8
RECEIPT = "receipt"
NEW_SALES = "new_sales"
NEW_PURCHASES = "new_purchases"
INFLOW_KINDS = (RECEIPT, NEW_SALES)
OUTFLOW_KINDS = ("bill", NEW_PURCHASES, "recurring", "payroll", "vat")
# The salary accounts by number, inclusive (5000 Loehne, 5003 and the rest of the 50xx run). The payroll line is
# the salary run only: the 57xx social contributions are paid through the insurers' bills, the 58xx other personnel
# costs come as bills or bank lines, so none of them is in the payroll basis.
SALARY_ACCOUNTS = ("5000", "5099")
# The VAT accounts by number: the liabilities (Umsatzsteuer, the transitory tax) and the input tax.
VAT_LIABILITY_ACCOUNTS = ("2200", "2202")
VAT_INPUT_ACCOUNTS = ("1170", "1171", "1172")
# The insurers' payables, inclusive: their contributions reach the forecast through the insurers' bills.
INSURER_PAYABLE_ACCOUNTS = ("2270", "2279")
# The salary clearing account (the net pay the bank moves) and the suppliers' payable (the bills' own account).
SALARY_CLEARING_ACCOUNT = "1091"
BILL_PAYABLE_ACCOUNT = "2000"
# The owners' current accounts: what they draw is average and discretionary, not a commitment.
OWNER_ACCOUNTS = ("2100", "2121")
BANK_ACCOUNT_TYPES = ("Bank", "Cash")
# A bank line's description is grouped by its first words: the payee's name. Digits, dates, punctuation and
# month names are dropped, so an invoice number or the month in the text does not split the group. Three words
# tell apart two services of one bank or telco, and stop short of the invoice reference that follows a name.
KEY_WORDS = 3
MONTH_WORDS = frozenset((
    "jan", "januar", "january", "feb", "februar", "february", "mär", "märz", "maerz", "mar", "march",
    "apr", "april", "mai", "may", "jun", "juni", "june", "jul", "juli", "july", "aug", "august",
    "sep", "sept", "september", "okt", "oktober", "october", "oct", "nov", "november",
    "dez", "dezember", "dec", "december",
))
# Words that start a salary or VAT payment's description (prefix match, so "lohnzahlung" counts). Such a bank
# line is left out of the recurring costs: the payroll and VAT lines already carry it.
PAYROLL_VAT_STEMS = ("lohn", "salär", "salar", "gehalt", "ahv", "mwst", "vat", "estv", "steuerverwaltung")
# A bank line is a purchase bill's payment when a bill of the same amount is dated this close to it.
BILL_MATCH_DAYS = 5
# The run-rates (new sales, new purchases): the receipts or payments of the last this many 13-week windows, from the
# invoices issued in the same window.
RUN_RATE_WINDOWS = 4


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


def description_words(text):
    """The words of a bank line's description: lower case, without digits, dates, punctuation or month names."""
    words = re.sub(r"[^a-zäöüéèàâêîôûçß]+", " ", (text or "").lower()).split()
    return [word for word in words if word not in MONTH_WORDS]


def description_key(text):
    """The group key of a bank line: its first KEY_WORDS words. "" when the description has no words."""
    return " ".join(description_words(text)[:KEY_WORDS])


def is_payroll_or_vat(text):
    """True when a word of the description starts like a salary or a VAT payment's word."""
    return any(word.startswith(PAYROLL_VAT_STEMS) for word in description_words(text))


def paid_by_a_bill(day, amount, bills):
    """bills: (posting date, amount) of the purchase bills. True when one of the same amount is dated within
    BILL_MATCH_DAYS of the bank line on day: that line is the bill's payment, and the bill source has it."""
    return any(abs((bill_day - day).days) <= BILL_MATCH_DAYS and abs(bill_amount - amount) < 0.005
               for bill_day, bill_amount in bills)


def carried_elsewhere(number):
    """True when a bank outflow to this contra account already reaches the forecast through another line: the payroll
    (5xxx and 1091), the insurers' bills (2270 to 2279), VAT (1170 to 1172, 2200 and 2202), or the bills (2000)."""
    return (number.startswith("5") or number == SALARY_CLEARING_ACCOUNT or number == BILL_PAYABLE_ACCOUNT
            or INSURER_PAYABLE_ACCOUNTS[0] <= number <= INSURER_PAYABLE_ACCOUNTS[1]
            or number in VAT_INPUT_ACCOUNTS or number in VAT_LIABILITY_ACCOUNTS)


def journal_occurrences(rows, billed):
    """rows: (contra account number, account type, posting date, amount, journal entry) of the debit lines of the Journal
    Entries that credit a bank or cash account. billed: the journal entries reconciled to a purchase invoice, which are
    bill payments. One occurrence per (account, posting date, amount), as (account number, date, amount, journal
    entry). Returns the occurrences and a Counter of the rows each rule left out."""
    left_out = collections.Counter()
    seen = set()
    occurrences = []
    for number, account_type, day, amount, entry in rows:
        if account_type in BANK_ACCOUNT_TYPES:
            left_out["transfer between own bank accounts"] += 1
        elif carried_elsewhere(number):
            left_out["carried by another line"] += 1
        elif entry in billed:
            left_out["reconciled to a purchase invoice"] += 1
        elif (number, day, amount) not in seen:
            seen.add((number, day, amount))
            occurrences.append((number, day, amount, entry))
    return occurrences, left_out


def counted_by_contra(bank, journal):
    """bank: (group key, date, amount, bank transaction name) of the bank lines the bank source groups. journal: the names a
    Journal Entry reconciles to. The bank groups every line of which a Journal Entry reconciles to: the contra account
    source counts those payments, so the bank source leaves the groups out. A group is a key, so the groups left in keep
    their medians: nothing is regrouped."""
    flags = collections.defaultdict(list)
    for key, _day, _amount, name in bank:
        flags[key].append(name in journal)
    return {key for key, reconciled in flags.items() if all(reconciled)}


def mostly_regular(dates, period):
    """True when at least REGULAR_SHARE of the gaps between the sorted dates fall in the period's band. The median
    alone labels a run of unrelated payments (a group of card settlements, say) as monthly when only half of its
    gaps are; a recurring cost repeats on its period nearly every time."""
    gaps = [(later - earlier).days for earlier, later in zip(dates, dates[1:])]
    low, high = PERIODS[period][1]
    return sum(low <= gap <= high for gap in gaps) >= REGULAR_SHARE * len(gaps)


def merge_same_day(rows):
    """rows: (day, amount, name) sorted by day. One row per day: the amounts summed, the last name kept."""
    merged = []
    for day, amount, name in rows:
        if merged and merged[-1][0] == day:
            merged[-1] = (day, merged[-1][1] + amount, name)
        else:
            merged.append((day, amount, name))
    return merged


def recurring_costs(bills, as_of, strict=False):
    """bills: (supplier, posting date, amount, bill name) of the look-back window, in the company currency.
    One dict per supplier with a fixed period: supplier, period, amount, count (the bills behind it), last_date,
    last_bill (the name of its last bill, as its source). A supplier whose last bill is older than two periods
    has stopped and is left out. strict: the gaps must also mostly fall in the period (see mostly_regular); the
    bank lines are passed strict, since a group of them, keyed by description, can mix several payees; the lines
    of one group on one day are then one occurrence (their sum), as a month-end run of fees is one cost, and
    count stays the number of lines."""
    by_supplier = {}
    for supplier, day, amount, name in bills:
        by_supplier.setdefault(supplier, []).append((day, amount, name))
    found = []
    for supplier, rows in sorted(by_supplier.items()):
        rows.sort()
        lines = len(rows)
        if strict:
            rows = merge_same_day(rows)
        dates = [day for day, _, _ in rows]
        detected = detect_period(dates, [amount for _, amount, _ in rows])
        if detected is None or (strict and not mostly_regular(dates, detected[0])):
            continue
        period, amount = detected
        months = PERIODS[period][0]
        last_date, _, last_bill = rows[-1]
        if add_months(last_date, 2 * months) < as_of:
            continue
        found.append({
            "supplier": supplier, "period": period, "amount": round(amount, 2), "count": lines,
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


def payroll_postings(rows):
    """rows: (posting date, account number, debit minus credit) of the personnel accounts (group 5). The salary
    accounts' postings only, as (posting date, amount): a contribution posted on the same day is not a salary."""
    return [(day, amount) for day, number, amount in rows if SALARY_ACCOUNTS[0] <= number <= SALARY_ACCOUNTS[1]]


def payroll_from_postings(postings):
    """postings: (posting date, amount) of the salary accounts over the look-back window (payroll_postings).
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


def vat_split(owed_at_closed_end, paid_since, owed_now):
    """Splits the VAT on the books into (closed quarter, current quarter so far). owed_at_closed_end: the VAT owed
    when the last quarter closed; paid_since: what was paid to the tax office since (debits on the liability
    accounts); owed_now: the VAT owed on the as-of date. A closed quarter's VAT already paid is not owed again,
    and is not taken off the current quarter. A refund due is left as the books show it."""
    closed = max(owed_at_closed_end - paid_since, 0.0) if owed_at_closed_end > 0 else owed_at_closed_end
    return round(closed, 2), round(owed_now - closed, 2)


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


def run_rate(payments, as_of):
    """payments: (invoice date, payment date, amount) of the Sales Invoices paid in the last RUN_RATE_WINDOWS windows.
    A window is WEEKS weeks, and the windows run back from as_of with no gap. Its receipts are the amounts collected
    inside it from the invoices issued inside it: the invoices a forecast cannot see yet, since they are not open.
    The run-rate is the mean over the windows, spread evenly over the weeks: (weekly amount, first day of the oldest
    window). A window with no receipts counts as 0 in the mean."""
    length = datetime.timedelta(days=WEEKS * DAYS_PER_WEEK)
    totals = []
    for back in range(1, RUN_RATE_WINDOWS + 1):
        start = as_of - length * back
        stop = as_of - length * (back - 1)
        totals.append(sum(amount for issued, paid, amount in payments
                          if start <= issued < stop and start <= paid < stop))
    return round(statistics.mean(totals) / WEEKS, 2), as_of - length * RUN_RATE_WINDOWS


def purchase_run_rate(payments, as_of, excluded):
    """payments: (supplier, bill date, payment date, amount) of the Purchase Invoices paid in the last RUN_RATE_WINDOWS
    windows. The run-rate of run_rate over the suppliers not in excluded: those the recurring costs already forecast,
    and the insurers' bills, which are recurring bills of their own. Returns (weekly amount, first day of the oldest
    window, how many excluded suppliers were paid in the windows: the count the basis note tells)."""
    weekly, since = run_rate([(issued, paid, amount) for supplier, issued, paid, amount in payments if supplier not in excluded], as_of)
    return weekly, since, len({supplier for supplier, _issued, _paid, _amount in payments if supplier in excluded})


def forecast(as_of, opening, lines):
    """lines: dicts with kind, day, amount (positive; negative for a vat refund), party, doctype, name, note.
    Places each line in its week and runs the balance from the opening cash.
    Returns (weeks, lowest, beyond): weeks is 13 dicts (week, start, end, the amount per kind, net, closing);
    lowest is the week with the lowest closing balance, the earliest on a tie; beyond counts the lines after the horizon."""
    weeks = []
    for week in range(1, WEEKS + 1):
        start, end = week_bounds(week, as_of)
        weeks.append({"week": week, "start": start, "end": end, "receipt": 0.0, "new_sales": 0.0, "bill": 0.0,
                      "new_purchases": 0.0, "recurring": 0.0, "payroll": 0.0, "vat": 0.0})
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
        for kind in INFLOW_KINDS:
            row[kind] = round(row[kind], 2)
        row["net"] = round(sum(row[kind] for kind in INFLOW_KINDS) - sum(row[kind] for kind in OUTFLOW_KINDS), 2)
        balance = round(balance + row["net"], 2)
        row["closing"] = balance
    lowest = min(weeks, key=lambda row: row["closing"])["week"]
    return weeks, lowest, beyond
