"""Frappe-side tests of the PayPal sync: where a run starts. Invented settings only, no site data.

The module imports frappe, so run it in the image, as test_wise is run (see test_wise.py for the command):

    ... sh -c 'cd /home/frappe/bi_finance_src && ../frappe-bench/env/bin/python -m unittest -v bi_finance.test_paypal_sync'
"""

import datetime
import types
import unittest

from bi_finance import paypal, paypal_client

NOW = datetime.datetime(2026, 10, 10, 9, 0)
EARLIEST = NOW - datetime.timedelta(days=paypal_client.HISTORY_DAYS)


def _settings(last_sync=None, backfill_from=datetime.date(2015, 1, 1)):
    return types.SimpleNamespace(last_sync=last_sync, backfill_from=backfill_from)


class Start(unittest.TestCase):
    def test_the_first_run_reads_from_the_backfill_date(self):
        self.assertEqual(paypal._start(_settings(backfill_from=datetime.date(2025, 6, 1)), NOW), datetime.datetime(2025, 6, 1))

    def test_a_backfill_older_than_paypal_history_starts_at_its_limit(self):
        self.assertEqual(paypal._start(_settings(backfill_from=datetime.date(2015, 1, 1)), NOW), EARLIEST)

    def test_a_later_run_reads_from_a_week_before_the_last_sync(self):
        last = datetime.datetime(2026, 10, 9, 8, 0)
        self.assertEqual(paypal._start(_settings(last_sync=last), NOW), last - paypal.OVERLAP)

    def test_a_last_sync_older_than_paypal_history_starts_at_its_limit(self):
        last = datetime.datetime(2023, 1, 1)
        self.assertEqual(paypal._start(_settings(last_sync=last), NOW), EARLIEST)


if __name__ == "__main__":
    unittest.main()
