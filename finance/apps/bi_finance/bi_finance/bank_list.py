"""The Booking Date and Value Date of lines that were imported before the fields existed (finance/bexio/backfill_booking_date.py)."""

import frappe

DATE_FIELDS = ("booking_date", "value_date")


@frappe.whitelist(methods=["POST"])
def set_booking_dates(dates, field="booking_date"):
    """dates: {Bank Transaction name: ISO date}, for the field (booking_date or value_date). Fills only the lines whose
    field is empty and returns how many.

    A plain set_value, not a save: no Version, no Comment per line, and `modified` stays as it was, because the date
    is a column, not an edit by anyone."""
    frappe.only_for("System Manager")
    if field not in DATE_FIELDS:
        frappe.throw("Not a date of a bank line: {0}".format(field))
    if isinstance(dates, str):
        dates = frappe.parse_json(dates)
    filled = 0
    for name, day in dates.items():
        if not frappe.db.get_value("Bank Transaction", name, field):
            frappe.db.set_value("Bank Transaction", name, field, frappe.utils.getdate(day), update_modified=False)
            filled += 1
    return filled
