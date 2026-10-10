"""Cash Flow Forecast: 13 weeks of cash from the as-of date, each line traceable to its document.

The dates and sums are in bi_finance/cash_forecast.py. This module reads the books for them:
- opening: the Bank and Cash accounts' balance in the company currency, as Cash Position reads it;
- receipts: the open Sales Invoices (Payment Ledger), on the due date moved by the customer's average days late
  over the last year, from the invoices paid in that year;
- bills: the open Purchase Invoices, on the due date;
- recurring costs: the suppliers whose bills repeat monthly, quarterly or yearly over the last year, unless an
  open bill of theirs falls due within a couple of weeks of the occurrence;
- payroll: the salary accounts (group 5) of the last year, the average of the last three months, paid on the 25th
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
from frappe.utils import escape_html, flt, getdate, nowdate

from bi_finance import cash_forecast as cf

LOOKBACK_DAYS = 365


def kind_label(kind):
    """The line kind's label, translated when asked for (a module-level _() would run before any language is set)."""
    return {
        cf.RECEIPT: _("Customer receipt"),
        "bill": _("Supplier bill"),
        "recurring": _("Recurring cost"),
        "payroll": _("Payroll"),
        "vat": _("VAT"),
    }[kind]


def open_documents(company, as_of, doctype):
    """{name: (party, amount owed in the company currency, due date)} of the doctype's invoices unpaid on as_of.
    Read from the Payment Ledger, so a past date gives the invoices open then. Sales Invoices are positive there,
    Purchase Invoices negative."""
    sign = 1 if doctype == "Sales Invoice" else -1
    rows = frappe.db.sql(
        """select ple.against_voucher_no, max(ple.party), sum(ple.amount)
        from `tabPayment Ledger Entry` ple
        where ple.company = %s and ple.against_voucher_type = %s and ple.delinked = 0 and ple.posting_date <= %s
        group by ple.against_voucher_no
        having sum(ple.amount) * %s > 0.005""",
        (company, doctype, as_of, sign),
    )
    invoices = _invoice_dates(doctype, [name for name, _party, _amount in rows])
    return {
        name: (party, sign * flt(amount), invoices[name])
        for name, party, amount in rows
    }


def _invoice_dates(doctype, names):
    """{name: due date, or the posting date when the invoice has none}."""
    if not names:
        return {}
    found = frappe.get_all(doctype, filters={"name": ["in", names]}, fields=["name", "due_date", "posting_date"])
    return {invoice.name: invoice.due_date or invoice.posting_date for invoice in found}


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


def salary_postings(company, as_of):
    """(posting date, debit minus credit) per day of the salary accounts (group 5) in the last year."""
    rows = frappe.db.sql(
        """select gle.posting_date, sum(gle.debit - gle.credit)
        from `tabGL Entry` gle join `tabAccount` a on a.name = gle.account
        where gle.company = %s and gle.is_cancelled = 0 and a.is_group = 0 and a.account_number like %s
          and gle.posting_date between %s and %s
        group by gle.posting_date""",
        (company, "5%", as_of - datetime.timedelta(days=LOOKBACK_DAYS), as_of),
    )
    return [(day, flt(amount)) for day, amount in rows]


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


def line(kind, day, amount, party, doctype, name, note):
    return {"kind": kind, "day": day, "amount": round(amount, 2), "party": party,
            "doctype": doctype, "name": name, "note": note}


def compute(company, as_of):
    """The forecast for the company as of the date: the weeks, the lowest week, the lines in the horizon, and
    the counts the messages tell."""
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

    open_due = collections.defaultdict(list)
    for _name, (party, _amount, due) in payables.items():
        open_due[party].append(due)
    for item in cf.recurring_costs(bills_in_window(company, as_of), as_of):
        for day in cf.recurring_dates(item, as_of, horizon):
            if any(abs((day - due).days) <= cf.OPEN_BILL_MATCH_DAYS for due in open_due[item["supplier"]]):
                continue
            note = _("{0}, from its last bill").format(item["period"])
            lines.append(line("recurring", day, item["amount"], item["supplier"], "Purchase Invoice", item["last_bill"], note))

    amount = cf.payroll_from_postings(salary_postings(company, as_of))
    if amount:
        note = _("average of the last three salary months, run on the 25th or the Friday before a weekend")
        for day in cf.payroll_dates(as_of, horizon):
            lines.append(line("payroll", day, amount, "", "", "", note))

    closed_end = cf.quarter_start(as_of) - datetime.timedelta(days=1)
    owed_closed = cf.vat_owed(vat_balances(company, closed_end))
    owed_this_quarter = cf.vat_owed(vat_balances(company, as_of)) - owed_closed
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
    }


def execute(filters=None):
    filters = frappe._dict(filters or {})
    company = filters.company or frappe.defaults.get_user_default("company")
    as_of = getdate(filters.as_of_date or nowdate())
    currency = frappe.get_cached_value("Company", company, "default_currency")
    result = compute(company, as_of)
    weeks, lowest, opening = result["weeks"], result["lowest"], result["opening"]

    rows = [
        {
            "week": row["week"], "from": row["start"], "to": row["end"],
            "receipt": row["receipt"], "bill": row["bill"], "recurring": row["recurring"],
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
        {"label": _("Supplier bills"), "fieldname": "bill", "fieldtype": "Float", "precision": 2, "width": 130},
        {"label": _("Recurring costs"), "fieldname": "recurring", "fieldtype": "Float", "precision": 2, "width": 130},
        {"label": _("Payroll"), "fieldname": "payroll", "fieldtype": "Float", "precision": 2, "width": 120},
        {"label": _("VAT"), "fieldname": "vat", "fieldtype": "Float", "precision": 2, "width": 120},
        {"label": _("Net"), "fieldname": "net", "fieldtype": "Float", "precision": 2, "width": 120},
        {"label": _("Closing cash ({0})").format(currency), "fieldname": "closing", "fieldtype": "Float", "precision": 2, "width": 150},
        {"label": _("Lowest point"), "fieldname": "lowest", "fieldtype": "Data", "width": 110},
    ]
