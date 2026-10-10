"""Cash Conversion Cycle: DSO, DPO and DIO per month, and CCC = DSO + DIO - DPO.

The arithmetic only, with no site: report/cash_conversion_cycle reads the GL and the Payment
Entry references and hands the months to build() here. For a month:

- DSO = receivables at month end / revenue of the month x days in the month
- DPO = payables at month end / purchases of the month x days in the month
- DIO = stock at month end / cost of goods sold of the month x days in the month; 0 when there is
  no stock at month end (the report says so), None when there is stock but no cost of goods sold
- CCC = DSO + DIO - DPO; None when one of the three is None

The rolling values use the 12 months ending with the month: the flows summed over them, the
balance at its end, and the days of those 12 months. They are None until 12 months of history
exist, since the GL starts in 2019 and a window before it would be short.

The actual days to pay are the payment date less the invoice date, weighted by the amount
allocated, over the payments dated in the month (or in the 12 months). Customers come from
Payment Entry references to Sales Invoices, suppliers from those to Purchase Invoices.

Invented numbers only in the tests (test_cash_conversion); no network, no frappe.
"""

import calendar
import datetime

ROLLING = 12


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


def month_index(day, first):
    """Which month, counting from 0 at the month of `first`, the day falls in."""
    return (day.year - first.year) * 12 + day.month - first.month


def month_totals(postings, first, count):
    """postings: (posting date, amount) pairs. Returns (before, per_month): the total of the postings
    dated before the first month, and the total of each of the `count` months from it. Postings
    after the last month are left out."""
    before = 0.0
    per_month = [0.0] * count
    for posted, amount in postings:
        index = month_index(posted, first)
        if index < 0:
            before += amount
        elif index < count:
            per_month[index] += amount
    return before, per_month


def running(before, per_month):
    """The balance at the end of each month: the total before the first month, plus each month's movement."""
    balances, balance = [], before
    for movement in per_month:
        balance += movement
        balances.append(balance)
    return balances


def ratio(balance, flow, days):
    """A balance as days of a flow. None when the flow is not positive."""
    if balance is None or flow is None or flow <= 0:
        return None
    return round(balance / flow * days, 1)


def dio_days(stock, cogs, days):
    """Stock as days of cost of goods sold. No stock at all is 0 days, whatever the cost of goods sold."""
    if stock == 0:
        return 0.0
    return ratio(stock, cogs, days)


def cash_conversion(dso, dio, dpo):
    if None in (dso, dio, dpo):
        return None
    return round(dso + dio - dpo, 1)


def weighted_days(payments):
    """payments: (paid on, invoiced on, amount) triples. The days from invoice to payment, weighted
    by amount; None when nothing was paid."""
    total = sum(amount for _, _, amount in payments)
    if total <= 0:
        return None
    return round(sum(amount * (paid - invoiced).days for paid, invoiced, amount in payments) / total, 1)


def paid_by_month(payments, first, count):
    """Payments grouped by the month they were paid in, as a list of `count` lists."""
    by_month = [[] for _ in range(count)]
    for payment in payments:
        index = month_index(payment[0], first)
        if 0 <= index < count:
            by_month[index].append(payment)
    return by_month


def membership(accounts, payables_excluded):
    """{account name: [group, ...]} for the accounts the report sums."""
    groups = {}
    for account in accounts:
        found = []
        if account.root_type == "Income":
            found.append("revenue")
        if account.account_type == "Receivable":
            found.append("receivables")
        if account.account_type == "Payable" and account.name not in payables_excluded:
            found.append("payables")
        if account.account_type == "Stock":
            found.append("inventory")
        if account.account_type == "Cost of Goods Sold":
            found.append("cogs")
        number = account.account_number or ""
        if (
            account.root_type == "Expense"
            and number[:1] in ("4", "6")
            and number[:2] != "69"
            and account.account_type not in ("Depreciation", "Round Off")
        ):
            found.append("purchases")
        if found:
            groups[account.name] = found
    return groups


def build(months, flows, balances, customers, suppliers):
    """The rows of the report, one per month, from data already loaded.

    months: the month ends, consecutive, from the first month of the history to the last one.
    flows: {"revenue", "purchases", "cogs"}: per month, the amounts of the month (revenue as a positive).
    balances: {"receivables", "payables", "inventory"}: per month, the balance at its end (payables positive).
    customers, suppliers: (paid on, invoiced on, amount) triples of the payments, any date.
    """
    first = months[0]
    days = [end.day for end in months]
    paid_customers = paid_by_month(customers, first, len(months))
    paid_suppliers = paid_by_month(suppliers, first, len(months))
    rows = []
    for i, end in enumerate(months):
        row = {
            "month": end,
            "revenue": flows["revenue"][i],
            "purchases": flows["purchases"][i],
            "cogs": flows["cogs"][i],
            "receivables": balances["receivables"][i],
            "payables": balances["payables"][i],
            "inventory": balances["inventory"][i],
            "dso": ratio(balances["receivables"][i], flows["revenue"][i], days[i]),
            "dpo": ratio(balances["payables"][i], flows["purchases"][i], days[i]),
            "dio": dio_days(balances["inventory"][i], flows["cogs"][i], days[i]),
            "paid_days_customers": weighted_days(paid_customers[i]),
            "paid_days_suppliers": weighted_days(paid_suppliers[i]),
        }
        row["ccc"] = cash_conversion(row["dso"], row["dio"], row["dpo"])
        rows.append(row)

    for i, row in enumerate(rows):
        row.update(dso_12=None, dpo_12=None, dio_12=None, ccc_12=None, ccc_12_change=None,
                   paid_days_customers_12=None, paid_days_suppliers_12=None)
        if i < ROLLING - 1:
            continue
        window = range(i - ROLLING + 1, i + 1)
        window_days = sum(days[j] for j in window)
        revenue = sum(flows["revenue"][j] for j in window)
        purchases = sum(flows["purchases"][j] for j in window)
        cogs = sum(flows["cogs"][j] for j in window)
        row["dso_12"] = ratio(row["receivables"], revenue, window_days)
        row["dpo_12"] = ratio(row["payables"], purchases, window_days)
        row["dio_12"] = dio_days(row["inventory"], cogs, window_days)
        row["ccc_12"] = cash_conversion(row["dso_12"], row["dio_12"], row["dpo_12"])
        row["paid_days_customers_12"] = weighted_days([p for j in window for p in paid_customers[j]])
        row["paid_days_suppliers_12"] = weighted_days([p for j in window for p in paid_suppliers[j]])
    for i, row in enumerate(rows):
        # The change is against the rolling value twelve months back, so it starts 24 months in.
        if i >= 2 * ROLLING - 1 and row["ccc_12"] is not None and rows[i - ROLLING]["ccc_12"] is not None:
            row["ccc_12_change"] = round(row["ccc_12"] - rows[i - ROLLING]["ccc_12"], 1)
    return rows
