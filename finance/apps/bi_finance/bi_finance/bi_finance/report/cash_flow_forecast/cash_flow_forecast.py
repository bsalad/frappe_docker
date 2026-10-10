"""Cash Flow Forecast: 13 weeks of cash from the as-of date, each line traceable to its document.

The dates and sums are in bi_finance/cash_forecast.py. This module reads the books for them:
- opening: the Bank and Cash accounts' balance in the company currency, as Cash Position reads it;
- receipts: the open Sales Invoices (Payment Ledger), on the due date moved by the customer's average days late
  over the last year, from the invoices paid in that year;
- new sales: receipts from invoices not yet issued on the as-of date, a run-rate: the receipts collected in each of
  the last four 13-week windows from the invoices issued in the same window, averaged and spread over the weeks.
  The report's "Include new sales run-rate" filter leaves it out, to see the forecast of the documents alone;
- bills: the open Purchase Invoices, on the due date;
- new purchases: payments of bills not yet posted on the as-of date, the mirror of new sales: the payments made in each
  of the last four 13-week windows against the bills posted in the same window, averaged and spread over the weeks.
  The suppliers the recurring costs forecast, and the insurers' bills (items on 2270-2279, their own recurring bills),
  are left out so nothing is counted twice. The report's "Include new purchases run-rate" filter leaves it out;
- recurring costs, from two sources, each over the last year and repeating monthly, quarterly or yearly:
  - the suppliers whose purchase bills repeat, unless an open bill of theirs falls due within a couple of weeks of
    the occurrence;
  - the outgoing bank lines not covered by a bill, grouped by the first words of their description (the payee).
    A line is left out when a payroll or VAT word is in its description, when it is reconciled to a payroll or VAT
    Journal Entry (accounts 5xxx, 2200, 2202), when it is reconciled to a Payment Entry of a Purchase Invoice, when
    it is a transfer between the company's own bank accounts (its other leg is in the opening cash already), or
    when a purchase bill of the same amount is dated within five days of it. A group needs 80% of its gaps in its
    period, since a key can mix payees; lines of a group on one day are one occurrence, their sum; its amount
    is the median of its occurrences;
- payroll: the salary accounts 5000 to 5099 of the last year (not the 57xx social contributions nor the 58xx other
  personnel costs: those are bills or bank lines), the average of the last three months, paid on the 25th
  (the Friday before when the 25th is a weekend);
- VAT: the balance of 2200 and 2202 less 1170 to 1172, split at the start of the current quarter: the quarter closed
  last is paid on its due date, the current quarter's VAT so far is projected on its own due date; a refund comes
  in 30 days after the due date.

Everything is as of the as-of date, so the report can be run for a past date (the back-test).
"""

import collections
import datetime

import frappe
from frappe import _
from frappe.utils import cint, escape_html, flt, fmt_money, getdate, nowdate

from bi_finance import cash_forecast as cf

LOOKBACK_DAYS = 365
# The payables of the insurers' bills (social security, pension, accident and sickness, withholding tax): their
# contributions reach the forecast through the bills, not the payroll line.
INSURER_PAYABLE_ACCOUNTS = ("2270", "2279")


def kind_label(kind):
    """The line kind's label, translated when asked for (a module-level _() would run before any language is set)."""
    return {
        cf.RECEIPT: _("Customer receipt"),
        cf.NEW_SALES: _("Expected receipts from new sales"),
        "bill": _("Supplier bill"),
        cf.NEW_PURCHASES: _("Expected payments for new purchase bills"),
        "recurring": _("Recurring cost"),
        "payroll": _("Payroll"),
        "vat": _("VAT"),
    }[kind]


def open_documents(company, as_of, doctype):
    """{name: (party, amount owed in the company currency, due date)} of the doctype's invoices unpaid on as_of.
    Read from the Payment Ledger, so a past date gives the invoices open then. An invoice's own row is positive
    and its payments and returns negative, for both doctypes, so the sum is what it still owes."""
    rows = frappe.db.sql(
        """select ple.against_voucher_no, max(ple.party), sum(ple.amount)
        from `tabPayment Ledger Entry` ple
        where ple.company = %s and ple.against_voucher_type = %s and ple.delinked = 0 and ple.posting_date <= %s
        group by ple.against_voucher_no
        having sum(ple.amount) > 0.005""",
        (company, doctype, as_of),
    )
    invoices = _invoice_dates(doctype, [name for name, _party, _amount in rows])
    return {
        name: (party, flt(amount), invoices[name])
        for name, party, amount in rows
    }


