"""Offline tests for import_manual_entries.py. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

import import_manual_entries as ime
import import_master as im
import import_purchase as ip

CURRENCIES = {"1": "CHF", "2": "EUR"}
# the accounts by number: the reverse-charge pair, and the VAT accounts a code's kind books to (invented names)
DEFAULT_BY_NUMBER = {
    "1170": ("1170 - Vorsteuer Test - bic", "Asset"), "1171": ("1171 - Vorsteuer Invest Test - bic", "Asset"),
    "2200": ("2200 - Umsatzsteuer Test - bic", "Liability"), "2203": ("2203 - Bezugsteuer Test - bic", "Liability"),
}


def lookups(taxes=None, by_number=None, currencies=None, depreciation=None, party=None, receivable=None):
    return ime.Lookups(
        currencies=currencies,
        accounts={
            "11": ("1020 - Bank Test - bic", "Asset"),
            "12": ("1100 - Debitoren Test - bic", "Asset"),
            "21": ("5001 - Testaufwand - bic", "Expense"),
            "22": ("1170 - Vorsteuer Test - bic", "Asset"),
            "31": ("2200 - Umsatzsteuer Test - bic", "Liability"),
            "32": ("3000 - Testertrag - bic", "Income"),
            "41": ("9100 - Eroeffnung Test - bic", "Equity"),
            "61": ("6820 - Abschreibung Test - bic", "Expense"),
            "99": ("9999 - Ohne Wurzel - bic", "Stock"),
        },
        taxes={"35": "Test MWST bexio 35", "22": "Test MWST bexio 22", "16": "Test MWST bexio 16", "38": "Test MWST bexio 38"}
        if taxes is None else dict(taxes),
        by_number=DEFAULT_BY_NUMBER if by_number is None else dict(by_number),
        depreciation=depreciation, party=party, receivable=receivable,
    )


def line(debit, credit, amount, description="Testbuchung", **extra):
    # a row with an id, as every row of a single, group or compound entry has except a compound's counterpart
    return dict({"id": 1, "debit_account_id": debit, "credit_account_id": credit, "amount": amount,
                 "description": description, "currency_id": 1, "currency_factor": 1, "tax_id": None,
                 "tax_account_id": None}, **extra)


def entry(eid="m-1", kind="manual_single_entry", lines=None, **extra):
    return dict({"id": eid, "type": kind, "date": "2025-06-30", "reference_nr": "BU-1",
                 "entries": lines if lines is not None else [line(21, 11, 100)]}, **extra)


def counterpart(debit, credit, amount, description="Gegenkonto"):
    """A compound entry's counterpart row: no id, the account every id row of the entry is booked against."""
    return dict(line(debit, credit, amount, description), id=None)


def side_totals(doc):
    debit = sum(Decimal(str(r.get("debit", 0))) for r in doc["accounts"])
    credit = sum(Decimal(str(r.get("credit", 0))) for r in doc["accounts"])
    return debit, credit


def rows_of(doc):
    """(account, debit, credit) per row, as floats, for comparing with the expected rows."""
    return [(r["account"], r.get("debit", 0.0), r.get("credit", 0.0)) for r in doc["accounts"]]


