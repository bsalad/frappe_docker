"""Map the exported bexio sales documents to ERPNext: invoices, credit notes, orders, offers, deliveries.

Step three of the bexio pipeline, after import_master.py: the customers, items
and accounts it imported are looked up here by their bexio_id. Each function
takes one export record and the lookups and returns the ERPNext document as a
dict, not yet inserted or submitted. Whether these documents post GL, and how
payments reach them, is the posting plan of finance-3qsp. What this step
writes is drafts only (docstatus 0, no GL): --dry-run, the default, reads
ERPNext and reports what a run would map; --apply writes the invoices as
drafts named by bexio's document number (see write_drafts).

A record that cannot be mapped (an unknown VAT code, account, contact, item or
position type, a credit note whose invoice is missing, a total that differs
from bexio's by more than 5 rappen) raises Unmapped, and the report names it by
bexio id only. Amounts are checked against bexio's own totals: the net, the
taxes per rate and the total. A difference is reported; the total is the one
exception, a difference of up to 5 rappen goes into the last tax row, or where no
tax row can take it (prices with the VAT in them, no VAT at all) into a "Rundung"
item line or a grand-total discount, so that the grand total is bexio's to the rappen.

The export directory holds the documents with their positions (the
single-document calls), next to the entity files of export.py:
invoices.json, orders.json, offers.json, deliveries.json and credit_vouchers.json when bexio
has credit notes. Amounts are in the document currency; the CHF figures are
the document amounts times the exchange rate bexio gives for the document.

Run it as:

    python3 finance/bexio/import_sales.py --dry-run [--export DIR]

    python3 finance/bexio/import_sales.py --apply [--export DIR]

--export defaults to the newest directory under <private>/bexio-export/. The
output is totals only; the differences and unmapped records go to a file under
<private>, never to the repository. Standard library only, plus import_master.
"""

import argparse
import collections
import json
import os
import subprocess
import sys
import urllib.request
from decimal import ROUND_HALF_UP, Decimal

import import_master as im

COMPANY = im.COMPANY
BASE_CURRENCY = "CHF"
# the ECB's rate of a currency on a day, from the service ERPNext's own exchange-rate fetch uses; a day
# without a rate (a weekend, a holiday) gets the last one before it, and the response names that day
RATE_URL = "https://api.frankfurter.app/{day}?from={code}&to=CHF"
# the documents --apply writes, by export file: the invoices, credit notes, orders, offers and deliveries, all as drafts
# (the submit of the invoices is bexio-drafts.sh's; orders, offers and delivery notes are never submitted here)
APPLY_FILES = ("invoices", "credit_vouchers", "orders", "offers", "deliveries")
DRAFTS_FILE = "bexio-sales-drafts.json"
LOADER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "bexio-drafts.sh")
# free-text positions and text lines: one service item, the description keeps the bexio text
GENERIC_ITEM = "bexio Position"
# the lookup value of an invoice that is in the export but not yet in ERPNext (dry run only)
PLANNED = "(planned)"
# the credit note's link to its invoice; the field name is taken from the bexio record and
# still needs confirming against the full export (see README)
ORIGINAL = "invoice_id"

# the documents of the export: file, ERPNext doctype, and whether a credit note
DOCUMENTS = (
    ("invoices", "Sales Invoice", False),
    ("credit_vouchers", "Sales Invoice", True),
    ("orders", "Sales Order", False),
    ("offers", "Quotation", False),
    ("deliveries", "Delivery Note", False),
)

# bexio position type -> how it maps. A type not listed here is refused, not guessed.
POSITION_TYPES = {
    "KbPositionCustom": "free",        # free-text position with a price and an account
    "KbPositionArticle": "article",    # a position of an article: the Item by bexio_id
    "KbPositionText": "text",          # a text line without a price: a row of 0
    "KbPositionPagebreak": "skip",     # layout only
    "KbPositionSubtotal": "skip",      # a display row of the sum above: ERPNext totals the rows itself
    "KbPositionDiscount": "discount",  # a document discount: no amount in the export, see _document
}

