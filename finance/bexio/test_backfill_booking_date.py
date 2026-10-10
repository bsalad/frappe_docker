"""Offline tests for backfill_booking_date.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import json
import unittest
from unittest import mock

import backfill_booking_date as bb

RECORDS = [
    {"id": 1, "value_date": "2026-03-30", "book_date": "2026-03-31T00:00:00+02:00"},
    {"id": 2, "value_date": "2026-04-02", "book_date": None},
    {"id": 3, "value_date": "2026-04-03", "book_date": "2026-04-04"},
]


def row(name, bexio_id, booking_date=None, value_date=None):
    return {"name": name, "bexio_id": bexio_id, "booking_date": booking_date, "value_date": value_date}


class PlanTest(unittest.TestCase):
    def test_a_line_gets_the_book_date_or_else_the_value_date(self):
        dates, counts = bb.plan(RECORDS, [row("BT-1", "1"), row("BT-2", "2")])
        self.assertEqual(dates, {"BT-1": "2026-03-31", "BT-2": "2026-04-02"})
        self.assertEqual(counts["to_fill"], 2)

    def test_a_line_with_a_booking_date_is_left_alone(self):
        dates, counts = bb.plan(RECORDS, [row("BT-3", "3", "2026-01-01")])
        self.assertEqual(dates, {})
        self.assertEqual(counts["already_set"], 1)

    def test_a_line_the_export_does_not_know_is_counted_not_filled(self):
        dates, counts = bb.plan(RECORDS, [row("BT-9", "99")])
        self.assertEqual(dates, {})
        self.assertEqual(counts["not_in_export"], 1)

    def test_a_second_run_has_nothing_to_fill(self):
        dates, _ = bb.plan(RECORDS, [row("BT-1", "1")])
        rows = [row(name, "1", day) for name, day in dates.items()]
        self.assertEqual(bb.plan(RECORDS, rows)[0], {})

    def test_the_summary_has_counts_only(self):
        _, counts = bb.plan(RECORDS, [row("BT-1", "1"), row("BT-9", "99")])
        text = bb.summary(counts)
        self.assertIn("to fill: 1", text)
        self.assertNotIn("BT-", text)

    def test_the_value_date_fills_the_export_value_date_not_the_book_date(self):
        dates, counts = bb.plan(RECORDS, [row("BT-1", "1", None, None), row("BT-3", "3", None, None)], "value_date")
        self.assertEqual(dates, {"BT-1": "2026-03-30", "BT-3": "2026-04-03"})
        self.assertEqual(counts["to_fill"], 2)

    def test_a_value_date_already_set_is_left_alone(self):
        dates, counts = bb.plan(RECORDS, [row("BT-1", "1", None, "2026-03-30")], "value_date")
        self.assertEqual(dates, {})
        self.assertEqual(counts["already_set"], 1)

    def test_a_line_with_a_value_date_still_gets_its_booking_date(self):
        dates, _ = bb.plan(RECORDS, [row("BT-1", "1", None, "2026-03-30")], "booking_date")
        self.assertEqual(dates, {"BT-1": "2026-03-31"})

    def test_differs_counts_the_lines_whose_booking_date_is_not_their_value_date(self):
        # RECORDS: 1 has a book date a day after its value date, 2 has none, 3 has one 1 day after
        _, counts = bb.plan(RECORDS, [row("BT-1", "1"), row("BT-2", "2"), row("BT-3", "3"), row("BT-9", "99")])
        self.assertEqual(counts["differs"], 2)
        self.assertIn("booking date differs from value date: 2", bb.summary(counts))

    def test_the_summary_names_the_field_it_counts(self):
        _, counts = bb.plan(RECORDS, [row("BT-1", "1", None, None)], "value_date")
        self.assertIn("value date already set: 0", bb.summary(counts, "value_date"))


class WriteTest(unittest.TestCase):
    def test_the_dates_go_in_batches_and_the_filled_counts_add_up(self):
        dates = {"BT-{:04d}".format(i): "2026-03-31" for i in range(bb.BATCH + 5)}
        erp = mock.Mock()
        erp.call.side_effect = lambda method, dates, field: len(json.loads(dates))
        self.assertEqual(bb.write(erp, dates), bb.BATCH + 5)
        self.assertEqual(erp.call.call_count, 2)
        self.assertEqual(erp.call.call_args.args[0], "bi_finance.bank_list.set_booking_dates")
        self.assertEqual(erp.call.call_args.kwargs["field"], "booking_date")

    def test_the_field_goes_to_erpnext_with_the_dates(self):
        erp = mock.Mock()
        erp.call.side_effect = lambda method, dates, field: len(json.loads(dates))
        self.assertEqual(bb.write(erp, {"BT-1": "2026-03-30"}, "value_date"), 1)
        self.assertEqual(erp.call.call_args.kwargs["field"], "value_date")


if __name__ == "__main__":
    unittest.main()