class MapEntryTest(unittest.TestCase):
    def test_single_entry_without_vat_is_one_debit_and_one_credit(self):
        doc = ime.map_entry(entry(), lookups(), CURRENCIES)
        self.assertEqual(doc["doctype"], "Journal Entry")
        self.assertEqual(doc["posting_date"], "2025-06-30")
        self.assertEqual(doc["bexio_id"], "manual-m-1")
        self.assertEqual(rows_of(doc), [("5001 - Testaufwand - bic", 100.0, 0.0), ("1020 - Bank Test - bic", 0.0, 100.0)])
        self.assertEqual(side_totals(doc), (Decimal("100"), Decimal("100")))
        self.assertEqual(doc["multi_currency"], 0)

    def test_bexio_key_is_prefixed_so_it_cannot_meet_a_journal_line_id(self):
        self.assertEqual(ime.manual_key(153), "manual-153")

    def test_entry_on_a_depreciation_account_is_a_depreciation_entry(self):
        doc = ime.map_entry(entry(lines=[line(61, 11, 100)]), lookups(depreciation={"6820 - Abschreibung Test - bic"}), CURRENCIES)
        self.assertEqual(doc["voucher_type"], "Depreciation Entry")
        self.assertEqual(doc["doctype"], "Journal Entry")

    def test_entry_without_a_depreciation_account_is_a_journal_entry(self):
        self.assertEqual(ime.map_entry(entry(), lookups(depreciation={"6820 - Abschreibung Test - bic"}), CURRENCIES)["voucher_type"], "Journal Entry")

    def test_zero_rate_code_of_the_sales_table_has_no_split(self):
        doc = ime.map_entry(entry(lines=[line(11, 32, 100, tax_id=3, tax_account_id=31)]), lookups(), CURRENCIES)
        self.assertEqual(rows_of(doc), [("1020 - Bank Test - bic", 100.0, 0.0), ("3000 - Testertrag - bic", 0.0, 100.0)])
        self.assertIn(3, ime.MANUAL_ZERO_RATE_IDS)

    def test_reference_goes_to_cheque_number_and_remark(self):
        doc = ime.map_entry(entry(reference_nr="BU-77"), lookups(), CURRENCIES)
        self.assertEqual((doc["cheque_no"], doc["user_remark"]), ("BU-77", "BU-77"))
        # ERPNext refuses a reference number without a reference date, so the date is the entry's own
        self.assertEqual(doc["cheque_date"], "2025-06-30")

    def test_entry_without_reference_has_no_reference_date(self):
        doc = ime.map_entry(entry(reference_nr=""), lookups(), CURRENCIES)
        self.assertIsNone(doc["cheque_date"])

    def test_compound_entry_puts_every_line_in_one_journal_entry(self):
        lines = [line(None, 11, 60, "Teil 1"), line(None, 12, 40, "Teil 2"), counterpart(21, None, 100)]
        doc = ime.map_entry(entry(kind="manual_compound_entry", lines=lines), lookups(), CURRENCIES)
        self.assertEqual(len(doc["accounts"]), 4)
        self.assertEqual([r["user_remark"] for r in doc["accounts"]], ["Teil 1", "Teil 1", "Teil 2", "Teil 2"])
        self.assertEqual(side_totals(doc), (Decimal("100"), Decimal("100")))

    def test_group_entry_maps_like_a_compound_one(self):
        lines = [line(21, 11, 10), line(21, 11, 20), line(21, 11, 30)]
        doc = ime.map_entry(entry(kind="manual_group_entry", lines=lines), lookups(), CURRENCIES)
        self.assertEqual(len(doc["accounts"]), 6)
        self.assertEqual(side_totals(doc), (Decimal("60"), Decimal("60")))

    def test_purchase_vat_is_split_off_the_debit_side(self):
        # 108.10 incl. 8.1 %: net 100.00 on the expense, tax 8.10 on Vorsteuer (asset, debited), 108.10 credited to the bank
        doc = ime.map_entry(entry(lines=[line(21, 11, 108.10, tax_id=35, tax_account_id=21)]), lookups(), CURRENCIES)
        self.assertEqual(rows_of(doc), [
            ("5001 - Testaufwand - bic", 100.0, 0.0),
            ("1170 - Vorsteuer Test - bic", 8.1, 0.0),
            ("1020 - Bank Test - bic", 0.0, 108.1),
        ])
        self.assertEqual(side_totals(doc), (Decimal("108.10"), Decimal("108.10")))

    def test_purchase_vat_of_an_investment_code_is_on_1171(self):
        # 6570 against 2010 at 8.1 % (invented numbers): net on the expense, VAT on 1171, gross on the bank
        doc = ime.map_entry(entry(lines=[line(21, 11, 108.10, tax_id=38, tax_account_id=21)]), lookups(), CURRENCIES)
        self.assertEqual(rows_of(doc), [
            ("5001 - Testaufwand - bic", 100.0, 0.0),
            ("1171 - Vorsteuer Invest Test - bic", 8.1, 0.0),
            ("1020 - Bank Test - bic", 0.0, 108.1),
        ])

    def test_each_kind_of_code_books_to_its_own_account(self):
        self.assertEqual((ime.VAT_NUMBER_OF_CODE[16], ime.VAT_NUMBER_OF_CODE[22], ime.VAT_NUMBER_OF_CODE[24]), ("2200", "1170", "1171"))
        # every code that carries VAT in import_master's table has its account; no 0 % code is in it
        self.assertEqual(set(ime.VAT_NUMBER_OF_CODE), set(im.VAT_OF_TAX_ID))
        self.assertFalse(set(ime.VAT_NUMBER_OF_CODE) & set(ime.MANUAL_ZERO_RATE_IDS))

    def test_sales_vat_is_split_off_the_credit_side(self):
        # 107.70 incl. 7.7 %: receivable debited 107.70, income 100.00 and Umsatzsteuer 7.70 credited
        doc = ime.map_entry(entry(lines=[line(12, 32, 107.70, tax_id=16, tax_account_id=32)]), lookups(), CURRENCIES)
        self.assertEqual(rows_of(doc), [
            ("1100 - Debitoren Test - bic", 107.7, 0.0),
            ("3000 - Testertrag - bic", 0.0, 100.0),
            ("2200 - Umsatzsteuer Test - bic", 0.0, 7.7),
        ])
        self.assertEqual(side_totals(doc), (Decimal("107.70"), Decimal("107.70")))

    def test_sales_reversal_debits_the_umsatzsteuer(self):
        # credit note: income debited 100.00, Umsatzsteuer debited 7.70, receivable credited 107.70
        doc = ime.map_entry(entry(lines=[line(32, 12, 107.70, tax_id=16, tax_account_id=32)]), lookups(), CURRENCIES)
        self.assertEqual(rows_of(doc), [
            ("3000 - Testertrag - bic", 100.0, 0.0),
            ("2200 - Umsatzsteuer Test - bic", 7.7, 0.0),
            ("1100 - Debitoren Test - bic", 0.0, 107.7),
        ])

    def test_purchase_refund_credits_the_vorsteuer(self):
        doc = ime.map_entry(entry(lines=[line(11, 21, 108.10, tax_id=35, tax_account_id=21)]), lookups(), CURRENCIES)
        self.assertEqual(rows_of(doc), [
            ("1020 - Bank Test - bic", 108.1, 0.0),
            ("5001 - Testaufwand - bic", 0.0, 100.0),
            ("1170 - Vorsteuer Test - bic", 0.0, 8.1),
        ])

    def test_vat_rows_add_up_to_the_gross_on_odd_amounts(self):
        # 0.33 at 8.1 %: the net is rounded, the tax is the rest, so the gross is kept to the rappen
        doc = ime.map_entry(entry(lines=[line(21, 11, 0.33, tax_id=35, tax_account_id=21)]), lookups(), CURRENCIES)
        self.assertEqual(side_totals(doc), (Decimal("0.33"), Decimal("0.33")))
        self.assertEqual(round(doc["accounts"][0]["debit"] + doc["accounts"][1]["debit"], 2), 0.33)

    def test_zero_rate_code_has_no_split(self):
        doc = ime.map_entry(entry(lines=[line(21, 11, 30, tax_id=47)]), lookups(), CURRENCIES)
        self.assertEqual(len(doc["accounts"]), 2)

    def test_foreign_currency_keeps_the_amount_and_the_rate(self):
        # 100 EUR at 0.93: 93.00 CHF, the EUR amount in the account-currency column
        lines = [line(21, 11, 100, currency_id=2, currency_factor=0.93)]
        doc = ime.map_entry(entry(lines=lines), lookups(), CURRENCIES)
        debit = doc["accounts"][0]
        self.assertEqual((debit["debit"], debit["debit_in_account_currency"], debit["exchange_rate"]), (93.0, 100.0, 0.93))
        self.assertEqual(doc["multi_currency"], 1)
        self.assertEqual(side_totals(doc), (Decimal("93.00"), Decimal("93.00")))

    def test_foreign_currency_on_a_chf_account_takes_the_chf_amount(self):
        # bexio: 100 EUR at 0.93 = 93.00 CHF. The CHF bank account must get 93.00 at rate 1, or ERPNext books 100.00 as CHF
        chf = {"1020 - Bank Test - bic": "CHF", "5001 - Testaufwand - bic": "CHF"}
        lines = [line(21, 11, 100, currency_id=2, currency_factor=0.93)]
        doc = ime.map_entry(entry(lines=lines), lookups(currencies=chf), CURRENCIES)
        self.assertEqual([(r["account"], r["debit_in_account_currency"], r["exchange_rate"]) for r in doc["accounts"] if "debit_in_account_currency" in r],
                         [("5001 - Testaufwand - bic", 93.0, 1.0)])
        self.assertEqual(rows_of(doc), [("5001 - Testaufwand - bic", 93.0, 0.0), ("1020 - Bank Test - bic", 0.0, 93.0)])
        self.assertEqual(doc["multi_currency"], 0)

    def test_account_in_the_line_currency_keeps_the_amount_and_rate(self):
        eur = {"1020 - Bank Test - bic": "EUR", "5001 - Testaufwand - bic": "EUR"}
        doc = ime.map_entry(entry(lines=[line(21, 11, 100, currency_id=2, currency_factor=0.93)]), lookups(currencies=eur), CURRENCIES)
        self.assertEqual((doc["accounts"][0]["debit"], doc["accounts"][0]["debit_in_account_currency"], doc["accounts"][0]["exchange_rate"]), (93.0, 100.0, 0.93))

    def test_account_in_another_currency_is_not_mapped(self):
        usd = {"1020 - Bank Test - bic": "USD", "5001 - Testaufwand - bic": "CHF"}
        with self.assertRaisesRegex(ip.MappingError, "a EUR line on an account in USD"):
            ime.map_entry(entry(lines=[line(21, 11, 100, currency_id=2, currency_factor=0.93)]), lookups(currencies=usd), CURRENCIES)

    def test_foreign_currency_with_vat_balances_in_chf(self):
        lines = [line(21, 11, 108.10, currency_id=2, currency_factor=0.93, tax_id=35, tax_account_id=21)]
        doc = ime.map_entry(entry(lines=lines), lookups(), CURRENCIES)
        self.assertEqual(side_totals(doc), (Decimal("100.53"), Decimal("100.53")))

    def test_untyped_entry_is_booked_as_a_single_one(self):
        doc = ime.map_entry(entry(kind=None), lookups(), CURRENCIES)
        self.assertEqual(rows_of(doc), [("5001 - Testaufwand - bic", 100.0, 0.0), ("1020 - Bank Test - bic", 0.0, 100.0)])

    def test_compound_row_with_vat_on_its_own_credit_takes_the_expense_from_the_counterpart(self):
        # the id row is paid from account 11 (its own account, named as the tax account); the expense is the counterpart
        # on 21, so the net goes there and the VAT is split off it, as bexio's journal books it
        lines = [line(None, 11, 108.10, "Teil", tax_id=35, tax_account_id=11), counterpart(21, None, 108.10, "Gegenkonto")]
        doc = ime.map_entry(entry(kind="manual_compound_entry", lines=lines), lookups(), CURRENCIES)
        self.assertEqual(rows_of(doc), [
            ("5001 - Testaufwand - bic", 100.0, 0.0),
            ("1170 - Vorsteuer Test - bic", 8.1, 0.0),
            ("1020 - Bank Test - bic", 0.0, 108.1),
        ])

    def test_compound_counterpart_is_not_posted_and_completes_each_row(self):
        first, second = dict(line(None, 11, 60, "Teil 1"), id=1), dict(line(None, 12, 40, "Teil 2"), id=2)
        doc = ime.map_entry(entry(kind="manual_compound_entry", lines=[first, counterpart(21, None, 100, "Gegenkonto"), second]), lookups(), CURRENCIES)
        self.assertEqual(rows_of(doc), [
            ("5001 - Testaufwand - bic", 60.0, 0.0), ("1020 - Bank Test - bic", 0.0, 60.0),
            ("5001 - Testaufwand - bic", 40.0, 0.0), ("1100 - Debitoren Test - bic", 0.0, 40.0),
        ])
        self.assertEqual(side_totals(doc), (Decimal("100"), Decimal("100")))

    def test_reverse_charge_is_net_with_the_tax_on_vorsteuer_and_bezugsteuer(self):
        # 100.00 net at 8.1 %: the expense and the bank are 100.00, and the tax 8.10 goes to Vorsteuer and back on Bezugsteuer
        doc = ime.map_entry(entry(lines=[line(21, 11, 100, tax_id=32, tax_account_id=21)]), lookups(), CURRENCIES)
        self.assertEqual(rows_of(doc), [
            ("5001 - Testaufwand - bic", 100.0, 0.0),
            ("1171 - Vorsteuer Invest Test - bic", 8.1, 0.0),
            ("1020 - Bank Test - bic", 0.0, 100.0),
            ("2203 - Bezugsteuer Test - bic", 0.0, 8.1),
        ])
        self.assertEqual(side_totals(doc), (Decimal("108.10"), Decimal("108.10")))

    def test_reversed_reverse_charge_takes_the_tax_back_the_other_way(self):
        # a refund of 100.00 net at 8.1 %: the expense is credited, and the tax is debited on 2203 and credited on Vorsteuer
        doc = ime.map_entry(entry(lines=[line(11, 21, 100, tax_id=32, tax_account_id=21)]), lookups(), CURRENCIES)
        self.assertEqual(rows_of(doc), [
            ("1020 - Bank Test - bic", 100.0, 0.0),
            ("2203 - Bezugsteuer Test - bic", 8.1, 0.0),
            ("5001 - Testaufwand - bic", 0.0, 100.0),
            ("1171 - Vorsteuer Invest Test - bic", 0.0, 8.1),
        ])
        self.assertEqual(side_totals(doc), (Decimal("108.10"), Decimal("108.10")))

    def test_banking_entry_is_mapped_and_is_the_same_shape(self):
        doc = ime.map_entry(entry(kind=ime.BANKING), lookups(), CURRENCIES)
        self.assertEqual(len(doc["accounts"]), 2)


