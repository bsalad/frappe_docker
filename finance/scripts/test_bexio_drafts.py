"""Offline tests for bexio-drafts.py: the upsert core, against a fake store. No site, no network.

Run with: python3 -m unittest discover -s finance/scripts -p 'test_*.py'
"""

import importlib.util
import os
import unittest

_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bexio-drafts.py")
_spec = importlib.util.spec_from_file_location("bexio_drafts", _path)
loader = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loader)


class FakeStore:
    """Documents by (doctype, name), each with its bexio_id and docstatus; the calls the loader makes, recorded."""

    def __init__(self, docs=None, fail=None):
        self.docs = {}              # (doctype, name) -> {"bexio_id", "docstatus", ...fields}
        self.rates = []
        self.fail = fail or set()   # bexio ids whose insert raises
        self.submit_fail = set()    # bexio ids whose submit raises
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        for doctype, name, fields in docs or []:
            self.docs[(doctype, name)] = dict(fields)

    def find(self, doctype, bexio_id):
        for (dt, name), fields in self.docs.items():
            if dt == doctype and fields.get("bexio_id") == bexio_id:
                return name, fields["docstatus"]
        return None

    def exists(self, doctype, name):
        return (doctype, name) in self.docs

    def values(self, doctype, name):
        return dict(self.docs[(doctype, name)])

    def insert(self, doctype, name, values):
        self.calls.append(("insert", doctype, name))
        if values.get("bexio_id") in self.fail:
            raise RuntimeError("the database said no")
        if name is None:  # no name: ERPNext's series names it
            name = "ACC-PAY-{:04d}".format(sum(1 for dt, _ in self.docs if dt == doctype) + 1)
        self.docs[(doctype, name)] = dict(values, docstatus=0)
        return name

    def update(self, doctype, name, values):
        self.calls.append(("update", doctype, name))
        self.docs[(doctype, name)].update(values)

    def submit(self, doctype, name):
        self.calls.append(("submit", doctype, name))
        if self.docs[(doctype, name)].get("bexio_id") in self.submit_fail:
            raise RuntimeError("validation failed on submit")
        self.docs[(doctype, name)]["docstatus"] = 1

    def rate_exists(self, rate):
        return any(r == rate for r in self.rates)

    def insert_rate(self, rate):
        self.rates.append(rate)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def invoice(bexio_id="500", name="RE-1001", **changes):
    values = {"customer": "Beispiel AG", "posting_date": "2024-03-01", "currency": "CHF", "conversion_rate": 1.0,
              "bexio_id": bexio_id, "remarks": "bexio Nr. RE-1001", "set_posting_time": 1,
              "items": [{"item_code": "BEISPIEL-DIENST", "qty": 2.0, "rate": 50.0, "amount": 100.0}],
              "taxes": [{"account_head": "2200 - USt - bic", "tax_amount": 8.1, "description": "USt 8.1%"}]}
    values.update(changes)
    return {"doctype": "Sales Invoice", "name": name, "bexio_id": bexio_id, "values": values}


def plan(*documents, rates=()):
    return {"documents": list(documents), "exchange_rates": list(rates)}


class Naming(unittest.TestCase):
    def test_a_new_draft_is_inserted_under_bexios_number(self):
        store = FakeStore()
        counts, failures = loader.apply_plan(plan(invoice()), store)
        self.assertEqual(store.calls, [("insert", "Sales Invoice", "RE-1001")])
        self.assertEqual(counts[("Sales Invoice", "created")], 1)
        self.assertEqual(failures, [])

    def test_a_name_another_document_holds_is_a_conflict_and_left_alone(self):
        store = FakeStore([("Sales Invoice", "RE-1001", {"bexio_id": "999", "docstatus": 0, "customer": "Anderer"})])
        counts, _ = loader.apply_plan(plan(invoice()), store)
        self.assertEqual(counts[("Sales Invoice", "conflict")], 1)
        self.assertEqual(store.docs[("Sales Invoice", "RE-1001")]["customer"], "Anderer")
        self.assertEqual(store.calls, [])

    def test_bexio_attachment_ids_are_dropped_before_the_insert(self):
        store = FakeStore()
        loader.apply_plan({"documents": [{"doctype": "Purchase Invoice", "name": "R-1", "bexio_id": "b-1",
                                          "values": {"supplier": "Lieferant", "bexio_attachment_ids": "a-1"}}],
                           "exchange_rates": []}, store)
        self.assertNotIn("bexio_attachment_ids", store.docs[("Purchase Invoice", "R-1")])


