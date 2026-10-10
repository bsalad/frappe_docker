"""Offline tests of the ESTV withholding tariffs, with invented rows in the ESTV record format. No frappe needed:

    python3 -m unittest bi_payroll.test_quellensteuer

from this app's directory. The rows are built by position, as the ESTV layout (quellensteuer.py) gives them, so a
field read at the wrong position fails here.
"""

import unittest
from datetime import date
from decimal import Decimal

from bi_payroll.quellensteuer import find_tariff, parse_tariff_file, withholding


def cents(value):
    return str(int(round(value * 100))).zfill(9)


def header(canton):
    # 00 record: canton at 3-4, the creation date at 20-27
    return ("00" + canton + " " * 15 + "20250101").ljust(62)


def end(canton, count):
    # 99 record: canton at 18-19, the record count at 20-27, the 00 and the 99 included
    return ("99" + " " * 15 + canton + str(count).zfill(8)).ljust(62)


def tariff(canton, code, valid, income, step, rate, minimum=0, kind="06", children=0, transaction="01"):
    # one tariff record: kind 1-2, transaction 3-4, canton 5-6, code 7-16, valid from 17-24, income from 25-33,
    # step 34-42, gender 43 (blank), children 44-45, minimum tax 46-54, rate 55-59
    return (kind + transaction + canton + code.ljust(10) + valid + cents(income) + cents(step) + " "
            + str(children).zfill(2) + cents(minimum) + str(round(rate * 100)).zfill(5)).ljust(62)


def file_of(canton, *records):
    lines = [header(canton), *records]
    return "\n".join(lines + [end(canton, len(lines) + 1)]) + "\n"


# invented: one canton, two brackets of 50 francs; a second version valid from 2026 with other rates
OLD = [
    tariff("XX", "A0N", "20250101", 1000, 50, 5.0),
    tariff("XX", "A0N", "20250101", 1050, 50, 5.5, minimum=10),
    tariff("XX", "B2N", "20250101", 1000, 50, 2.0),
]
NEW = [tariff("XX", "A0N", "20260101", 1000, 50, 6.0)]


class ParseFile(unittest.TestCase):
    def test_the_fields_are_read_at_the_spec_positions(self):
        row = parse_tariff_file(file_of("XX", tariff("XX", "B2N", "20250101", 3101, 50, 7.15, minimum=20, children=2)))[0]
        self.assertEqual(row, {
            "canton": "XX", "tariff_code": "B2N", "valid_from": date(2025, 1, 1),
            "income_from": Decimal("3101"), "step": Decimal("50"), "minimum_tax": Decimal("20"),
            "rate": Decimal("7.15"),
        })

    def test_a_predefined_category_is_read_too(self):
        row = parse_tariff_file(file_of("XX", tariff("XX", "HEN", "20250101", 1, 999999, 23, kind="11")))[0]
        self.assertEqual((row["tariff_code"], row["rate"]), ("HEN", Decimal("23")))

    def test_the_fee_and_median_records_are_no_rate_and_skipped(self):
        text = file_of("XX", tariff("XX", "PEL", "20250101", 0, 0, 0, kind="12"),
                       tariff("XX", "MED", "20250101", 0, 0, 0, kind="13"))
        self.assertEqual(parse_tariff_file(text), [])

    def test_a_record_of_another_canton_is_refused(self):
        with self.assertRaises(ValueError):
            parse_tariff_file(file_of("XX", tariff("YY", "A0N", "20250101", 1000, 50, 5.0)))

    def test_the_record_count_of_the_end_record_must_match(self):
        text = "\n".join([header("XX"), *OLD, end("XX", len(OLD) + 9)]) + "\n"
        with self.assertRaises(ValueError):
            parse_tariff_file(text)

    def test_only_new_records_are_loaded(self):
        with self.assertRaises(ValueError):
            parse_tariff_file(file_of("XX", tariff("XX", "A0N", "20250101", 1000, 50, 5.0, transaction="03")))


class FindTariff(unittest.TestCase):
    ROWS = parse_tariff_file(file_of("XX", *OLD)) + parse_tariff_file(file_of("XX", *NEW))

    def test_the_bracket_starts_at_its_income_and_ends_before_the_next(self):
        self.assertEqual(find_tariff(self.ROWS, "XX", "A0N", Decimal("1000"), date(2026, 3, 1))["rate"], Decimal("6"))
        self.assertEqual(find_tariff(self.ROWS, "XX", "A0N", Decimal("1049.99"), date(2025, 3, 1))["rate"], Decimal("5"))
        self.assertEqual(find_tariff(self.ROWS, "XX", "A0N", Decimal("1050"), date(2025, 3, 1))["rate"], Decimal("5.5"))

    def test_the_latest_version_valid_on_the_date_wins(self):
        self.assertEqual(find_tariff(self.ROWS, "XX", "A0N", Decimal("1000"), date(2025, 12, 31))["rate"], Decimal("5"))

    def test_another_code_or_canton_is_no_match(self):
        with self.assertRaises(LookupError):
            find_tariff(self.ROWS, "XX", "C0N", Decimal("1000"), date(2025, 3, 1))
        with self.assertRaises(LookupError):
            find_tariff(self.ROWS, "YY", "A0N", Decimal("1000"), date(2025, 3, 1))

    def test_an_income_below_the_first_bracket_is_no_match(self):
        with self.assertRaises(LookupError):
            find_tariff(self.ROWS, "XX", "A0N", Decimal("999"), date(2025, 3, 1))


class Withholding(unittest.TestCase):
    ROWS = parse_tariff_file(file_of("XX", *OLD))

    def test_the_rate_of_the_bracket_applies_to_the_whole_income(self):
        row = find_tariff(self.ROWS, "XX", "A0N", Decimal("1000"), date(2025, 3, 1))
        self.assertEqual(withholding(Decimal("1000"), row), Decimal("50.00"))

    def test_the_minimum_tax_applies_when_the_rate_gives_less(self):
        row = find_tariff(self.ROWS, "XX", "A0N", Decimal("1050"), date(2025, 3, 1))
        # 1050 at 5.5 percent is 57.75, above the minimum of 10
        self.assertEqual(withholding(Decimal("1050"), row), Decimal("57.75"))
        low = dict(row, minimum_tax=Decimal("100"))
        self.assertEqual(withholding(Decimal("1050"), low), Decimal("100.00"))

    def test_the_tax_is_rounded_half_up_to_the_rappen(self):
        row = find_tariff(self.ROWS, "XX", "B2N", Decimal("1000"), date(2025, 3, 1))
        self.assertEqual(withholding(Decimal("1001.25"), row), Decimal("20.03"))

    def test_no_income_gives_no_tax(self):
        row = find_tariff(self.ROWS, "XX", "A0N", Decimal("1000"), date(2025, 3, 1))
        self.assertEqual(withholding(0, row), Decimal("0.00"))


if __name__ == "__main__":
    unittest.main()