class MapEntryErrorTest(unittest.TestCase):
    def assertUnmapped(self, record, words, known=None):
        with self.assertRaisesRegex(ip.MappingError, words):
            ime.map_entry(record, known or lookups(), CURRENCIES)

    def test_unknown_type_is_reported(self):
        self.assertUnmapped(entry(kind="manual_other_entry"), "unknown entry type 'manual_other_entry'")

    def test_row_without_id_outside_a_compound_entry_is_reported(self):
        self.assertUnmapped(entry(kind="manual_group_entry", lines=[line(21, 11, 50), counterpart(21, None, 50)]),
                            "row without id in a manual_group_entry entry")

    def test_entry_without_id_is_reported(self):
        self.assertUnmapped(dict(entry(), id=None), "entry without id")

    def test_compound_entry_needs_exactly_one_counterpart_row(self):
        self.assertUnmapped(entry(kind="manual_compound_entry", lines=[line(None, 11, 100)]), "compound entry with 0 counterpart rows")

    def test_compound_counterpart_without_account_is_reported(self):
        self.assertUnmapped(entry(kind="manual_compound_entry", lines=[line(None, 11, 100), counterpart(None, None, 100)]),
                            "counterpart row without an account")

    def test_reverse_charge_without_its_accounts_is_reported(self):
        self.assertUnmapped(entry(lines=[line(21, 11, 100, tax_id=32)]), "no Account 1171 in ERPNext", known=lookups(by_number={}))

    def test_entry_without_lines_is_reported(self):
        self.assertUnmapped(entry(lines=[]), "no entries lines")

    def test_single_entry_with_two_lines_is_reported(self):
        self.assertUnmapped(entry(lines=[line(21, 11, 1), line(21, 11, 2)]), "single entry with 2 lines")

    def test_unknown_account_is_reported(self):
        self.assertUnmapped(entry(lines=[line(777, 11, 10)]), "no Account for account 777")

    def test_vat_code_without_template_is_reported_not_guessed(self):
        self.assertUnmapped(entry(lines=[line(21, 11, 10, tax_id=35, tax_account_id=21)]),
                            "no Item Tax Template with bexio_id 35", lookups(taxes={}))

    def test_unknown_vat_code_is_reported(self):
        self.assertUnmapped(entry(lines=[line(21, 11, 10, tax_id=99, tax_account_id=21)]), "unknown VAT code 99")

    def test_vat_without_tax_account_is_reported(self):
        self.assertUnmapped(entry(lines=[line(21, 11, 10, tax_id=35)]), "tax account None is not an account of its row")

    def test_tax_account_that_is_not_an_account_of_the_row_is_reported_not_guessed(self):
        # the row names account 12 as its tax account, but is booked on 21 and 11: the VAT would sit on a guess, so the entry is listed
        self.assertUnmapped(entry(lines=[line(21, 11, 108.10, tax_id=35, tax_account_id=12)]), "tax account 12 is not an account of its row")

    def test_vat_account_of_an_unknown_root_is_reported(self):
        known = lookups(by_number=dict(DEFAULT_BY_NUMBER, **{"1170": ("1170 - Vorsteuer Test - bic", "Stock")}))
        self.assertUnmapped(entry(lines=[line(21, 11, 10, tax_id=35, tax_account_id=21)]), "VAT account 1170 has root type Stock", known)

    def test_vat_account_missing_from_erpnext_is_reported(self):
        known = lookups(by_number={k: v for k, v in DEFAULT_BY_NUMBER.items() if k != "1171"})
        self.assertUnmapped(entry(lines=[line(21, 11, 10, tax_id=38, tax_account_id=21)]), "no Account 1171 in ERPNext", known)

    def test_entry_on_a_party_account_is_reported_not_written(self):
        self.assertUnmapped(entry(lines=[line(12, 21, 100)]), "account 1100 - Debitoren Test - bic needs a party",
                            lookups(party={"1100 - Debitoren Test - bic"}))

    def test_unknown_currency_is_reported(self):
        self.assertUnmapped(entry(lines=[line(21, 11, 10, currency_id=9)]), "unknown currency 9")

    def test_bad_date_is_reported(self):
        self.assertUnmapped(entry(date="30.06.2025"), "no valid date")