class Upsert(unittest.TestCase):
    def test_a_rerun_changes_nothing(self):
        store = FakeStore()
        loader.apply_plan(plan(invoice()), store)
        counts, _ = loader.apply_plan(plan(invoice()), store)
        self.assertEqual(counts[("Sales Invoice", "unchanged")], 1)
        self.assertEqual(store.calls, [("insert", "Sales Invoice", "RE-1001")])

    def test_a_changed_draft_is_updated_in_place_and_never_duplicated(self):
        store = FakeStore()
        loader.apply_plan(plan(invoice()), store)
        counts, _ = loader.apply_plan(plan(invoice(items=[{"item_code": "BEISPIEL-DIENST", "qty": 3.0, "rate": 50.0,
                                                           "amount": 150.0}])), store)
        self.assertEqual(counts[("Sales Invoice", "updated")], 1)
        self.assertEqual(store.calls[-1], ("update", "Sales Invoice", "RE-1001"))
        self.assertEqual([d for (dt, _), d in store.docs.items() if dt == "Sales Invoice"][0]["items"][0]["qty"], 3.0)
        self.assertEqual(len(store.docs), 1)

    def test_a_submitted_document_is_skipped_and_counted(self):
        store = FakeStore([("Sales Invoice", "RE-1001", {"bexio_id": "500", "docstatus": 1, "customer": "Beispiel AG"})])
        counts, _ = loader.apply_plan(plan(invoice(customer="Neu")), store)
        self.assertEqual(counts[("Sales Invoice", "skipped")], 1)
        self.assertEqual(store.docs[("Sales Invoice", "RE-1001")]["customer"], "Beispiel AG")
        self.assertEqual(store.calls, [])

    def test_a_number_difference_within_float_noise_is_no_change(self):
        store = FakeStore()
        loader.apply_plan(plan(invoice(conversion_rate=0.8774700000001)), store)
        counts, _ = loader.apply_plan(plan(invoice(conversion_rate=0.87747)), store)
        self.assertEqual(counts[("Sales Invoice", "unchanged")], 1)


class Failures(unittest.TestCase):
    def test_one_failing_document_is_rolled_back_and_the_rest_still_load(self):
        store = FakeStore(fail={"501"})
        counts, failures = loader.apply_plan(plan(invoice("500", "RE-1001"), invoice("501", "RE-1002"),
                                                  invoice("502", "RE-1003")), store)
        self.assertEqual(counts[("Sales Invoice", "created")], 2)
        self.assertEqual(counts[("Sales Invoice", "failed")], 1)
        self.assertEqual(store.rollbacks, 1)
        self.assertEqual([(dt, bid) for dt, bid, _ in failures], [("Sales Invoice", "501")])
        self.assertEqual(store.commits, 2)


class Submit(unittest.TestCase):
    def test_each_new_draft_is_submitted_after_it_is_loaded(self):
        store = FakeStore()
        counts, failures = loader.apply_plan(plan(invoice("500", "RE-1001")), store, submit=True)
        self.assertEqual(store.calls, [("insert", "Sales Invoice", "RE-1001"), ("submit", "Sales Invoice", "RE-1001")])
        self.assertEqual(store.docs[("Sales Invoice", "RE-1001")]["docstatus"], 1)
        self.assertEqual(counts[("Sales Invoice", "created")], 1)
        self.assertEqual(counts[("Sales Invoice", "submitted")], 1)
        self.assertEqual(failures, [])

    def test_a_draft_loaded_earlier_is_submitted_in_place(self):
        store = FakeStore()
        loader.apply_plan(plan(invoice()), store)
        counts, _ = loader.apply_plan(plan(invoice()), store, submit=True)
        self.assertEqual(counts[("Sales Invoice", "unchanged")], 1)
        self.assertEqual(counts[("Sales Invoice", "submitted")], 1)
        self.assertEqual(store.calls[-1], ("submit", "Sales Invoice", "RE-1001"))
        self.assertEqual(len(store.docs), 1)

    def test_a_second_run_submits_nothing(self):
        store = FakeStore()
        loader.apply_plan(plan(invoice()), store, submit=True)
        store.calls = []
        counts, _ = loader.apply_plan(plan(invoice()), store, submit=True)
        self.assertEqual(counts[("Sales Invoice", "skipped")], 1)
        self.assertEqual(counts.get(("Sales Invoice", "submitted"), 0), 0)
        self.assertEqual(store.calls, [])

    def test_drafts_are_submitted_in_posting_date_order(self):
        store = FakeStore()
        late = invoice("501", "RE-1002", posting_date="2024-05-01")
        early = invoice("500", "RE-1001", posting_date="2024-03-01")
        loader.apply_plan(plan(late, early), store, submit=True)
        submits = [name for call, _, name in store.calls if call == "submit"]
        self.assertEqual(submits, ["RE-1001", "RE-1002"])

    def test_a_document_the_plan_keeps_stays_a_draft_and_is_counted(self):
        store = FakeStore()
        counts, _ = loader.apply_plan(dict(plan(invoice("500", "RE-1001"), invoice("501", "RE-1002")),
                                           keep_draft=["501"]), store, submit=True)
        self.assertEqual(store.docs[("Sales Invoice", "RE-1002")]["docstatus"], 0)
        self.assertEqual(store.docs[("Sales Invoice", "RE-1001")]["docstatus"], 1)
        self.assertEqual(counts[("Sales Invoice", "kept draft")], 1)
        self.assertEqual(counts[("Sales Invoice", "submitted")], 1)

    def test_a_draft_that_fails_on_submit_is_rolled_back_and_listed(self):
        store = FakeStore()
        store.submit_fail = {"500"}
        counts, failures = loader.apply_plan(plan(invoice("500", "RE-1001"), invoice("501", "RE-1002")),
                                             store, submit=True)
        self.assertEqual(counts[("Sales Invoice", "failed")], 1)
        self.assertEqual(counts[("Sales Invoice", "submitted")], 1)
        self.assertEqual(counts[("Sales Invoice", "created")], 1)  # the rolled-back one is not also "created"
        self.assertEqual(store.rollbacks, 1)
        self.assertEqual([(dt, bid) for dt, bid, _ in failures], [("Sales Invoice", "500")])
        self.assertEqual(store.docs[("Sales Invoice", "RE-1002")]["docstatus"], 1)

    def test_without_submit_nothing_is_submitted(self):
        store = FakeStore()
        loader.apply_plan(plan(invoice()), store)
        self.assertNotIn("submit", [call for call, _, _ in store.calls])