def _invoice_dates(doctype, names):
    """{name: due date, or the posting date when the invoice has none}."""
    if not names:
        return {}
    found = frappe.get_all(doctype, filters={"name": ["in", names]}, fields=["name", "due_date", "posting_date"])
    return {invoice.name: invoice.due_date or invoice.posting_date for invoice in found}


def sales_paid_in_windows(company, as_of):
    """(invoice date, payment date, amount) of the Sales Invoices paid by a Payment Entry in the last run-rate windows,
    in the company currency. The receipts the Payment Ledger shows against the invoices, as paid_history reads them."""
    since = as_of - datetime.timedelta(days=cf.WEEKS * cf.DAYS_PER_WEEK * cf.RUN_RATE_WINDOWS)
    rows = frappe.db.sql(
        """select inv.posting_date, ple.posting_date, -sum(ple.amount)
        from `tabPayment Ledger Entry` ple join `tabSales Invoice` inv on inv.name = ple.against_voucher_no
        where ple.company = %s and ple.against_voucher_type = 'Sales Invoice' and ple.voucher_type = 'Payment Entry'
          and ple.delinked = 0 and ple.posting_date between %s and %s
        group by ple.against_voucher_no, inv.posting_date, ple.posting_date""",
        (company, since, as_of),
    )
    return [(issued, paid, flt(amount)) for issued, paid, amount in rows]


def purchase_paid_in_windows(company, as_of):
    """(supplier, bill date, payment date, amount) of the Purchase Invoices paid by a Payment Entry in the last run-rate
    windows, in the company currency. The payments the Payment Ledger shows against the bills, as sales_paid_in_windows
    reads the receipts: a payment is negative there, so its sum is negated."""
    since = as_of - datetime.timedelta(days=cf.WEEKS * cf.DAYS_PER_WEEK * cf.RUN_RATE_WINDOWS)
    rows = frappe.db.sql(
        """select pi.supplier, pi.posting_date, ple.posting_date, -sum(ple.amount)
        from `tabPayment Ledger Entry` ple join `tabPurchase Invoice` pi on pi.name = ple.against_voucher_no
        where ple.company = %s and ple.against_voucher_type = 'Purchase Invoice' and ple.voucher_type = 'Payment Entry'
          and ple.delinked = 0 and ple.posting_date between %s and %s
        group by ple.against_voucher_no, pi.supplier, pi.posting_date, ple.posting_date""",
        (company, since, as_of),
    )
    return [(supplier, issued, paid, flt(amount)) for supplier, issued, paid, amount in rows]


def paid_history(company, as_of, doctype, open_names):
    """{party: [(due date, last payment date)]} of the doctype's invoices paid in the last year, not the open ones."""
    rows = frappe.db.sql(
        """select ple.against_voucher_no, max(ple.party), max(ple.posting_date)
        from `tabPayment Ledger Entry` ple
        where ple.company = %s and ple.against_voucher_type = %s and ple.voucher_type != %s and ple.delinked = 0
          and ple.posting_date between %s and %s
        group by ple.against_voucher_no""",
        (company, doctype, doctype, as_of - datetime.timedelta(days=LOOKBACK_DAYS), as_of),
    )
    rows = [(name, party, paid) for name, party, paid in rows if name not in open_names]
    dates = _invoice_dates(doctype, [name for name, _party, _paid in rows])
    history = collections.defaultdict(list)
    for name, party, paid in rows:
        if name in dates:
            history[party].append((dates[name], paid))
    return history