class DryRunTest(unittest.TestCase):
    def test_totals_per_year_with_banking_counted_apart(self):
        entries = [
            entry("m-1"),
            entry("m-2", kind=ime.BANKING, date="2025-07-01"),
            entry("m-3", date="2026-01-05"),
        ]
        totals = ime.dry_run(entries, lookups(), CURRENCIES)
        self.assertEqual(totals.rows[2025]["entries"], 2)
        self.assertEqual(totals.rows[2025]["banking"], 1)
        self.assertEqual(totals.rows[2025]["debit"], Decimal("200"))
        self.assertEqual(totals.rows[2025]["banking_debit"], Decimal("100"))
        self.assertEqual(totals.rows[2026]["mapped"], 1)
        self.assertEqual(totals.problems, [])

    def test_unmapped_entries_are_listed_by_bexio_id(self):
        totals = ime.dry_run([entry("m-9", lines=[line(777, 11, 10)])], lookups(), CURRENCIES)
        self.assertEqual(totals.rows[2025]["unmapped"], 1)
        self.assertEqual(totals.problems, ["manual entry m-9: unmapped, no Account for account 777"])

    def test_documents_hold_what_maps_and_nothing_else(self):
        totals = ime.dry_run([entry("m-1"), entry("m-9", lines=[line(777, 11, 10)])], lookups(), CURRENCIES)
        self.assertEqual([d["bexio_id"] for d in totals.documents], ["manual-m-1"])
        self.assertEqual(totals.documents[0]["doctype"], "Journal Entry")
        self.assertIsNone(totals.documents[0]["name"])

    def test_entry_with_bad_date_is_counted_under_no_year(self):
        totals = ime.dry_run([entry("m-8", date="x")], lookups(), CURRENCIES)
        self.assertEqual(totals.rows[None]["unmapped"], 1)

    def test_report_prints_totals_only(self):
        totals = ime.dry_run([entry("m-1")], lookups(), CURRENCIES)
        text = ime.report(totals, "/somewhere")
        self.assertIn("nothing was written", text)
        self.assertNotIn("Testaufwand", text)
        self.assertNotIn("m-1", text)


