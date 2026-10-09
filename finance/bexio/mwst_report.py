"""Report the figures of one Swiss VAT period (effective method) from ERPNext, by bexio VAT code.

Reads the submitted Sales and Purchase Invoices of a period from ERPNext, one
row per tax template and per invoice, and adds them up by bexio's 42 VAT codes
and by the Ziffern of the MWST form. Nothing is written to ERPNext: the report
reads, and writes one CSV file under <private>. The stdout shows totals only.

A tax row finds its template by the row's description, which the importers set
to the template's name, and the template by its bexio_id. A row whose template
or code is not known is reported as unmapped, never guessed from the rate.

The base of a tax row is derived from its tax (tax / rate), because the import
does not carry the net per row. At 8.1 % one rappen of tax is about 0.06 of
base, so the derived bases are approximate; the tax per row is exact.

Ziffern: the table below is the form's row per code. Only the rows that the
ESTV form confirmed (302/303, 312/313, 342/343, 382/383, 399, 400, 405, 415,
420) carry a Ziffer. The Umsatz rows 220 to 299 and the Abzüge are not checked
yet, so the codes that would go there carry None and their tax is reported as
not in a checked Ziffer. See the note of erp-agpf for the open points.

Run it as:

    python3 finance/bexio/mwst_report.py --period 2026Q3

Standard library only, plus import_master for the ERPNext client.
"""

import argparse
import csv
import datetime
import os
import sys
from decimal import ROUND_HALF_UP, Decimal

import import_master as im

COMPANY = im.COMPANY
CENT = Decimal("0.01")
ZERO = Decimal("0")

# The Bezugsteuer owed moved from Ziffer 382 to 383 with the rate change; the form has one row each.
# Date of the change as the form gives it (the bexio validity column is not used for this).
BEZUG_NEW_ROW_FROM = datetime.date(2024, 1, 1)

# Optiert (UO77, UO81) is taxed at the normal rate, so its tax is owed in 302/303 like UN77/UN81.
# bexio VAT id -> (code, rate %, kind, form row). Kind S = sales, P = purchase, BZ = Bezugsteuer.
# The form row is where the tax of the template's Add row goes; for BZ the Deduct row
# (the reverse charge on 2203) is the owed tax, see owed_row(). None = not in a checked Ziffer.
# The ids and codes are those of swiss-setup.py's BEXIO_TAXES; the test checks that they match.
TEMPLATES = {
    16: ("UN77", 7.7, "S", "302"),
    28: ("UN81", 8.1, "S", "303"),
    17: ("UR25", 2.5, "S", "312"),
    29: ("UR26", 2.6, "S", "313"),
    18: ("US37", 3.7, "S", "342"),
    30: ("US38", 3.8, "S", "343"),
    3: ("UEX", 0, "S", None),
    4: ("ULA", 0, "S", None),
    5: ("MEL", 0, "S", None),
    6: ("UNO", 0, "S", None),
    13: ("SUB", 0, "S", None),
    14: ("SPE", 0, "S", None),
    15: ("UO77", 7.7, "S", "302"),
    31: ("UO81", 8.1, "S", "303"),
    48: ("U00", 0, "S", None),
    22: ("VM77", 7.7, "P", "400"),
    35: ("VM81", 8.1, "P", "400"),
    8: ("VM25", 2.5, "P", "400"),
    34: ("VM26", 2.6, "P", "400"),
    21: ("VM37", 3.7, "P", "400"),
    36: ("VM38", 3.8, "P", "400"),
    7: ("VIM", 0, "P", None),
    9: ("ZOLLM", 0, "P", None),
    33: ("BZM81", 8.1, "BZ", "400"),
    19: ("BZM77", 7.7, "BZ", "400"),
    24: ("VB77", 7.7, "P", "405"),
    38: ("VB81", 8.1, "P", "405"),
    12: ("VB25", 2.5, "P", "405"),
    37: ("VB26", 2.6, "P", "405"),
    23: ("VB37", 3.7, "P", "405"),
    39: ("VB38", 3.8, "P", "405"),
    47: ("V00", 0, "P", "405"),
    10: ("VSF", 0, "P", None),
    11: ("ZOLLB", 0, "P", None),
    32: ("BZB81", 8.1, "BZ", "405"),
    20: ("BZB77", 7.7, "BZ", "405"),
    25: ("VES", 7.7, "P", "415"),
    26: ("VEV", 7.7, "P", "415"),
    27: ("VKÜ", 7.7, "P", "415"),
    40: ("VES", 8.1, "P", "415"),
    41: ("VEV", 8.1, "P", "415"),
    42: ("VKÜ", 8.1, "P", "415"),
}