def bills_in_window(company, as_of):
    """(supplier, posting date, amount, bill name) of the purchase bills of the last year, the amounts in the company currency."""
    bills = frappe.get_all(
        "Purchase Invoice",
        filters={
            "company": company, "docstatus": 1, "is_return": 0,
            "posting_date": ["between", [as_of - datetime.timedelta(days=LOOKBACK_DAYS), as_of]],
        },
        fields=["name", "supplier", "posting_date", "base_grand_total"],
    )
    return [(bill.supplier, bill.posting_date, flt(bill.base_grand_total), bill.name) for bill in bills]


def bank_outflows(company, as_of, bills):
    """(group key, date, amount, bank transaction name) of the outgoing bank lines of the last year that no bill,
    payroll or VAT payment covers, and a Counter of the lines each rule left out. bills: bills_in_window."""
    left_out = collections.Counter()
    accounts = frappe.get_all("Bank Account", filters={"company": company}, pluck="name")
    lines = frappe.get_all(
        "Bank Transaction",
        filters={
            "docstatus": 1, "bank_account": ["in", accounts], "withdrawal": [">", 0],
            "date": ["between", [as_of - datetime.timedelta(days=LOOKBACK_DAYS), as_of]],
        },
        fields=["name", "date", "withdrawal", "description"],
    ) if accounts else []
    payroll_or_vat_paid, bill_paid, transfer_paid = reconciled_bank_lines([line.name for line in lines])
    bill_days = [(day, amount) for _supplier, day, amount, _name in bills]
    kept = []
    for line in lines:
        amount = flt(line.withdrawal)
        if not cf.description_key(line.description):
            left_out["no words in the description"] += 1
        elif cf.is_payroll_or_vat(line.description):
            left_out["payroll or VAT words"] += 1
        elif line.name in payroll_or_vat_paid:
            left_out["payroll or VAT journal entry"] += 1
        elif line.name in bill_paid:
            left_out["paid by a bill's payment entry"] += 1
        elif line.name in transfer_paid:
            # the other leg is an account in the opening cash already: the money stays in the company
            left_out["transfer between own bank accounts"] += 1
        elif cf.paid_by_a_bill(line.date, amount, bill_days):
            left_out["amount of a bill"] += 1
        else:
            kept.append((cf.description_key(line.description), line.date, amount, line.name))
    return kept, left_out


def reconciled_bank_lines(names):
    """(payroll or VAT journal entry, purchase bill payment, transfer) as three sets of the bank transaction names: a
    Journal Entry on a salary (5xxx) or VAT liability (2200, 2202) account, a Payment Entry referencing a Purchase
    Invoice, and a transfer between the company's own accounts (a Journal Entry with two bank or cash lines, or an
    Internal Transfer Payment Entry)."""
    if not names:
        return set(), set(), set()
    payroll_or_vat = frappe.db.sql(
        """select distinct btp.parent
        from `tabBank Transaction Payments` btp
        join `tabJournal Entry Account` jea on jea.parent = btp.payment_entry
        join `tabAccount` a on a.name = jea.account
        where btp.payment_document = 'Journal Entry' and btp.parent in %s
          and (a.account_number like %s or a.account_number in %s)""",
        (names, "5%", cf.VAT_LIABILITY_ACCOUNTS),
    )
    bill = frappe.db.sql(
        """select distinct btp.parent
        from `tabBank Transaction Payments` btp
        join `tabPayment Entry Reference` per on per.parent = btp.payment_entry
        where btp.payment_document = 'Payment Entry' and btp.parent in %s
          and per.reference_doctype = 'Purchase Invoice'""",
        (names,),
    )
    transfer_je = frappe.db.sql(
        """select distinct btp.parent
        from `tabBank Transaction Payments` btp
        join `tabJournal Entry Account` jea on jea.parent = btp.payment_entry
        join `tabAccount` a on a.name = jea.account
        where btp.payment_document = 'Journal Entry' and btp.parent in %s
          and a.account_type in ('Bank', 'Cash')
        group by btp.parent, btp.payment_entry
        having count(*) >= 2""",
        (names,),
    )
    transfer_pe = frappe.db.sql(
        """select distinct btp.parent
        from `tabBank Transaction Payments` btp
        join `tabPayment Entry` pe on pe.name = btp.payment_entry
        where btp.payment_document = 'Payment Entry' and btp.parent in %s
          and pe.payment_type = 'Internal Transfer'""",
        (names,),
    )
    transfer = {row[0] for row in transfer_je} | {row[0] for row in transfer_pe}
    return {row[0] for row in payroll_or_vat}, {row[0] for row in bill}, transfer