class PartyOverrideTest(unittest.TestCase):
    """D10: an entry on a receivable account books to the Customer its party override names (invented names)."""
    RECEIVABLE = "1100 - Debitoren Test - bic"

    def known(self):
        return lookups(party={self.RECEIVABLE}, receivable={self.RECEIVABLE})

    def test_receivable_row_is_booked_to_the_override_customer(self):
        doc = ime.map_entry(entry("m-1", lines=[line(12, 21, 100)]), self.known(), CURRENCIES, "Kunde Test")
        receivable = [r for r in doc["accounts"] if r["account"] == self.RECEIVABLE]
        self.assertEqual([(r.get("party_type"), r.get("party")) for r in receivable], [("Customer", "Kunde Test")])
        self.assertEqual([r.get("party_type") for r in doc["accounts"] if r["account"] != self.RECEIVABLE], [None])

    def test_receivable_row_without_an_override_is_still_refused(self):
        with self.assertRaisesRegex(ip.MappingError, "needs a party"):
            ime.map_entry(entry("m-1", lines=[line(12, 21, 100)]), self.known(), CURRENCIES)

    def test_override_is_refused_on_a_party_account_that_is_not_receivable(self):
        # a payable account (here 1100 typed as one) takes no Customer: the override covers receivables only
        known = lookups(party={self.RECEIVABLE})
        with self.assertRaisesRegex(ip.MappingError, "needs a party"):
            ime.map_entry(entry("m-1", lines=[line(12, 21, 100)]), known, CURRENCIES, "Kunde Test")

    def test_dry_run_plans_the_customer_ahead_of_its_entry(self):
        totals = ime.dry_run([entry("m-1", lines=[line(12, 21, 100)])], self.known(), CURRENCIES, {"m-1": "Unbekannt Test"})
        self.assertEqual([d["doctype"] for d in totals.documents], ["Customer", "Journal Entry"])
        customer = totals.documents[0]
        self.assertEqual((customer["name"], customer["bexio_id"]), ("Unbekannt Test", "manual-party-m-1"))
        self.assertEqual(customer["values"]["customer_name"], "Unbekannt Test")
        self.assertEqual(customer["values"]["territory"], "Rest Of The World")
        self.assertEqual(totals.documents[1]["bexio_id"], "manual-m-1")
        self.assertEqual(totals.rows[2025]["mapped"], 1)

    def test_dry_run_without_an_override_plans_no_customer(self):
        totals = ime.dry_run([entry("m-1")], self.known(), CURRENCIES, {"m-2": "Unbekannt Test"})
        self.assertEqual([d["doctype"] for d in totals.documents], ["Journal Entry"])

    def test_journal_check_compares_an_overridden_entry_instead_of_leaving_it_unmapped(self):
        known = self.known()
        record = entry("m-1", lines=[line(12, 21, 100)])
        totals = ime.dry_run([record], known, CURRENCIES, {"m-1": "Unbekannt Test"})
        problems, _, compared, won, unmapped = ime.journal_check([record], totals, [journal_line(1, 12, 21, 100)], known)
        self.assertEqual((problems, compared, won, unmapped), ([], 1, 0, 0))


