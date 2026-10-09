"""Which ERPNext document carries each bexio journal line, and the check that every line is carried once.

Reads an export directory (export.py's output) and assigns every line of journal.json to
exactly one bucket. A bucket names the ERPNext doctype that posts the line, so the import
of a bucket books it once and nothing else books it. The check at the end is the proof:
every journal line id appears in exactly one bucket, and per year and per account the
buckets add up to the raw journal's totals.

Run it as:
    python3 finance/bexio/posting_plan.py <export dir>

It prints counts and sums per bucket and year (CHF, base currency). Those numbers are
company data: the output goes to the private posting plan, never into the repository.
This module holds no data; its tests use invented lines (test_posting_plan.py).

Source of each bucket, from the journal's own fields:
  KbInvoice                    -> sales_invoice     (Sales Invoice)
  KbClientAccountEntry, ref_id -> an invoice payment -> sales_payment (Payment Entry, customer)
  KbClientAccountEntry, other  -> a bill payment     -> purchase_payment (Payment Entry, supplier)
  KbBill                       -> purchase_invoice  (Purchase Invoice)
  KbCreditVoucher              -> credit_voucher    (Sales Invoice, is_return)
  manual entry of type banking_transaction (and its tax line) -> bank_booking (Journal Entry)
  other manual entries (and their tax lines)                  -> manual (Journal Entry)
  01-01 carry-forward lines                                   -> carry_forward (Journal Entry)
  any other line with no export source (payroll, ...)          -> unsourced (Journal Entry)

A manual entry's tax line has no id of its own in the manual entries file, and is found in the
journal by its date and description (the parent's); the parent's bucket is the line's bucket.
"""

import argparse
import collections
import json
import os
import sys

BUCKETS = {
    "sales_invoice": "Sales Invoice",
    "sales_payment": "Payment Entry (customer)",
    "purchase_invoice": "Purchase Invoice",
    "purchase_payment": "Payment Entry (supplier)",
    "credit_voucher": "Sales Invoice (is_return)",
    "bank_booking": "Journal Entry (banking_transaction)",
    "manual": "Journal Entry (manual)",
    "carry_forward": "Journal Entry (opening, per year)",
    "unsourced": "Journal Entry (no export source)",
}

# bexio names the carry-forward lines of each year-start in these words (German and English).
CARRY_FORWARD_WORDS = ("Saldovortrag", "carried forward")


def load(export_dir):
    def read(name):
        with open(os.path.join(export_dir, name + ".json"), encoding="utf-8") as f:
            return json.load(f)

    return {
        "journal": read("journal"),
        "manual_entries": read("manual_entries"),
        "invoice_payments": read("invoice_payments"),
    }


def classify(data):
    """{journal line id: bucket} for every line of the journal; each line gets exactly one bucket."""
    manual_rows = {}
    for entry in data["manual_entries"]:
        for row in entry["entries"]:
            if row.get("id") is not None:
                manual_rows[row["id"]] = (entry, row)
    # A manual row's tax line: found by the parent's date and the row's description.
    by_date_text = collections.defaultdict(list)
    for entry, row in manual_rows.values():
        by_date_text[(str(entry["date"])[:10], row["description"])].append(entry)

    invoice_payment_ids = {row["id"] for part in data["invoice_payments"] for row in part["rows"]}

    buckets = {}
    for line in data["journal"]:
        ref_class = line["ref_class"]
        if ref_class == "KbInvoice":
            bucket = "sales_invoice"
        elif ref_class == "KbBill":
            bucket = "purchase_invoice"
        elif ref_class == "KbCreditVoucher":
            bucket = "credit_voucher"
        elif ref_class == "KbClientAccountEntry":
            bucket = "sales_payment" if line["ref_id"] in invoice_payment_ids else "purchase_payment"
        elif line["id"] in manual_rows:
            bucket = _manual_bucket(manual_rows[line["id"]][0])
        elif _is_carry_forward(line):
            bucket = "carry_forward"
        else:
            parents = by_date_text.get((line["date"][:10], line["description"]), [])
            buckets_of_parents = {_manual_bucket(entry) for entry in parents}
            if len(buckets_of_parents) == 1:
                bucket = buckets_of_parents.pop()
            else:
                bucket = "unsourced"
        buckets[line["id"]] = bucket
    return buckets


def _manual_bucket(entry):
    return "bank_booking" if entry["type"] == "banking_transaction" else "manual"


def _is_carry_forward(line):
    return line["date"][5:10] == "01-01" and any(word in line["description"] for word in CARRY_FORWARD_WORDS)


def coverage(data, buckets):
    """The check: every line in exactly one bucket, and per year and account the buckets add up to the journal.

    Returns the list of problems (empty when the plan covers the whole journal).
    """
    problems = []
    ids = [line["id"] for line in data["journal"]]
    if len(ids) != len(set(ids)):
        problems.append("journal ids are not unique")
    missing = [i for i in ids if i not in buckets]
    if missing:
        problems.append("{} journal lines have no bucket".format(len(missing)))
    unknown = [i for i, b in buckets.items() if b not in BUCKETS]
    if unknown:
        problems.append("{} lines have an unknown bucket".format(len(unknown)))
    extra = set(buckets) - set(ids)
    if extra:
        problems.append("{} buckets name no journal line".format(len(extra)))

    raw = _totals(data["journal"], lambda line: line["date"][:4])
    planned = _totals(data["journal"], lambda line: line["date"][:4], buckets=buckets)
    for key in set(raw) | set(planned):
        if abs(raw.get(key, 0.0) - planned.get(key, 0.0)) > 0.005:
            problems.append("sum differs for account {} in {}".format(key[1], key[0]))
    return problems


def _totals(lines, year_of, buckets=None):
    """Debit minus credit in base currency per (year, account), summed over lines (or only those in buckets)."""
    totals = collections.defaultdict(float)
    for line in lines:
        if buckets is not None and line["id"] not in buckets:
            continue
        amount = line["base_currency_amount"]
        year = year_of(line)
        totals[(year, line["debit_account_id"])] += amount
        totals[(year, line["credit_account_id"])] -= amount
    return totals


def summary(data, buckets):
    """{(bucket, year): (count, sum of base currency amounts)}."""
    table = collections.defaultdict(lambda: [0, 0.0])
    for line in data["journal"]:
        cell = table[(buckets[line["id"]], line["date"][:4])]
        cell[0] += 1
        cell[1] += line["base_currency_amount"]
    return {key: (count, round(total, 2)) for key, (count, total) in table.items()}


def main(argv):
    parser = argparse.ArgumentParser(description="Assign every bexio journal line to the ERPNext doctype that carries it.")
    parser.add_argument("export_dir", help="the export directory (export.py --out)")
    args = parser.parse_args(argv)
    data = load(args.export_dir)
    buckets = classify(data)
    for (bucket, year), (count, total) in sorted(summary(data, buckets).items(), key=lambda item: (item[0][1], item[0][0])):
        print("{:<16} {}  {:>6} lines  {:>16,.2f} CHF  {}".format(bucket, year, count, total, BUCKETS[bucket]))
    problems = coverage(data, buckets)
    if problems:
        for problem in problems:
            print("COVERAGE FAILED: " + problem)
        return 1
    print("coverage: every journal line in exactly one bucket; per year and account the buckets add up to the journal")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