def personnel_postings(company, as_of):
    """(posting date, account number, debit minus credit) per day and account of the personnel accounts (group 5) in
    the last year. cf.payroll_postings keeps the salary accounts of them."""
    rows = frappe.db.sql(
        """select gle.posting_date, a.account_number, sum(gle.debit - gle.credit)
        from `tabGL Entry` gle join `tabAccount` a on a.name = gle.account
        where gle.company = %s and gle.is_cancelled = 0 and a.is_group = 0 and a.account_number like %s
          and gle.posting_date between %s and %s
        group by gle.posting_date, a.account_number""",
        (company, "5%", as_of - datetime.timedelta(days=LOOKBACK_DAYS), as_of),
    )
    return [(day, number, flt(amount)) for day, number, amount in rows]


def vat_balances(company, as_of):
    """{account number: debit minus credit} of the VAT accounts on as_of."""
    numbers = cf.VAT_LIABILITY_ACCOUNTS + cf.VAT_INPUT_ACCOUNTS
    rows = frappe.db.sql(
        """select a.account_number, sum(gle.debit - gle.credit)
        from `tabGL Entry` gle join `tabAccount` a on a.name = gle.account
        where gle.company = %s and gle.is_cancelled = 0 and a.account_number in %s and gle.posting_date <= %s
        group by a.account_number""",
        (company, numbers, as_of),
    )
    return {number: flt(amount) for number, amount in rows}


def vat_paid_since(company, start, as_of):
    """The debits of the VAT liability accounts after start up to as_of: the payments to the tax office."""
    rows = frappe.db.sql(
        """select sum(gle.debit)
        from `tabGL Entry` gle join `tabAccount` a on a.name = gle.account
        where gle.company = %s and gle.is_cancelled = 0 and a.account_number in %s
          and gle.posting_date > %s and gle.posting_date <= %s""",
        (company, cf.VAT_LIABILITY_ACCOUNTS, start, as_of),
    )
    return flt(rows[0][0])


def opening_cash(company, as_of):
    """The Bank and Cash accounts' balance on as_of, in the company currency."""
    rows = frappe.db.sql(
        """select sum(gle.debit - gle.credit)
        from `tabGL Entry` gle join `tabAccount` a on a.name = gle.account
        where gle.company = %s and gle.is_cancelled = 0 and a.is_group = 0
          and a.account_type in ('Bank', 'Cash') and gle.posting_date <= %s""",
        (company, as_of),
    )
    return flt(rows[0][0])


def recurring_sources(company, as_of):
    """The recurring costs of each source as recurring_costs finds them: "bills" from the purchase bills, "bank"
    from the bank lines, keyed by their description's group key. "left_out" counts the bank lines each rule
    left out; "kept" is the number of bank lines that remain."""
    bills = bills_in_window(company, as_of)
    bank, left_out = bank_outflows(company, as_of, bills)
    return {
        "bills": cf.recurring_costs(bills, as_of),
        "bank": cf.recurring_costs(bank, as_of, strict=True),
        "left_out": left_out,
        "kept": len(bank),
    }


def insurer_suppliers(company):
    """The suppliers with a purchase bill that has an item booked to an insurer's payable (INSURER_PAYABLE_ACCOUNTS).
    The bill's own credit account is the supplier's payable (2000), so the item's expense account is what tells."""
    accounts = frappe.get_all(
        "Account", filters={"company": company, "account_number": ["between", list(INSURER_PAYABLE_ACCOUNTS)]},
        pluck="name")
    if not accounts:
        return set()
    rows = frappe.db.sql(
        """select distinct pi.supplier
        from `tabPurchase Invoice Item` pii join `tabPurchase Invoice` pi on pi.name = pii.parent
        where pi.company = %s and pi.docstatus = 1 and pii.expense_account in %s""",
        (company, accounts),
    )
    return {row[0] for row in rows}