class LoadPartiesTest(unittest.TestCase):
    def write(self, text):
        handle = tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False)
        handle.write(text)
        handle.close()
        self.addCleanup(os.remove, handle.name)
        return handle.name

    def test_missing_file_lists_no_overrides(self):
        self.assertEqual(ime.load_parties("/nonexistent/bexio-manual-parties.txt"), {})

    def test_each_line_is_an_entry_id_and_its_customer_name(self):
        path = self.write("# invented\n\n1151 Unbekannt Test (bexio 1151)\n")
        self.assertEqual(ime.load_parties(path), {"1151": "Unbekannt Test (bexio 1151)"})

    def test_line_without_a_name_is_refused(self):
        path = self.write("1151\n")
        with self.assertRaisesRegex(ValueError, "no Customer name"):
            ime.load_parties(path)


def journal_line(line_id, debit, credit, amount, description="Testbuchung", ref_class="ManualEntry", date="2025-06-30"):
    """A line of bexio's journal, in the shape of journal.json (invented)."""
    return {"id": line_id, "ref_class": ref_class, "ref_id": None, "date": date, "description": description,
            "debit_account_id": debit, "credit_account_id": credit, "base_currency_amount": amount}


class EntryOfLinesTest(unittest.TestCase):
    def test_a_row_line_and_its_vat_line_belong_to_the_entry(self):
        owner = ime.entry_of_lines([entry("m-1", lines=[line(21, 11, 108.10, tax_id=35, tax_account_id=21)])],
                                   [journal_line(1, 21, 11, 108.10), journal_line(2, 22, 21, 8.10)])
        self.assertEqual(owner, {1: "m-1", 2: "m-1"})

    def test_two_vat_lines_of_one_row_text_both_belong_to_the_entry(self):
        # the second line of the same date and text must not be lost: a set read for one claimant must not change it
        owner = ime.entry_of_lines([entry("m-1")], [journal_line(7, 22, 21, 8.10), journal_line(8, 22, 21, 1.00)])
        self.assertEqual(owner, {7: "m-1", 8: "m-1"})

    def test_a_vat_line_two_entries_could_claim_belongs_to_neither(self):
        entries = [entry("m-1"), entry("m-2", lines=[dict(line(21, 11, 100), id=2)])]
        self.assertEqual(ime.entry_of_lines(entries, [journal_line(9, 21, 11, 100)]), {})

    def test_a_vat_line_two_entries_could_claim_goes_to_the_one_whose_row_is_before_it(self):
        # bexio numbers a row's VAT line right after the row: the row 5 of m-2 is before line 6, so line 6 is m-2's
        entries = [entry("m-1"), entry("m-2", lines=[dict(line(21, 11, 100), id=5)])]
        self.assertEqual(ime.entry_of_lines(entries, [journal_line(6, 22, 21, 8.10)]), {6: "m-2"})

    def test_a_vat_line_booked_after_other_rows_goes_to_the_row_whose_gross_it_completes(self):
        # bexio booked m-1's VAT line (id 30) after other rows, so it is not the row id + 1 of its row, and m-2 has a row of the
        # same date and text: the row whose net line plus this VAT line is its gross (120.00 = 111.00 + 9.00) owns it
        entries = [entry("m-1", lines=[line(21, 11, 120.00, description="Sample service", id=10)]),
                   entry("m-2", lines=[line(21, 11, 150.00, description="Sample service", id=20)])]
        journal = [journal_line(10, 21, 11, 111.00, description="Sample service"), journal_line(20, 21, 11, 140.00, description="Sample service"),
                   journal_line(30, 22, 21, 9.00, description="Sample service")]
        self.assertEqual(ime.entry_of_lines(entries, journal), {10: "m-1", 20: "m-2", 30: "m-1"})

    def test_a_vat_line_whose_gross_two_rows_both_complete_belongs_to_neither(self):
        entries = [entry("m-1", lines=[line(21, 11, 120.00, description="Sample service", id=10)]),
                   entry("m-2", lines=[line(21, 11, 120.00, description="Sample service", id=20)])]
        journal = [journal_line(10, 21, 11, 111.00, description="Sample service"), journal_line(20, 21, 11, 111.00, description="Sample service"),
                   journal_line(30, 22, 21, 9.00, description="Sample service")]
        self.assertEqual(ime.entry_of_lines(entries, journal), {10: "m-1", 20: "m-2"})

    def test_document_lines_are_not_an_entrys(self):
        self.assertEqual(ime.entry_of_lines([entry("m-1")], [journal_line(1, 21, 11, 100, ref_class="KbInvoice")]), {})