# The form rows that get a total, with a label for the output.
LABELS = {
    "200": "Gesamtbetrag der Entgelte (CHF, netto)",
    "302": "Normalsatz 7.7 %",
    "303": "Normalsatz 8.1 %",
    "312": "Reduzierter Satz 2.5 %",
    "313": "Reduzierter Satz 2.6 %",
    "342": "Sondersatz 3.7 %",
    "343": "Sondersatz 3.8 %",
    "382": "Bezugsteuer (bis 2023)",
    "383": "Bezugsteuer (ab 2024)",
    "399": "Total der geschuldeten Steuer",
    "400": "Vorsteuer Material- und Dienstleistungsaufwand",
    "405": "Vorsteuer Investitionen und übriger Betriebsaufwand",
    "415": "Vorsteuerkorrekturen",
    "420": "Total der Vorsteuern",
}


def period_bounds(period):
    """'2026Q3' is a quarter, '2026H1' or '2026H2' a half-year: the first and last day as dates."""
    year, part = int(period[:4]), period[4:]
    if part in ("Q1", "Q2", "Q3", "Q4"):
        first_month = 3 * (int(part[1]) - 1) + 1
        last_month = first_month + 2
    elif part in ("H1", "H2"):
        first_month = 1 if part == "H1" else 7
        last_month = first_month + 5
    else:
        raise ValueError("period {!r}: use 2026Q3 or 2026H1".format(period))
    first = datetime.date(year, first_month, 1)
    nxt = datetime.date(year + (last_month == 12), last_month % 12 + 1, 1)
    return first, nxt - datetime.timedelta(days=1)


def owed_row(posting_date):
    """The Ziffer of Bezugsteuer owed on a document of this date."""
    return "383" if posting_date >= BEZUG_NEW_ROW_FROM else "382"


def derived_base(tax, rate):
    """The net of a tax row from its tax: exact only to a few rappen, see the module note."""
    if not rate:
        return None
    return (tax * 100 / Decimal(str(rate))).quantize(CENT, rounding=ROUND_HALF_UP)


class Totals:
    """Base and tax per form row, and what could not be put in a row."""

    def __init__(self):
        self.rows = {}
        self.unmapped = []
        self.unchecked_tax = ZERO
        self.by_code = {}

    def add(self, row, base, tax):
        acc = self.rows.setdefault(row, [ZERO, ZERO])
        if base is not None:
            acc[0] += base
        acc[1] += tax

    def tax_of(self, row):
        return self.rows.get(row, [ZERO, ZERO])[1]

    def base_of(self, row):
        return self.rows.get(row, [ZERO, ZERO])[0]


def plan(sales, purchases, templates, totals=None):
    """Add the documents up by form row. sales and purchases are ERPNext documents as dicts:
    posting_date (date), base_net_total, and taxes (rows with description, rate, base_tax_amount,
    tax_amount, add_deduct_tax). templates maps a template name to its bexio id (string).
    Returns the Totals; unmapped rows are named by their template only."""
    totals = totals or Totals()
    for doc in sales:
        totals.add("200", Decimal(str(doc["base_net_total"])), ZERO)
        for row in doc["taxes"]:
            _tax_row(totals, row, templates, doc["posting_date"], purchase=False)
    for doc in purchases:
        for row in doc["taxes"]:
            _tax_row(totals, row, templates, doc["posting_date"], purchase=True)
    return totals


