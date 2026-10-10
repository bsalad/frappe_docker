"""The ESTV withholding tariffs (Quellensteuertarife, Löhne) from their fixed-format text files, and the tax a monthly
gross gives under them. Pure Python, so the offline tests run without frappe (test_quellensteuer.py).

The record layout is the one of ESTV's "Aufbau und Recordformate der Quellensteuer-Tarife ab 2025 (Löhne)", D_DVS
Nr. 0005 / 06.06, valid from 1 January 2025:
https://www.estv.admin.ch/dam/de/sd-web/AvojuyFCZY95/qst-tarife-recordformate-loehne-2025-de.pdf
The download page is https://www.estv.admin.ch/de/quellensteuertarife-import-in-lohnbuchhaltungssysteme. A 2026
version of the format was not read; check it before the 2026 file is loaded.

Every tariff is for a monthly income. Annual, variable and 13th-salary cases are not computed here.
"""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

TARIFF_RECORDS = {"06", "11"}  # progressive tariffs (06) and predefined categories (11); 12 (fee) and 13 (median) are no rate
NEW_RECORD = "01"  # Transaktionsart Neuzugang: the only kind a full yearly file carries


def field(line, start, length):
    # the ESTV positions are 1-based and inclusive
    return line[start - 1:start - 1 + length]


def amount(text):
    # nine digits, the last two are the Rappen
    return Decimal(int(text)) / 100


def parse_tariff_file(text):
    """The rows of one canton's file: a dict per tariff record (06 and 11), with the canton, the tariff code, the date
    valid from, the bracket (income from and step), the minimum tax and the rate in percent."""
    lines = [line for line in text.splitlines() if line.strip()]
    header, end = lines[0], lines[-1]
    if field(header, 1, 2) != "00" or field(end, 1, 2) != "99":
        raise ValueError("a tariff file starts with a 00 record and ends with a 99 record")
    canton = field(header, 3, 2)
    # the 99 record counts all records, the 00 and the 99 included
    if field(end, 18, 2) != canton or int(field(end, 20, 8)) != len(lines):
        raise ValueError("the 99 record does not match the file: canton or record count")
    rows = []
    for line in lines[1:-1]:
        if field(line, 1, 2) not in TARIFF_RECORDS:
            continue
        if field(line, 3, 2) != NEW_RECORD:
            raise ValueError(f"only Neuzugang records are loaded, found Transaktionsart {field(line, 3, 2)}")
        if field(line, 5, 2) != canton:
            raise ValueError("a record names another canton than the file's 00 record")
        rows.append({
            "canton": canton,
            "tariff_code": field(line, 7, 10).strip(),
            "valid_from": date(int(field(line, 17, 4)), int(field(line, 21, 2)), int(field(line, 23, 2))),
            "income_from": amount(field(line, 25, 9)),
            "step": amount(field(line, 34, 9)),
            "minimum_tax": amount(field(line, 46, 9)),
            "rate": Decimal(int(field(line, 55, 5))) / 100,
        })
    return rows


def find_tariff(rows, canton, tariff_code, income, on):
    """The row of the bracket that holds the income: income from <= income < income from + step. Of the rows
    valid on the date, the latest valid from wins, so a year's file replaces the one before it."""
    valid = [
        row for row in rows
        if row["canton"] == canton and row["tariff_code"] == tariff_code and row["valid_from"] <= on
        and row["income_from"] <= income < row["income_from"] + row["step"]
    ]
    if not valid:
        raise LookupError(f"no tariff {tariff_code} in {canton} for an income of {income} on {on}")
    latest = max(row["valid_from"] for row in valid)
    return next(row for row in valid if row["valid_from"] == latest)


def withholding(income, row):
    """The tax on the whole monthly income at the bracket's rate, and never less than the minimum tax (ESTV 4.4)."""
    income = Decimal(str(income))
    if income <= 0:
        return Decimal("0.00")
    tax = (income * row["rate"] / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return max(tax, row["minimum_tax"])


def qst_amount(rows, canton, tariff_code, gross, on):
    """The tax withheld from a monthly gross on the date, under the employee's tariff code. Zero without a code or a
    gross; a code with no bracket for the gross raises LookupError, never a silent zero."""
    gross = Decimal(str(gross))
    if not tariff_code or gross <= 0:
        return Decimal("0.00")
    return withholding(gross, find_tariff(rows, canton, tariff_code, gross, on))