class JournalCheckTest(unittest.TestCase):
    def check(self, journal, record=None, wins=None):
        known = lookups()
        record = record or entry("m-1", lines=[line(21, 11, 108.10, tax_id=35, tax_account_id=21)])
        totals = ime.dry_run([record], known, CURRENCIES)
        return ime.journal_check([record], totals, journal, known, wins)

    def test_mapping_that_bexio_books_the_same_way_has_no_problem(self):
        problems, per_year, compared, won, unmapped = self.check([journal_line(1, 21, 11, 108.10), journal_line(2, 22, 21, 8.10)])
        self.assertEqual(problems, [])
        self.assertEqual(compared, 1)
        self.assertEqual((won, unmapped), (0, 0))
        # 1170 (VAT) and 5001 (expense, profit and loss) are compared; the bank account is not
        self.assertEqual(per_year["2025"][0:2], [2, 0])

    def test_a_vat_line_bexio_books_differently_is_listed_by_entry_and_account(self):
        # 7.00 instead of 8.10 on 1170: the expense is then 101.10 instead of 100.00, both are listed
        problems, per_year, _, _, _ = self.check([journal_line(1, 21, 11, 108.10), journal_line(2, 22, 21, 7.00)])
        self.assertEqual(problems, ["entry m-1 (2025): 1170 - Vorsteuer Test - bic mapped 8.10 bexio 7.00",
                                    "entry m-1 (2025): 5001 - Testaufwand - bic mapped 100.00 bexio 101.10"])
        self.assertEqual(per_year["2025"][1], 2)

    def test_a_line_bexio_books_beyond_the_mapping_is_listed(self):
        # a further line on the entry's description: the bank and the expense differ from the mapping
        problems, _, _, _, _ = self.check([journal_line(1, 21, 11, 108.10), journal_line(2, 22, 21, 8.10), journal_line(5, 21, 11, 50)])
        self.assertIn("entry m-1 (2025): 5001 - Testaufwand - bic mapped 100.00 bexio 150.00", problems)

    def test_an_entry_on_the_journal_wins_list_is_not_a_difference(self):
        # the same 7.00 as above, but bexio booked the entry differently on purpose: it is a journal win, not listed
        problems, per_year, compared, won, unmapped = self.check([journal_line(1, 21, 11, 108.10), journal_line(2, 22, 21, 7.00)],
                                                                 wins={"m-1": "invented reason"})
        self.assertEqual((problems, per_year, compared, won, unmapped), ([], {}, 0, 1, 0))

    def test_an_entry_not_on_the_journal_wins_list_is_still_a_difference(self):
        problems, _, compared, won, _ = self.check([journal_line(1, 21, 11, 108.10), journal_line(2, 22, 21, 7.00)],
                                                   wins={"m-2": "invented reason"})
        self.assertEqual((len(problems), compared, won), (2, 1, 0))

    def test_an_unmapped_entry_bexio_books_is_counted_apart_not_as_a_difference(self):
        # m-9 has an account with no Account in ERPNext, so the mapping does not hold it: bexio's line for it is unmapped (D10)
        problems, per_year, compared, won, unmapped = self.check([journal_line(1, 777, 11, 100), journal_line(2, 22, 21, 8.10)],
                                                                 record=entry("m-9", lines=[line(777, 11, 100)]))
        self.assertEqual((problems, per_year, compared, won, unmapped), ([], {}, 0, 0, 1))

    def test_extra_entries_are_not_compared(self):
        known = lookups()
        export = [entry("m-1")]
        extra = entry("1099-correction", lines=[line(11, 21, 165.66)])
        totals = ime.dry_run(export + [extra], known, CURRENCIES)
        problems, _, compared, _, _ = ime.journal_check(export, totals, [journal_line(1, 21, 11, 100)], known)
        self.assertEqual((problems, compared), ([], 1))

    def test_report_header_counts_unexplained_journal_wins_and_unmapped(self):
        self.assertIn("1 entries compared, 0 unexplained, 6 journal wins (D11), 1 unmapped (D10), nothing was written",
                      ime.check_report({}, 1, 0, 6, 1))