def dry_run(company=None, as_of=None):
    """Counts only, for a read-only run of the bank source (bench execute): the groups per period, the bank lines
    they cover, the lines each rule left out, and the bank groups whose key is a bill supplier's key (a possible
    overlap of the two sources). The payroll and the insurers' bills: how many, not what they are. No description
    and no amount, so the output can go in a note."""
    company = company or frappe.defaults.get_user_default("company")
    as_of = getdate(as_of or nowdate())
    sources = recurring_sources(company, as_of)
    bill_keys = {cf.description_key(item["supplier"]) for item in sources["bills"]}
    insurers = insurer_suppliers(company)
    lines = compute(company, as_of)["lines"]
    salary_months = {(day.year, day.month) for day, _amount in cf.payroll_postings(personnel_postings(company, as_of))}
    return {
        "bill_groups": len(sources["bills"]),
        "bill_groups_per_period": dict(collections.Counter(item["period"] for item in sources["bills"])),
        "insurer_bill_groups_per_period": dict(collections.Counter(
            item["period"] for item in sources["bills"] if item["supplier"] in insurers)),
        "insurer_open_bills": sum(1 for party, _amount, _due in open_documents(company, as_of, "Purchase Invoice").values()
                                  if party in insurers),
        "insurer_recurring_lines": sum(1 for l in lines if l["kind"] == "recurring" and l["party"] in insurers),
        "payroll_salary_months": len(salary_months),
        "payroll_lines": sum(1 for l in lines if l["kind"] == "payroll"),
        "bank_lines_kept": sources["kept"],
        "bank_lines_left_out": dict(sources["left_out"]),
        "bank_groups_per_period": dict(collections.Counter(item["period"] for item in sources["bank"])),
        "bank_lines_in_groups": sum(item["count"] for item in sources["bank"]),
        "bank_groups_with_a_bill_supplier_key": sum(1 for item in sources["bank"] if item["supplier"] in bill_keys),
    }


def line(kind, day, amount, party, doctype, name, note):
    return {"kind": kind, "day": day, "amount": round(amount, 2), "party": party,
            "doctype": doctype, "name": name, "note": note}