CENT = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")
# ERPNext keeps a quantity to three places
QUANTITY = Decimal("0.001")
# the most a document's total may differ from bexio's; the difference goes into the last tax row
TOTAL_TOLERANCE = Decimal("0.05")
# the most a foreign document's CHF total may differ from bexio's own CHF booking on the receivables account
JOURNAL_TOLERANCE = Decimal("0.05")
RECEIVABLES = "1100"
# the output VAT of a sale at the invoice date (bexio's transitory account); the template's own account is not used
VAT_ACCOUNT = "2202"


class Unmapped(Exception):
    """A record the importer does not map. The message names the reason, never a company name."""


def _dec(value):
    return Decimal(str(value))


def _cents(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def ecb_rate(code, day):
    """(rate to CHF, the day it is for) of a currency on a day, from the ECB's published rates."""
    # the service refuses urllib's default User-Agent with 403
    request = urllib.request.Request(RATE_URL.format(day=day, code=code), headers={"User-Agent": "bexio-import/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as f:
            body = json.load(f)
    except OSError as err:
        raise Unmapped("no ECB rate for {} on {}: {}".format(code, day, err))
    return _dec(body["rates"][BASE_CURRENCY]), body["date"]


def _currency(record, lookups):
    """(currency code, rate to CHF, the day of the ECB rate or None). A foreign currency takes the rate bexio gives;
    where bexio gives none, the ECB's rate of the document date (lookups["rate_at"]), listed as a difference."""
    code = lookups["currency"].get(str(record.get("currency_id")))
    if code is None:
        raise Unmapped("currency {} is not in the export".format(record.get("currency_id")))
    if code == BASE_CURRENCY:
        return code, Decimal("1"), None
    if record.get("exchange_rate"):
        return code, _dec(record["exchange_rate"]), None
    rate_at = lookups.get("rate_at")
    if rate_at is None:
        raise Unmapped("no exchange rate for {}".format(code))
    rate, day = rate_at(code, record["is_valid_from"])
    return code, rate, day


def _in_chf(record, rate):
    """The record with its amounts in CHF at bexio's rate, each to the cent: the unit prices, the taxes and the totals.

    A foreign document is booked in CHF, as bexio books it: the receivable account is CHF in ERPNext, and a
    document must be in the currency of its account. The original amounts go into the remarks.
    """
    def chf(value):
        return None if value is None else float(_cents(_dec(value) * rate))

    positions = [dict(pos, unit_price=chf(pos["unit_price"])) if "unit_price" in pos else pos
                 for pos in record.get("positions") or []]
    taxs = [dict(tax_line, value=chf(tax_line["value"])) for tax_line in record.get("taxs") or []]
    return dict(record, positions=positions, taxs=taxs, total=chf(record.get("total")),
                total_net=chf(record.get("total_net")), total_taxes=chf(record.get("total_taxes")))


def _tax(tax_id, lookups):
    """The ERPNext tax template whose bexio_id is this bexio tax id. Never chosen by rate."""
    template = lookups["tax"].get(str(tax_id))
    if template is None:
        raise Unmapped("bexio tax {} has no ERPNext template with that bexio_id".format(tax_id))
    if template["rate"] is None:
        raise Unmapped("bexio tax {}: its ERPNext template has no single rate row".format(tax_id))
    return template


def _rows(record, lookups):
    """The item rows in bexio's order, the amounts per bexio tax id, and whether the document has a discount row.

    A position's rate is bexio's unit price. A position discount goes to ERPNext's own discount fields
    (price list rate and discount percentage), with the rate worked out in cents the way ERPNext does it,
    so that the row amount is the one ERPNext computes. A line without a tax id has no tax: its amount is
    kept under the key None and gets no tax row.

    A row at rate 0 gets price list rate 0 too: the free-text item has a selling price in ERPNext, and
    a zero rate would otherwise be filled from it on save. A line ERPNext cannot hold as it is (a quantity of
    more than three places, a fraction on the free-text item, which takes whole quantities, or a discount on
    the free-text item, whose price ERPNext would refill) is one unit at its amount, with its quantity and
    discount kept in its text.
    """
    rows, amounts, discount_row = [], {}, False
    for pos in record.get("positions") or []:
        kind = POSITION_TYPES.get(pos.get("type"))
        if kind is None:
            raise Unmapped("position type {} is not mapped".format(pos.get("type")))
        if kind == "skip":
            continue
        if kind == "discount":
            discount_row = True
            continue
        text = pos.get("text") or ""
        if kind == "text":
            rows.append({"item_code": GENERIC_ITEM, "description": text, "qty": 1.0, "rate": 0.0, "amount": 0.0,
                         "price_list_rate": 0.0})
            continue
        qty, price = _dec(pos["amount"]), _dec(pos["unit_price"])
        percent = _dec(pos.get("discount_in_percent") or 0)
        if kind == "article":
            item = lookups["item"].get(str(pos.get("article_id")))
            if item is None:
                raise Unmapped("article {} has no Item".format(pos.get("article_id")))
        else:
            item = GENERIC_ITEM
        account = lookups["account"].get(str(pos.get("account_id")))
        if account is None:
            raise Unmapped("account {} has no Account".format(pos.get("account_id")))
        unit_discount = _cents(price * percent / 100)
        rate = price - unit_discount
        whole = qty == qty.to_integral_value()
        precise = qty == qty.quantize(QUANTITY)
        if (item == GENERIC_ITEM and (not whole or percent)) or not precise:
            details = "{:f} x {:f}".format(qty.normalize(), price)
            if percent:
                details += " less {:f}%".format(percent.normalize())
            text = "{}: {}".format(details, text)
            qty, rate, percent = ONE, _cents(qty * rate), ZERO
        row = {"item_code": item, "description": text, "qty": float(qty), "rate": float(rate),
               "income_account": account}
        if percent:
            row.update(price_list_rate=float(price), discount_percentage=float(percent))
        if not rate:
            row["price_list_rate"] = 0.0
        rows.append(row)
        key = None
        if pos.get("tax_id") is not None:
            _tax(pos["tax_id"], lookups)
            key = str(pos["tax_id"])
        # bexio leaves an optional position out of its total; ERPNext's quotation row for it is an alternative,
        # which it leaves out of the totals too, so the row keeps what was offered and the total stays bexio's
        if pos.get("is_optional"):
            row["is_alternative"] = 1
        else:
            amounts[key] = amounts.get(key, ZERO) + _cents(qty * rate)
    return rows, amounts, discount_row


def _document(doctype, record, lookups, credit=False):
    """(ERPNext document, differences, (net, tax, gross) in the document currency, rate to CHF).

    Prices are net, unless bexio says they include the VAT (mwst_is_net false): then the tax is taken out of
    the gross and its row is marked as included. The tax is worked out per rate from the lines, the way
    ERPNext would; bexio's own tax per rate is compared, never copied in. The total is bexio's `total`, the
    one that includes the VAT after the discounts (total_gross is before them). A difference of up to
    5 rappen against it goes into the last tax row, or into a Rundung line or a grand-total discount where no
    tax row takes it; a larger one is unmapped.
    """
    bexio_id = ("credit-" if credit else "") + str(record["id"])
    customer = lookups["customer"].get(str(record.get("contact_id")))
    if customer is None:
        raise Unmapped("contact {} has no Customer".format(record.get("contact_id")))
    included = record.get("mwst_is_net") is False
    currency, rate, rate_day = _currency(record, lookups)
    foreign = currency != BASE_CURRENCY
    if foreign:
        original = record
        record = _in_chf(record, rate)
    rows, amounts, discount_row = _rows(record, lookups)
    # an empty document cannot be saved in ERPNext (its totals fail on no items): it is left out, not planned
    if not rows:
        raise Unmapped("no positions")
    if doctype != "Quotation" and any(row.get("is_alternative") for row in rows):
        raise Unmapped("an optional position outside a quotation")
    # bexio gives a delivery a header total of zero though its positions carry prices: the draft keeps the
    # quantities at zero rate, so it agrees with bexio's total; the prices stay in the private export
    zero_rated = doctype == "Delivery Note" and record.get("total") is not None and _dec(record["total"]) == ZERO and any(amounts.values())
    if zero_rated:
        for row in rows:
            row.update(rate=0.0, price_list_rate=0.0)
            row.pop("discount_percentage", None)
        amounts = {key: ZERO for key in amounts}
    if discount_row and (included or credit):
        raise Unmapped("a document discount with prices including the VAT or on a credit note")
    if included and credit:
        raise Unmapped("a credit note with prices including the VAT")
    lines = sum(amounts.values(), ZERO)

    # a document discount: bexio gives none as an amount, so it is the lines less bexio's net; the
    # taxes are worked out on what is left, shared out over the lines
    discount = ZERO
    if discount_row:
        if record.get("total_net") is None:
            raise Unmapped("record has no total_net")
        discount = lines - _dec(record["total_net"])
        if discount <= ZERO:
            raise Unmapped("the document discount does not reduce the net")
    taxable = lines - discount

    taxes, computed, differences = [], {}, []
    if zero_rated:
        differences.append("bexio's total is zero: rows at zero rate with their quantities, the prices stay in the private export")
    last_rate = None
    for key, amount in amounts.items():
        if key is None:
            continue
        template = _tax(key, lookups)
        base = amount * taxable / lines if discount else amount
        # the VAT of a sale goes to bexio's transitory account 2202 at the invoice date; bexio moves it to 2200 on
        # payment (import_payments_in), so the template's own account (2200) would book it twice
        if included:
            # ERPNext takes the VAT out of an included price itself; an actual amount cannot be included
            tax_amount = _cents(base * template["rate"] / (100 + template["rate"]))
            row = {"charge_type": "On Net Total", "account_head": lookups["vat"],
                   "description": template["name"], "rate": float(template["rate"]), "included_in_print_rate": 1}
        else:
            tax_amount = _cents(base * template["rate"] / 100)
            row = {"charge_type": "Actual", "account_head": lookups["vat"],
                   "description": template["name"], "tax_amount": float(tax_amount)}
        taxes.append(row)
        computed[template["rate"]] = computed.get(template["rate"], ZERO) + tax_amount
        last_rate = template["rate"]
    tax = sum(computed.values(), ZERO)

    given = {}
    for tax_line in record.get("taxs") or []:
        given[_dec(tax_line["percentage"])] = _cents(_dec(tax_line["value"]))
    for percentage in sorted(set(computed) | set(given)):
        diff = given.get(percentage, ZERO) - computed.get(percentage, ZERO)
        if diff:
            differences.append("tax {:f}% {:+}".format(percentage.normalize(), diff))
    if rate_day:
        differences.append("exchange rate {} for {}: bexio gives none, the ECB's of {} is used".format(rate, currency, rate_day))

    # bexio charged no VAT on the document at all, though its positions carry codes: it is imported as bexio
    # charged it, untaxed, and the remark says so; the per-rate differences above list the codes
    uncharged = not included and bool(tax) and record.get("total_taxes") is not None and _dec(record["total_taxes"]) == ZERO
    if uncharged:
        differences.append("bexio charged no VAT: imported untaxed, as bexio charged it")
        taxes, computed, tax, last_rate = [], {}, ZERO, None

    if record.get("total") is None:
        raise Unmapped("record has no total")
    target = _dec(record["total"])
    grand = lines if included else taxable + tax
    absorb = target - grand
    if abs(absorb) > TOTAL_TOLERANCE:
        raise Unmapped("total differs from bexio's by {:+}".format(absorb))
    # no VAT row takes the difference (prices with the VAT in them, or no VAT at all): bexio's total is the booked
    # amount, so a rounding line or a grand-total discount makes ERPNext's total bexio's. Invoices, orders and offers
    # take it; a credit note's sign would be the wrong way round, and delivery notes keep their refusal
    grand_discount = ZERO
    if absorb and (included or last_rate is None):
        if credit or doctype == "Delivery Note":
            raise Unmapped("nothing to take a total difference of {:+} in".format(absorb))
        rappen = int(absorb * 100)
        if absorb > 0:
            income = next((row["income_account"] for row in rows if "income_account" in row), None)
            if income is None:
                raise Unmapped("nothing to take a total difference of {:+} in".format(absorb))
            rows.append({"item_code": GENERIC_ITEM, "description": "Rundung (bexio Total)", "qty": 1.0,
                         "rate": float(absorb), "income_account": income})
            differences.append("total {:+} rappen absorbed in a Rundung line".format(rappen))
        else:
            if discount:
                raise Unmapped("a document discount and a total difference of {:+}: ERPNext has one discount field per document".format(absorb))
            grand_discount = -absorb
            differences.append("total {:+} rappen absorbed as a grand-total discount".format(rappen))
    elif absorb:
        taxes[-1]["tax_amount"] = float(_dec(taxes[-1]["tax_amount"]) + absorb)
        computed[last_rate] += absorb
        tax += absorb
        differences.append("total {:+} taken into the last tax row".format(absorb))
    net = target - tax if included else taxable
    for label, have, field in (("net", net, "total_net"), ("taxes", tax, "total_taxes")):
        if record.get(field) is None:
            raise Unmapped("record has no {}".format(field))
        diff = have - _dec(record[field])
        if diff:
            differences.append("{} {:+}".format(label, diff))

    doc = {
        "doctype": doctype, "company": COMPANY, "currency": BASE_CURRENCY, "conversion_rate": 1.0,
        "bexio_id": bexio_id,
        "terms": "\n".join(part for part in (record.get("header"), record.get("footer")) if part),
        "items": rows, "taxes": taxes,
    }
    if discount:
        doc.update(apply_discount_on="Net Total", discount_amount=float(discount))
    if grand_discount:
        doc.update(apply_discount_on="Grand Total", discount_amount=float(grand_discount))
    if doctype == "Quotation":
        doc.update(quotation_to="Customer", party_name=customer, transaction_date=record["is_valid_from"],
                   valid_till=record.get("is_valid_until"))
    else:
        doc["customer"] = customer
    if doctype == "Sales Invoice":
        remarks = "bexio Nr. {}".format(record.get("document_nr") or "")
        if foreign:
            remarks += "; bexio: {} {:.2f} @ {:f}".format(currency, _dec(original["total"]), rate)
            if rate_day:
                remarks += " (ECB {})".format(rate_day)
        if uncharged:
            remarks += "; bexio: no VAT charged although positions carry a VAT code"
        doc.update(remarks=remarks,
                   posting_date=record["is_valid_from"], due_date=record.get("is_valid_to"),
                   set_posting_time=1)
    else:
        # ERPNext's Sales Order and Quotation have neither a remarks field nor an income_account on their items
        for row in rows:
            row.pop("income_account", None)
    if doctype == "Sales Order":
        doc["transaction_date"] = record["is_valid_from"]
        for row in rows:
            row["delivery_date"] = record["is_valid_from"]
    if doctype == "Delivery Note":
        # its date is the posting date; without one ERPNext would take today
        doc.update(posting_date=record["is_valid_from"], set_posting_time=1)

    sign = -1 if credit else 1
    if credit:
        original = lookups["invoice"].get(str(record.get(ORIGINAL)))
        if original is None:
            raise Unmapped("original invoice {} is not in ERPNext or the export".format(record.get(ORIGINAL)))
        doc.update(is_return=1, return_against=original)
        for row in rows:
            row["qty"] = -row["qty"]
        for row in taxes:
            row["tax_amount"] = -row["tax_amount"]
    totals = tuple(sign * x for x in (net, tax, target))
    # the document is in CHF, so its totals are CHF already
    return doc, differences, totals, ONE


def sales_invoice(record, lookups):
    """The Sales Invoice of a bexio invoice (kb_invoice with its positions)."""
    return _document("Sales Invoice", record, lookups)[0]


def credit_note(record, lookups):
    """A Sales Invoice with is_return against the invoice it credits."""
    return _document("Sales Invoice", record, lookups, credit=True)[0]


def sales_order(record, lookups):
    """The Sales Order of a bexio order (kb_order)."""
    return _document("Sales Order", record, lookups)[0]


def quotation(record, lookups):
    """The Quotation of a bexio offer (kb_offer)."""
    return _document("Quotation", record, lookups)[0]


def delivery_note(record, lookups):
    """The Delivery Note of a bexio delivery (kb_delivery with its positions); a draft, so it moves no stock and posts no GL."""
    return _document("Delivery Note", record, lookups)[0]


def unknown_fields(doc, metas):
    """Fields of the document and its rows that the ERPNext doctype does not have."""
    found = []
    for key in doc:
        if key in ("doctype", "items", "taxes"):
            continue
        if key not in metas[doc["doctype"]]:
            found.append("{}.{}".format(doc["doctype"], key))
    for table, doctype in (("items", doc["doctype"] + " Item"), ("taxes", "Sales Taxes and Charges")):
        for row in doc.get(table, []):
            found.extend("{}.{}".format(doctype, key) for key in row if key not in metas[doctype])
    return sorted(set(found))


def _journal_problem(key, record, total, booked):
    """Why a foreign document is not bexio's own CHF booking on the receivables account, or None when it is."""
    if key != "invoices":
        return "a foreign-currency {} is not checked against bexio's journal".format(key)
    chf = booked.get(("KbInvoice", str(record["id"])))
    if chf is None:
        return "bexio's journal has no CHF booking on {} for the invoice".format(RECEIVABLES)
    if abs(total - chf) > JOURNAL_TOLERANCE:
        return "CHF total differs from bexio's CHF booking on {} by {:+}".format(RECEIVABLES, total - chf)
    return None


def plan(data, lookups, metas=None):
    """Map every record of the export. Nothing is written; returns one result per record.

    A foreign document is mapped in CHF and checked against bexio's journal: its CHF total must be the
    CHF bexio booked on the receivables account for it, within JOURNAL_TOLERANCE, or it is left out.
    """
    journal, accounts = data.get("journal", []), data.get("accounts", [])
    booked = im.booked_chf(journal, accounts, RECEIVABLES)
    # an invoice without a rate of its own takes the one bexio booked it at, before the ECB's (see _currency)
    booked_rates = im.booking_rates(journal, accounts, RECEIVABLES, "KbInvoice")
    results = []
    for key, doctype, credit in DOCUMENTS:
        for record in data.get(key, []):
            bexio_id = ("credit-" if credit else "") + str(record["id"])
            result = {"key": key, "doctype": doctype, "bexio_id": bexio_id, "record": record,
                      "year": str(record.get("is_valid_from") or "????")[:4], "doc": None,
                      "differences": [], "chf": None, "unknown": [], "error": None}
            if key == "invoices" and not record.get("exchange_rate") and str(record["id"]) in booked_rates:
                record = dict(record, exchange_rate=booked_rates[str(record["id"])])
            try:
                doc, differences, totals, rate = _document(doctype, record, lookups, credit)
                if lookups["currency"].get(str(record.get("currency_id"))) != BASE_CURRENCY:
                    problem = _journal_problem(key, record, totals[2], booked)
                    if problem:
                        raise Unmapped(problem)
            except Unmapped as err:
                result["error"] = str(err)
            else:
                result.update(doc=doc, differences=differences, chf=tuple(x * rate for x in totals))
                if metas:
                    result["unknown"] = unknown_fields(doc, metas)
            results.append(result)
    return results


def load_documents(path):
    """The export's documents and currencies; a file the export does not have is left out."""
    data = {}
    for name in [key for key, _doctype, _credit in DOCUMENTS] + ["currencies", "accounts", "journal"]:
        file = os.path.join(path, name + ".json")
        if os.path.exists(file):
            with open(file, encoding="utf-8") as f:
                data[name] = json.load(f)
    data.setdefault("currencies", [])
    return data


def lookups_from_erp(erp, data):
    """What the mapping needs from ERPNext, read only: the documents by bexio_id, the tax account."""
    def bexio_names(doctype):
        rows = erp.list(doctype, [["bexio_id", "is", "set"]], ["name", "bexio_id"])
        return {r["bexio_id"]: r["name"] for r in rows}

    invoices = bexio_names("Sales Invoice")
    for record in data.get("invoices", []):
        invoices.setdefault(str(record["id"]), PLANNED)
    # the tax templates by their bexio tax id; a template with one rate row gives its rate and account
    tax = {}
    for template in erp.list("Sales Taxes and Charges Template", [["bexio_id", "is", "set"]], ["name", "bexio_id"]):
        rows = erp.get("Sales Taxes and Charges Template", template["name"]).get("taxes") or []
        single = rows[0] if len(rows) == 1 else None
        tax[template["bexio_id"]] = {
            "name": template["name"],
            "account": single and single["account_head"],
            "rate": single and _dec(single["rate"]),
        }
    vat = erp.list("Account", [["company", "=", COMPANY], ["account_number", "=", VAT_ACCOUNT]], ["name"])
    if len(vat) != 1:
        raise Unmapped("expected one Account {} in ERPNext, found {}".format(VAT_ACCOUNT, len(vat)))
    return {
        "vat": vat[0]["name"],
        "currency": {str(c["id"]): c["name"] for c in data["currencies"]},
        "customer": bexio_names("Customer"),
        "item": bexio_names("Item"),
        "account": bexio_names("Account"),
        "tax": tax,
        "invoice": invoices,
        "existing": {dt: set(bexio_names(dt)) for dt in ("Sales Invoice", "Sales Order", "Quotation", "Delivery Note")},
        "rate_at": ecb_rate,
    }


def doctype_fields(erp, doctype):
    """The field names of a doctype, custom fields included: the meta ERPNext builds, which the agent API user may read."""
    return {f["fieldname"] for f in erp.meta(doctype)["fields"]}


def summary(results, lookups):
    """The totals of a dry run, one line per export file and per year; no names, no bexio ids."""
    lines = ["{:<17}{:>9}{:>8}{:>10}{:>13}{:>12}{:>11}".format(
        "export", "records", "mapped", "unmapped", "differences", "in ERPNext", "to create")]
    for key, doctype, _credit in DOCUMENTS:
        rows = [r for r in results if r["key"] == key]
        if not rows:
            continue
        mapped = [r for r in rows if r["doc"]]
        exists = sum(1 for r in mapped if r["bexio_id"] in lookups["existing"][doctype])
        lines.append("{:<17}{:>9}{:>8}{:>10}{:>13}{:>12}{:>11}".format(
            key, len(rows), len(mapped), len(rows) - len(mapped), sum(1 for r in mapped if r["differences"]),
            exists, len(mapped) - exists))

    per_year = collections.OrderedDict()
    for r in sorted((r for r in results if r["doc"]), key=lambda r: (r["key"], r["year"])):
        acc = per_year.setdefault((r["key"], r["year"]), [0, ZERO, ZERO, ZERO])
        acc[0] += 1
        for i, value in enumerate(r["chf"]):
            acc[i + 1] += value
    lines.append("")
    lines.append("CHF by year of the document date (net, tax, gross):")
    for (key, year), (count, net, tax, gross) in per_year.items():
        lines.append("  {:<16}{}  {:>5} docs  {:>14,.2f}{:>12,.2f}{:>14,.2f}".format(key, year, count, net, tax, gross))

    statuses = collections.Counter(str(r["record"].get("kb_item_status_id")) for r in results if r["key"] == "invoices")
    if statuses:
        lines.append("")
        lines.append("invoices by status: " + ", ".join("{}: {}".format(s, n) for s, n in sorted(statuses.items())))
    return "\n".join(lines)


def write_drafts(results, path):
    """The mapped invoices and credit notes as the loader takes them, into the private file; returns their number.

    Each draft is named by bexio's document number, which is its ERPNext name; the loader inserts it as a draft
    (docstatus 0), so nothing posts. A foreign document is in CHF already, so no exchange rate is handed over:
    the Currency Exchange records that an ECB rate needed are there from its own run.
    """
    documents = []
    for r in results:
        if r["doc"] is None or r["key"] not in APPLY_FILES:
            continue
        doc = dict(r["doc"])
        doctype = doc.pop("doctype")
        documents.append({"doctype": doctype, "name": str(r["record"]["document_nr"]), "bexio_id": r["bexio_id"],
                          "values": doc})
    write_private(path, json.dumps({"documents": documents, "exchange_rates": []}, indent=1))
    return len(documents)


def write_private(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text + "\n")


def detail_lines(results):
    """The differences and unmapped records, by bexio id, for the private report file."""
    lines = []
    for r in results:
        if r["error"]:
            lines.append("{} {}: unmapped: {}".format(r["doctype"], r["bexio_id"], r["error"]))
        elif r["differences"]:
            lines.append("{} {}: {}".format(r["doctype"], r["bexio_id"], "; ".join(r["differences"])))
        for field in r["unknown"]:
            lines.append("{} {}: ERPNext has no field {}".format(r["doctype"], r["bexio_id"], field))
    return lines


def main(argv):
    parser = argparse.ArgumentParser(description="Map the exported bexio sales documents to ERPNext (a dry run by default).")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals (the default)")
    parser.add_argument("--apply", action="store_true", help="write the invoices as drafts into ERPNext, through the loader")
    parser.add_argument("--report", default=os.path.join(im.PRIVATE, "bexio-sales-differences.txt"),
                        help="private file for the unmapped and differing records, by bexio id")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are one or the other")

    export_dir = args.export or im.newest_export()
    data = load_documents(export_dir)
    erp = im.Erp.from_file(args.token_file)
    try:
        lookups = lookups_from_erp(erp, data)
        metas = {dt: doctype_fields(erp, dt) for dt in (
            "Sales Invoice", "Sales Invoice Item", "Sales Order", "Sales Order Item",
            "Quotation", "Quotation Item", "Delivery Note", "Delivery Note Item", "Sales Taxes and Charges")}
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2

    results = plan(data, lookups, metas)
    print("sales documents from {}".format(export_dir))
    print(summary(results, lookups))
    lines = detail_lines(results)
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    if args.apply:
        path = os.path.join(im.PRIVATE, DRAFTS_FILE)
        documents = write_drafts(results, path)
        print("{} drafts handed to the loader; {} lines of differences or unmapped records in {}".format(
            documents, len(lines), args.report))
        subprocess.run(["sh", LOADER, path], check=True)
    else:
        print("dry run: nothing was written; {} lines of differences or unmapped records in {}".format(len(lines), args.report))
    unmapped = sum(1 for r in results if r["error"])
    return 1 if unmapped or lines else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