def _tax_row(totals, row, templates, posting_date, purchase):
    bexio_id = templates.get(row.get("description"))
    entry = TEMPLATES.get(int(bexio_id)) if bexio_id and bexio_id.isdigit() else None
    if entry is None:
        totals.unmapped.append(row.get("description") or "(no template)")
        return
    code, rate, kind, form_row = entry
    tax = Decimal(str(row.get("base_tax_amount") or row.get("tax_amount") or 0))
    deduct = purchase and row.get("add_deduct_tax") == "Deduct"
    if deduct:
        # a reverse charge comes back on 2203 as the owed tax; the same amount is the Vorsteuer on the Add row
        if kind != "BZ":
            totals.unmapped.append("Deduct row on {}".format(code))
            return
        form_row = owed_row(posting_date)
    totals.by_code.setdefault(code, [ZERO, ZERO])[1] += tax
    if form_row is None:
        totals.unchecked_tax += tax
        return
    totals.add(form_row, derived_base(tax, rate), tax)


def form_totals(totals):
    """The totals of the form rows that the form adds up: 399 owed, 420 Vorsteuer. 479, 500, 510 need data not in bexio."""
    owed = sum((totals.tax_of(r) for r in ("302", "303", "312", "313", "342", "343", "382", "383")), ZERO)
    vorsteuer = sum((totals.tax_of(r) for r in ("400", "405", "415")), ZERO)
    out = dict(totals.rows)
    out["399"] = [ZERO, owed]
    out["420"] = [ZERO, vorsteuer]
    return out, owed - vorsteuer


def csv_rows(totals):
    """One line per form row, base and tax, for the private CSV."""
    rows, _saldo = form_totals(totals)
    for row in sorted(set(rows) | set(LABELS)):
        if row == "200" or row in rows:
            base, tax = rows.get(row, [ZERO, ZERO])
            yield [row, LABELS.get(row, row), "{:.2f}".format(base), "{:.2f}".format(tax)]


def summary_lines(totals):
    """The stdout of a run: totals per form row and what is not in a checked row. No names, no bexio ids."""
    rows, saldo = form_totals(totals)
    lines = ["{:<6}{:<50}{:>16}{:>14}".format("Ziffer", "", "Entgelt", "Steuer")]
    for row in ("200", "302", "303", "312", "313", "342", "343", "382", "383", "399", "400", "405", "415", "420"):
        base, tax = rows.get(row, [ZERO, ZERO])
        if row == "200" or base or tax:
            lines.append("{:<6}{:<50}{:>16,.2f}{:>14,.2f}".format(row, LABELS[row], base, tax))
    lines.append("")
    lines.append("Saldo before Ziffer 479 and Abzüge 280: {:,.2f} (positive: to pay)".format(saldo))
    lines.append("tax not in a checked Ziffer (Umsatz 220-299, zero-rate, imports): {:,.2f}".format(totals.unchecked_tax))
    if totals.unmapped:
        lines.append("unmapped tax rows: {} (names in the private CSV's log, not here)".format(len(totals.unmapped)))
    return lines


def write_csv(path, totals):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ziffer", "label", "entgelt_chf", "steuer_chf"])
        for line in csv_rows(totals):
            writer.writerow(line)
        writer.writerow([])
        writer.writerow(["code", "steuer_chf"])
        for code in sorted(totals.by_code):
            writer.writerow([code, "{:.2f}".format(totals.by_code[code][1])])
        writer.writerow([])
        writer.writerow(["unmapped_template", "rows"])
        for name, count in sorted(_count(totals.unmapped).items()):
            writer.writerow([name, count])


def _count(items):
    out = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return out