def compute(company, as_of, include_run_rate=True, include_new_purchases=True):
    """The forecast for the company as of the date: the weeks, the lowest week, the lines in the horizon, and
    the counts the messages tell. include_run_rate: the new sales run-rate's lines are in the forecast (see the
    module's docstring); run_rate is its weekly amount and the first day of its oldest window, None when left out.
    include_new_purchases: the same for the new purchases run-rate, new_purchases its weekly amount, since and the
    excluded suppliers' count."""
    horizon = cf.horizon_end(as_of)
    opening = opening_cash(company, as_of)
    receivables = open_documents(company, as_of, "Sales Invoice")
    payables = open_documents(company, as_of, "Purchase Invoice")
    history = paid_history(company, as_of, "Sales Invoice", set(receivables))
    late = {party: cf.average_days_late(pairs) for party, pairs in history.items()}

    lines = []
    without_history = set()
    for name, (party, amount, due) in receivables.items():
        if party not in late:
            without_history.add(party)
        days = late.get(party, 0)
        note = _("due {0}, {1} days late on average").format(due, days) if party in late else _("due {0}, no paid invoice in the last year").format(due)
        lines.append(line(cf.RECEIPT, cf.expected_receipt(due, days), amount, party, "Sales Invoice", name, note))

    for name, (party, amount, due) in payables.items():
        lines.append(line("bill", due, amount, party, "Purchase Invoice", name, _("due {0}").format(due)))

    sources = recurring_sources(company, as_of)
    run_rate = None
    if include_run_rate:
        # the invoices not issued yet on the as-of date: a receipt every week, the mean of the recent windows
        weekly, since = cf.run_rate(sales_paid_in_windows(company, as_of), as_of)
        note = _("new sales run-rate: the mean of the last four 13-week windows since {0}, the receipts from the invoices issued in each window, a thirteenth each week").format(since)
        for week in range(1, cf.WEEKS + 1):
            lines.append(line(cf.NEW_SALES, cf.week_bounds(week, as_of)[0], weekly, "", "", "", note))
        run_rate = {"weekly": weekly, "since": since}

    new_purchases = None
    if include_new_purchases:
        # the bills not posted yet on the as-of date: a payment every week, the mean of the recent windows. The suppliers
        # the recurring costs forecast and the insurers' bills are left out: they are forecast already
        excluded = {item["supplier"] for item in sources["bills"]} | insurer_suppliers(company)
        weekly, since, excluded_count = cf.purchase_run_rate(purchase_paid_in_windows(company, as_of), as_of, excluded)
        note = _("new purchases run-rate: the mean of the last four 13-week windows since {0}, the payments of the bills posted in each window, {1} suppliers left out (recurring or insurer), a thirteenth each week").format(
            since, excluded_count)
        for week in range(1, cf.WEEKS + 1):
            lines.append(line(cf.NEW_PURCHASES, cf.week_bounds(week, as_of)[0], weekly, "", "", "", note))
        new_purchases = {"weekly": weekly, "since": since, "excluded": excluded_count}

    open_due = collections.defaultdict(list)
    for _name, (party, _amount, due) in payables.items():
        open_due[party].append(due)
    for item in sources["bills"]:
        for day in cf.recurring_dates(item, as_of, horizon):
            if any(abs((day - due).days) <= cf.OPEN_BILL_MATCH_DAYS for due in open_due[item["supplier"]]):
                continue
            note = _("{0}, {1} bills, from the last one").format(item["period"], item["count"])
            lines.append(line("recurring", day, item["amount"], item["supplier"], "Purchase Invoice", item["last_bill"], note))
    for item in sources["bank"]:
        for day in cf.recurring_dates(item, as_of, horizon):
            note = _("{0}, {1} bank lines described “{2}”, from the last one").format(
                item["period"], item["count"], item["supplier"])
            lines.append(line("recurring", day, item["amount"], "", "Bank Transaction", item["last_bill"], note))

    amount = cf.payroll_from_postings(cf.payroll_postings(personnel_postings(company, as_of)))
    if amount:
        note = _("average of the last three salary months (accounts {0} to {1}), run on the 25th or the Friday before a weekend").format(
            *cf.SALARY_ACCOUNTS)
        for day in cf.payroll_dates(as_of, horizon):
            lines.append(line("payroll", day, amount, "", "", "", note))

    closed_end = cf.quarter_start(as_of) - datetime.timedelta(days=1)
    owed_closed, owed_this_quarter = cf.vat_split(
        cf.vat_owed(vat_balances(company, closed_end)), vat_paid_since(company, closed_end, as_of),
        cf.vat_owed(vat_balances(company, as_of)))
    for day, amount, end in cf.vat_lines(as_of, owed_closed, owed_this_quarter):
        if end == closed_end:
            basis = _("VAT of the quarter ending {0}, as the books owed it on {1}").format(end, closed_end)
        else:
            basis = _("VAT booked in the quarter ending {0} so far, as of {1}").format(end, as_of)
        if amount < 0:
            basis = _("refund, {0}").format(basis)
        lines.append(line("vat", day, amount, "", "", "", basis))

    weeks, lowest, beyond = cf.forecast(as_of, opening, lines)
    return {
        "opening": opening,
        "weeks": weeks,
        "lowest": lowest,
        "beyond": beyond,
        "lines": sorted((l for l in lines if "week" in l), key=lambda l: (l["week"], l["day"])),
        "without_history": len(without_history),
        "run_rate": run_rate,
        "new_purchases": new_purchases,
    }


