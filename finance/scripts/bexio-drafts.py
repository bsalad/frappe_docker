"""Insert the bexio drafts into ERPNext: the loader bexio-drafts.sh runs inside the backend container.

Its input, on stdin, is the plan that import_sales.py and import_purchase.py write (with --apply) under
<private>: {"documents": [{"doctype", "name", "bexio_id", "values"}], "exchange_rates": [{"date", "from", "to", "rate"}]}.

Each document is a draft, docstatus 0, so nothing posts. It is named by bexio's number (the name is set on
insert, which bypasses the naming series, so the ACC-SINV and ACC-PINV counters do not move). A document is
keyed by bexio_id: one that exists as a draft is updated in place, one that is submitted is skipped, and a
name that another document holds is a conflict and is left alone. A rerun changes nothing that is already
right. Each document is committed on its own, so a run that stops half way resumes where it stopped.

The loader runs as Administrator, as swiss-setup.py does: the API user may not name documents (the
run is not a rights change). Only the core in apply_plan is tested offline (test_bexio_drafts.py); the
Frappe store is the one part that needs the backend container.

Standard library only; frappe is imported where the store is made, inside the container.
"""

import html
import json
import sys
from collections import Counter

SITE = "frontend"
DRAFT = 0
# fields the importers hand over that ERPNext's doctypes do not have: the files come with erp-a2ma
DROP = ("bexio_attachment_ids",)


def norm(value):
    """A value as the importer and the database both write it: a number to six places, text as text, nothing when empty.

    ERPNext's text editor writes a line break as <br />, the importer as <br>, a newline as LF where the
    importer has CRLF, and a non-ASCII letter as an entity (&uuml;): the same text, so both are written the same way.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return round(float(value), 6)
    if isinstance(value, str):
        try:
            return round(float(value), 6)
        except ValueError:
            return html.unescape(value.replace("\r\n", "\n").replace("<br />", "<br>").replace("<br/>", "<br>"))
    return str(value)


def row_differs(row, want):
    return any(norm(row.get(key)) != norm(value) for key, value in want.items())


def differs(have, want):
    """Whether the fields the importer gives differ from the document's. A table compares row by row, in order."""
    for key, value in want.items():
        if isinstance(value, list):
            rows = have.get(key) or []
            if len(rows) != len(value) or any(row_differs(r, w) for r, w in zip(rows, value)):
                return True
        elif norm(have.get(key)) != norm(value):
            return True
    return False


def load_document(item, store):
    """'created', 'updated', 'unchanged', 'skipped' (submitted), or 'conflict' (the name is another document's)."""
    doctype, name, bexio_id = item["doctype"], str(item["name"]), str(item["bexio_id"])
    values = {key: value for key, value in item["values"].items() if key not in DROP}
    found = store.find(doctype, bexio_id)
    if found:
        current, docstatus = found
        if docstatus != DRAFT:
            return "skipped"
        if not differs(store.values(doctype, current), values):
            return "unchanged"
        store.update(doctype, current, values)
        return "updated"
    if store.exists(doctype, name):
        return "conflict"
    store.insert(doctype, name, values)
    return "created"


def apply_plan(plan, store):
    """Load the exchange rates, then the documents, committing each one; returns the counts and the failures."""
    counts, failures = Counter(), []
    for rate in plan.get("exchange_rates", []):
        if store.rate_exists(rate):
            counts["exchange rate unchanged"] += 1
        else:
            store.insert_rate(rate)
            counts["exchange rate created"] += 1
        store.commit()
    for item in plan["documents"]:
        try:
            status = load_document(item, store)
            store.commit()
        except Exception as err:  # one document's failure must not stop the others; it is listed by bexio id
            store.rollback()
            status = "failed"
            failures.append((item["doctype"], str(item["bexio_id"]), "{}: {}".format(type(err).__name__, err)))
        counts[(item["doctype"], status)] += 1
    return counts, failures


class FrappeStore:
    """The database calls of apply_plan, through frappe. Only used inside the backend container."""

    def __init__(self, frappe):
        self.frappe = frappe

    def find(self, doctype, bexio_id):
        row = self.frappe.db.get_value(doctype, {"bexio_id": bexio_id}, ["name", "docstatus"], as_dict=True)
        return (row.name, row.docstatus) if row else None

    def exists(self, doctype, name):
        return bool(self.frappe.db.exists(doctype, name))

    def values(self, doctype, name):
        return self.frappe.get_doc(doctype, name).as_dict()

    def insert(self, doctype, name, values):
        doc = self.frappe.get_doc(dict(values, doctype=doctype))
        # set_name keeps bexio's number as the name and bypasses the naming series
        doc.insert(set_name=name)

    def update(self, doctype, name, values):
        doc = self.frappe.get_doc(doctype, name)
        doc.update(values)
        doc.save()

    def rate_exists(self, rate):
        return bool(self.frappe.db.exists("Currency Exchange", {
            "date": rate["date"], "from_currency": rate["from"], "to_currency": rate["to"], "for_selling": 1}))

    def insert_rate(self, rate):
        self.frappe.get_doc({
            "doctype": "Currency Exchange", "date": rate["date"], "from_currency": rate["from"],
            "to_currency": rate["to"], "exchange_rate": rate["rate"], "for_selling": 1, "for_buying": 0,
        }).insert()

    def commit(self):
        self.frappe.db.commit()

    def rollback(self):
        self.frappe.db.rollback()


def main(stdin):
    import frappe  # the backend container's; the core above runs without it

    plan = json.load(stdin)
    frappe.init(site=SITE)
    frappe.connect()
    frappe.set_user("Administrator")
    counts, failures = apply_plan(plan, FrappeStore(frappe))
    for key in sorted(counts, key=str):
        if isinstance(key, tuple):
            print("{:<18}{:<12}{:>6}".format(key[0], key[1], counts[key]))
        else:
            print("{:<30}{:>6}".format(key, counts[key]))
    for doctype, bexio_id, message in failures:
        print("failed {} {}: {}".format(doctype, bexio_id, message))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.stdin))