def fetch(erp, start, end):
    """The submitted invoices of the period and their tax rows, read through the API user."""
    filters = [["company", "=", COMPANY], ["docstatus", "=", 1], ["posting_date", "between", [str(start), str(end)]]]
    sales = erp.list("Sales Invoice", filters, ["name", "posting_date", "base_net_total"])
    purchases = erp.list("Purchase Invoice", filters, ["name", "posting_date", "base_net_total"])
    out = {}
    for kind, docs in (("sales", sales), ("purchases", purchases)):
        out[kind] = []
        for d in docs:
            doctype = "Sales Invoice" if kind == "sales" else "Purchase Invoice"
            full = erp.get(doctype, d["name"])
            out[kind].append({
                "posting_date": datetime.date.fromisoformat(str(full["posting_date"])),
                "base_net_total": full["base_net_total"],
                "taxes": full.get("taxes") or [],
            })
    return out


def templates_by_name(erp):
    """Tax template name -> bexio id, for the templates that have one (the sales and purchase ones)."""
    names = {}
    for doctype in ("Sales Taxes and Charges Template", "Purchase Taxes and Charges Template"):
        for t in erp.list(doctype, [["bexio_id", "is", "set"]], ["name", "bexio_id"]):
            names[t["name"]] = str(t["bexio_id"])
    return names


def main(argv):
    parser = argparse.ArgumentParser(description="MWST figures of one period by bexio VAT code, from ERPNext (read only).")
    parser.add_argument("--period", required=True, help="2026Q3 or 2026H1 (effective method)")
    parser.add_argument("--report", default=None, help="private CSV (default <private>/mwst-<period>.csv)")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    start, end = period_bounds(args.period)
    report = args.report or os.path.join(im.PRIVATE, "mwst-{}.csv".format(args.period))

    erp = im.Erp.from_file(args.token_file)
    try:
        templates = templates_by_name(erp)
        docs = fetch(erp, start, end)
        gl = gl_by_account(erp, start, end)
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2

    totals = plan(docs["sales"], docs["purchases"], templates)
    print("MWST {} ({} to {}): {} sales and {} purchase invoices".format(
        args.period, start, end, len(docs["sales"]), len(docs["purchases"])))
    for line in summary_lines(totals):
        print(line)
    print("")
    for line in reconcile_lines(totals, gl):
        print(line)
    write_csv(report, totals)
    print("written: {}".format(report))
    return 1 if totals.unmapped else 0


def gl_by_account(erp, start, end):
    """Net GL balance (debit minus credit) of the VAT accounts 2200, 1170, 1171 in the period. The check against the
    report: manual journal entries with tax show up here and not in the invoice rows."""
    accounts = erp.list("Account", [["company", "=", COMPANY], ["account_number", "in", ["2200", "1170", "1171"]]],
                        ["name", "account_number"])
    names = {a["name"]: a["account_number"] for a in accounts}
    if not names:
        return {}
    entries = erp.list("GL Entry", [["account", "in", list(names)], ["is_cancelled", "=", 0],
                                    ["posting_date", "between", [str(start), str(end)]]],
                       ["account", "debit", "credit"])
    out = {}
    for e in entries:
        out[names[e["account"]]] = out.get(names[e["account"]], ZERO) + Decimal(str(e["debit"])) - Decimal(str(e["credit"]))
    return out


def reconcile_lines(totals, gl):
    """Sales tax (Ziffer 399) against the credit of 2200, Vorsteuer (420) against the debit of 1170 and 1171."""
    rows, _ = form_totals(totals)
    owed = rows.get("399", [ZERO, ZERO])[1]
    vorsteuer = rows.get("420", [ZERO, ZERO])[1]
    gl_sales = -gl.get("2200", ZERO)
    gl_input = gl.get("1170", ZERO) + gl.get("1171", ZERO)
    return [
        "check against GL: 2200 credit {:,.2f} vs Ziffer 399 {:,.2f}: difference {:,.2f}".format(gl_sales, owed, gl_sales - owed),
        "check against GL: 1170+1171 debit {:,.2f} vs Ziffer 420 {:,.2f}: difference {:,.2f}".format(gl_input, vorsteuer, gl_input - vorsteuer),
        "differences come from manual journal entries with tax and from the bases not in a checked Ziffer",
    ]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