def execute(filters=None):
    filters = frappe._dict(filters or {})
    company = filters.company or frappe.defaults.get_user_default("company")
    as_of = getdate(filters.as_of_date or nowdate())
    currency = frappe.get_cached_value("Company", company, "default_currency")
    result = compute(company, as_of, include_run_rate=cint(filters.get("include_run_rate", 1)),
                     include_new_purchases=cint(filters.get("include_new_purchases", 1)))
    weeks, lowest, opening = result["weeks"], result["lowest"], result["opening"]

    rows = [
        {
            "week": row["week"], "from": row["start"], "to": row["end"],
            "receipt": row["receipt"], "new_sales": row["new_sales"], "bill": row["bill"],
            "new_purchases": row["new_purchases"], "recurring": row["recurring"],
            "payroll": row["payroll"], "vat": row["vat"], "net": row["net"], "closing": row["closing"],
            "lowest": _("lowest") if row["week"] == lowest else None,
        }
        for row in weeks
    ]

    lowest_closing = weeks[lowest - 1]["closing"]
    final_closing = weeks[-1]["closing"]
    summary = [
        {"value": opening, "label": _("Opening cash"), "datatype": "Currency", "currency": currency, "indicator": "Blue"},
        {"value": lowest_closing, "label": _("Lowest, week {0}").format(lowest), "datatype": "Currency",
         "currency": currency, "indicator": "Red" if lowest_closing < 0 else "Orange"},
        {"value": final_closing, "label": _("End of week {0}").format(len(weeks)), "datatype": "Currency",
         "currency": currency, "indicator": "Red" if final_closing < 0 else "Green"},
    ]

    chart = {
        "data": {
            "labels": [_("Today")] + [_("Week {0}").format(row["week"]) for row in weeks],
            "datasets": [
                {"name": _("Closing cash"), "values": [opening] + [row["closing"] for row in weeks]},
                {"name": _("Lowest point"), "values": [None] + [
                    row["closing"] if row["week"] == lowest else None for row in weeks]},
            ],
        },
        "type": "line",
    }

    messages = []
    if result["run_rate"]:
        messages.append(_("New sales are in the forecast at {0} a week: the mean of the last four 13-week windows since {1}, receipts from the invoices issued in each window.").format(
            fmt_money(result["run_rate"]["weekly"], currency=currency), result["run_rate"]["since"]))
    if result["new_purchases"]:
        messages.append(_("New purchases are in the forecast at {0} a week: the mean of the last four 13-week windows since {1}, payments of the bills posted in each window, {2} suppliers left out.").format(
            fmt_money(result["new_purchases"]["weekly"], currency=currency), result["new_purchases"]["since"],
            result["new_purchases"]["excluded"]))
    if result["beyond"]:
        messages.append(_("{0} lines fall after week {1} and are not in the forecast.").format(result["beyond"], len(weeks)))
    if result["without_history"]:
        messages.append(_("{0} customers have no paid invoice in the last year: their open invoices are taken as paid on the due date.").format(result["without_history"]))
    message = "<br>".join(escape_html(m) for m in messages) or None
    return columns(currency), rows, message, chart, summary


def columns(currency):
    return [
        {"label": _("Week"), "fieldname": "week", "fieldtype": "Int", "width": 70},
        {"label": _("From"), "fieldname": "from", "fieldtype": "Date", "width": 110},
        {"label": _("To"), "fieldname": "to", "fieldtype": "Date", "width": 110},
        {"label": _("Receipts"), "fieldname": "receipt", "fieldtype": "Float", "precision": 2, "width": 130},
        {"label": _("New sales"), "fieldname": "new_sales", "fieldtype": "Float", "precision": 2, "width": 120},
        {"label": _("Supplier bills"), "fieldname": "bill", "fieldtype": "Float", "precision": 2, "width": 130},
        {"label": _("New purchases"), "fieldname": "new_purchases", "fieldtype": "Float", "precision": 2, "width": 130},
        {"label": _("Recurring costs"), "fieldname": "recurring", "fieldtype": "Float", "precision": 2, "width": 130},
        {"label": _("Payroll"), "fieldname": "payroll", "fieldtype": "Float", "precision": 2, "width": 120},
        {"label": _("VAT"), "fieldname": "vat", "fieldtype": "Float", "precision": 2, "width": 120},
        {"label": _("Net"), "fieldname": "net", "fieldtype": "Float", "precision": 2, "width": 120},
        {"label": _("Closing cash ({0})").format(currency), "fieldname": "closing", "fieldtype": "Float", "precision": 2, "width": 150},
        {"label": _("Lowest point"), "fieldname": "lowest", "fieldtype": "Data", "width": 110},
    ]
