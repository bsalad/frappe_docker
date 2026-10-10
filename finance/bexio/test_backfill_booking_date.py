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


def row(name, bexio_id, booking_date=None):
    return {"name": name, "bexio_id": bexio_id, "booking_date": booking_date}


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


class WriteTest(unittest.TestCase):
    def test_the_dates_go_in_batches_and_the_filled_counts_add_up(self):
        dates = {"BT-{:04d}".format(i): "2026-03-31" for i in range(bb.BATCH + 5)}
        erp = mock.Mock()
        erp.call.side_effect = lambda method, dates: len(json.loads(dates))
        self.assertEqual(bb.write(erp, dates), bb.BATCH + 5)
        self.assertEqual(erp.call.call_count, 2)
        self.assertEqual(erp.call.call_args.args[0], "bi_finance.bank_list.set_booking_dates")


if __name__ == "__main__":
    unittest.main()
