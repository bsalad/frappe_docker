"""Cash Flow Forecast Lines: each line of the 13-week forecast, with the document it comes from.

The same computation as the Cash Flow Forecast report (report/cash_flow_forecast), line by line: the week, the
day it is expected, the party and the source document. Inflows are positive, outflows negative.
"""

import frappe
from frappe import _
from frappe.utils import cint, escape_html, getdate, nowdate

from bi_finance import cash_forecast as cf
from bi_finance.bi_finance.report.cash_flow_forecast.cash_flow_forecast import closing_band_messages, compute, kind_label


def execute(filters=None):
    filters = frappe._dict(filters or {})
    company = filters.company or frappe.defaults.get_user_default("company")
    as_of = getdate(filters.as_of_date or nowdate())
    currency = frappe.get_cached_value("Company", company, "default_currency")
    result = compute(company, as_of, include_run_rate=cint(filters.get("include_run_rate", 1)),
                     include_new_purchases=cint(filters.get("include_new_purchases", 1)),
                     include_owner_accounts=cint(filters.get("include_owner_accounts", 1)))

    rows = []
    for line in result["lines"]:
        sign = 1 if line["kind"] in cf.INFLOW_KINDS else -1
        rows.append({
            "week": line["week"],
            "expected": line["day"],
            "type": kind_label(line["kind"]),
            "party": line["party"],
            "document_type": line["doctype"],
            "document": line["name"],
            "amount": sign * line["amount"],
            "note": line["note"],
        })
    # the band of each run-rate is in its lines' note; the closing cash with its band has no line of its own
    message = "<br>".join(escape_html(m) for m in closing_band_messages(result, currency)) or None
    return columns(), rows, message


def columns():
    return [
        {"label": _("Week"), "fieldname": "week", "fieldtype": "Int", "width": 70},
        {"label": _("Expected"), "fieldname": "expected", "fieldtype": "Date", "width": 110},
        {"label": _("Type"), "fieldname": "type", "fieldtype": "Data", "width": 130},
        {"label": _("Party"), "fieldname": "party", "fieldtype": "Data", "width": 200},
        {"label": _("Document type"), "fieldname": "document_type", "fieldtype": "Data", "width": 130},
        {"label": _("Document"), "fieldname": "document", "fieldtype": "Data", "width": 170},
        {"label": _("Amount"), "fieldname": "amount", "fieldtype": "Float", "precision": 2, "width": 130},
        {"label": _("Note"), "fieldname": "note", "fieldtype": "Data", "width": 360},
    ]
