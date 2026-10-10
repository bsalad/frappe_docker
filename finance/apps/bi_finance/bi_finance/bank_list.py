"""The Booking Date of lines that were imported before the field existed (finance/bexio/backfill_booking_date.py)."""

import frappe


@frappe.whitelist(methods=["POST"])
def set_booking_dates(dates):
    """dates: {Bank Transaction name: ISO date}. Fills only the lines whose Booking Date is empty and returns how many.

    A plain set_value, not a save: no Version, no Comment per line, and `modified` stays as it was, because the date
    is a column, not an edit by anyone."""
    frappe.only_for("System Manager")
    if isinstance(dates, str):
        dates = frappe.parse_json(dates)
    filled = 0
    for name, day in dates.items():
        if not frappe.db.get_value("Bank Transaction", name, "booking_date"):
            frappe.db.set_value("Bank Transaction", name, "booking_date", frappe.utils.getdate(day), update_modified=False)
            filled += 1
    return filled