class LoadEntriesTest(unittest.TestCase):
    def test_no_file_means_not_exported_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ime.load_entries(tmp))

    def test_file_is_read_with_the_currency_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ime.ENTRIES_FILE), "w", encoding="utf-8") as f:
                json.dump([entry()], f)
            with open(os.path.join(tmp, ime.CURRENCIES_FILE), "w", encoding="utf-8") as f:
                json.dump([{"id": 1, "name": "CHF"}, {"id": 2, "name": "EUR"}], f)
            entries, currencies = ime.load_entries(tmp)
        self.assertEqual(entries[0]["id"], "m-1")
        self.assertEqual(currencies, {"1": "CHF", "2": "EUR"})


class MainTest(unittest.TestCase):
    def test_no_mode_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                ime.main(["--export", "/nonexistent"])
        self.assertEqual(cm.exception.code, 2)

    def test_write_puts_the_loader_plan_in_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ime.ENTRIES_FILE), "w", encoding="utf-8") as f:
                json.dump([entry("m-1"), entry("m-9", lines=[line(777, 11, 10)])], f)
            with open(os.path.join(tmp, ime.CURRENCIES_FILE), "w", encoding="utf-8") as f:
                json.dump([{"id": 1, "name": "CHF"}], f)
            with open(os.path.join(tmp, ime.JOURNAL_FILE), "w", encoding="utf-8") as f:
                json.dump([], f)
            out_file = os.path.join(tmp, "plan.json")
            # the problem list goes to <private>: a temp dir here, so a test run never writes the real one
            with mock.patch.object(ime.im, "PRIVATE", tmp), \
                    mock.patch.object(ime.Lookups, "from_erp", return_value=lookups()), \
                    mock.patch.object(ime.im.Erp, "from_file", return_value=None), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                ime.main(["--write", out_file, "--export", tmp])
            with open(out_file, encoding="utf-8") as f:
                plan = json.load(f)
        self.assertEqual([d["bexio_id"] for d in plan["documents"]], ["manual-m-1"])
        self.assertEqual(plan["exchange_rates"], [])

    def test_extra_entries_join_the_plan_after_the_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, ime.ENTRIES_FILE), "w", encoding="utf-8") as f:
                json.dump([entry("m-1")], f)
            with open(os.path.join(tmp, ime.CURRENCIES_FILE), "w", encoding="utf-8") as f:
                json.dump([{"id": 1, "name": "CHF"}], f)
            with open(os.path.join(tmp, ime.JOURNAL_FILE), "w", encoding="utf-8") as f:
                json.dump([], f)
            extra = os.path.join(tmp, "extra.json")
            with open(extra, "w", encoding="utf-8") as f:
                json.dump([entry("1099-correction", date="2024-10-29", lines=[line(11, 21, 165.66)])], f)
            out_file = os.path.join(tmp, "plan.json")
            with mock.patch.object(ime.im, "PRIVATE", tmp), \
                    mock.patch.object(ime.Lookups, "from_erp", return_value=lookups()), \
                    mock.patch.object(ime.im.Erp, "from_file", return_value=None), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                ime.main(["--write", out_file, "--export", tmp, "--extra", extra])
            with open(out_file, encoding="utf-8") as f:
                plan = json.load(f)
        self.assertEqual([d["bexio_id"] for d in plan["documents"]], ["manual-m-1", "manual-1099-correction"])
        self.assertEqual(plan["documents"][1]["values"]["posting_date"], "2024-10-29")

    def test_pending_export_says_not_exported_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(ime.main(["--dry-run", "--export", tmp]), 0)
        self.assertIn("not exported yet", out.getvalue())


if __name__ == "__main__":
    unittest.main()