class SeriesNames(unittest.TestCase):
    """A Payment Entry has no name of bexio's: ERPNext's series names it, and the upsert is by bexio_id alone."""

    def payment(self, bexio_id="811"):
        return {"doctype": "Payment Entry", "name": None, "bexio_id": bexio_id,
                "values": {"payment_type": "Receive", "bexio_id": bexio_id, "posting_date": "2026-01-20"}}

    def test_a_payment_is_inserted_under_the_series_and_a_rerun_changes_nothing(self):
        store = FakeStore()
        counts, failures = loader.apply_plan(plan(self.payment()), store)
        self.assertEqual(store.calls, [("insert", "Payment Entry", None)])
        self.assertEqual(counts[("Payment Entry", "created")], 1)
        self.assertEqual(failures, [])
        counts, _ = loader.apply_plan(plan(self.payment()), store)
        self.assertEqual(counts[("Payment Entry", "unchanged")], 1)
        self.assertEqual(len(store.docs), 1)

    def test_a_payment_is_submitted_under_the_name_the_series_gave_it(self):
        store = FakeStore()
        counts, _ = loader.apply_plan(plan(self.payment()), store, submit=True)
        self.assertEqual(store.calls, [("insert", "Payment Entry", None), ("submit", "Payment Entry", "ACC-PAY-0001")])
        self.assertEqual(counts[("Payment Entry", "submitted")], 1)
        counts, _ = loader.apply_plan(plan(self.payment()), store, submit=True)
        self.assertEqual(counts[("Payment Entry", "skipped")], 1)
        self.assertEqual(len(store.calls), 2)


class Rates(unittest.TestCase):
    def test_an_exchange_rate_is_inserted_once(self):
        store = FakeStore()
        rate = {"date": "2024-08-01", "from": "USD", "to": "CHF", "rate": 0.87747}
        counts, _ = loader.apply_plan(plan(rates=[rate]), store)
        self.assertEqual(counts["exchange rate created"], 1)
        counts, _ = loader.apply_plan(plan(rates=[rate]), store)
        self.assertEqual(counts["exchange rate unchanged"], 1)
        self.assertEqual(len(store.rates), 1)


class Differs(unittest.TestCase):
    def test_numbers_text_and_empty_compare_as_the_database_writes_them(self):
        self.assertFalse(loader.differs({"a": 1, "b": "x", "c": None}, {"a": 1.0, "b": "x", "c": ""}))
        self.assertTrue(loader.differs({"a": 1}, {"a": 2}))

    def test_erpnext_line_breaks_and_line_endings_are_the_same_text(self):
        self.assertFalse(loader.differs({"terms": "Hallo<br />Welt\r\nDank"}, {"terms": "Hallo<br>Welt\nDank"}))

    def test_an_entity_for_a_letter_is_the_same_letter(self):
        self.assertFalse(loader.differs({"terms": "Grüsse &uuml;"}, {"terms": "Gr&uuml;sse ü"}))

    def test_a_table_compares_row_by_row_in_order(self):
        have = {"items": [{"qty": 1.0, "name": "row-1"}]}
        self.assertFalse(loader.differs(have, {"items": [{"qty": 1}]}))
        self.assertTrue(loader.differs(have, {"items": [{"qty": 1}, {"qty": 2}]}))


if __name__ == "__main__":
    unittest.main()
