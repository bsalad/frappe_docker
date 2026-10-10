"""Map the bexio payments on invoices to ERPNext Payment Entries (receive).

Step four of the bexio pipeline, after import_master.py and import_sales.py: each
payment row of invoice_payments.json becomes a Payment Entry of payment_type Receive,
allocated against the Sales Invoice it pays. The invoice is found by its bexio_id in
ERPNext; when erp-tvjk has not drafted it yet, the planned name is bexio's document
number, which erp-tvjk names its drafts by. Each function takes one export record and
the lookups and returns the ERPNext document as a dict, not yet inserted.

Whether a receipt posts GL and how a foreign currency is settled is the posting plan of
finance-3qsp; the live run is erp-a2ma's. So nothing is written here: --dry-run reads
ERPNext and prints totals only.

A payment that cannot be mapped raises Unmapped with a reason, and the reason is counted
in the summary. A payment larger than what its invoice still owes is unmapped, not
capped, unless the invoice owes nothing: then the excess is an advance (unallocated, on
Erhaltene Anzahlungen, as bexio books it). What an invoice owes is counted over the
mapped payments in date order, so an unmapped payment does not reduce it. A rerun maps the receipts an earlier run
loaded (submitted Payment Entries with a bexio id) from the same start as the first run, and hands none of them over.

A receipt on a foreign-currency invoice that ERPNext holds in CHF (erp-h7dj books it at
bexio's rate) is received in the CHF bexio booked to the bank for it, from the journal.
The gap to the CHF outstanding is an exchange difference, on Kursdifferenzen as a
deduction, when it is within TOLERANCE; a larger gap is left open, not booked.

The VAT that bexio moves on payment (2202 to 2200 on the receipt date) is one Journal
Entry per receipt, from the journal's own line: vat_documents. erp-fd93 must not import
those lines again: their ids go to <private> with write_plan's run.

The reasons never name a company; the bexio ids of the unmapped payments and the
reconciliation lines go to <private>, never to the screen or the repository.

Amounts are in the invoice currency. bexio's payments settle the invoice's `total`, the
gross including VAT; `total_gross` is not the gross for most VAT invoices in the export.
A foreign-currency payment on an invoice in that currency is received in CHF at the
Currency Exchange rate on or before its date (the records erp-tvjk creates).

Run it as:

    python3 finance/bexio/import_payments_in.py --dry-run [--export DIR]
    python3 finance/bexio/import_payments_in.py --write FILE [--export DIR]

--export defaults to the newest directory under <private>/bexio-export/. Standard
library only, plus import_master and import_sales.
"""

import argparse
import collections
import json
import os
import sys
from decimal import ROUND_HALF_UP, Decimal

import import_master as im
import import_sales as isl

BASE_CURRENCY = "CHF"
CENT = Decimal("0.01")
ZERO = Decimal("0")
PROBLEMS_FILE = "bexio-payments-dry-run.txt"
VAT_IDS_FILE = "bexio-vat-on-payment-ids.txt"
OVER_ALLOCATED = "payment exceeds the outstanding amount of its invoice"

# bexio account numbers the receipts use
BANK_ACCOUNTS = ("1020", "1021")
RECEIVABLE = "1100"
ADVANCE = "2030"  # Erhaltene Anzahlungen von Dritten: money received before any invoice it settles
VAT_FROM, VAT_TO = "2202", "2200"  # VAT moved on payment: Abrechnungskonto to Geschuldete MWST
# the exchange difference account of a customer receipt that is received at another CHF amount: the revenue-side
# Kursdifferenzen of the KMU chart. bexio's journal names no account for it; 4906 is for supplier payments (erp-9h1k)
EXCHANGE_DIFFERENCE = "3806"
# the CHF gap a foreign-currency receipt may leave against its invoice and still be an exchange difference
TOLERANCE = Decimal("0.05")

# bexio payment flags that are not money in a bank: each is unmapped, never booked as a receipt
NOT_A_RECEIPT = (
    ("kb_credit_voucher_id", "credit voucher offset, not a bank receipt"),
    ("is_cash_discount", "cash discount, not a bank receipt"),
    ("is_client_account_redemption", "client account redemption, not a bank receipt"),
)


class Unmapped(Exception):
    """A payment the importer does not map. The message names the reason, never a company name."""


def _dec(value):
    return Decimal(str(value))


def _cents(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _money(value):
    """A bexio amount as cents: bexio's payment values carry six decimals."""
    return _cents(_dec(value))


def invoice_total(invoice):
    """The amount the payments settle: bexio's total, the gross including VAT."""
    return _dec(invoice["total"])


def rate_on(lookups, currency, day):
    """The Currency Exchange rate to CHF on or before the day, the newest record; None when there is none."""
    if currency == BASE_CURRENCY:
        return Decimal("1")
    for rate_day, rate in lookups["exchange"].get((currency, BASE_CURRENCY), []):
        if rate_day <= day:
            return rate
    return None


def chf_in_erpnext(invoice, lookups):
    """The ERPNext invoice when it is held in CHF and bexio's is in another currency; else None."""
    if invoice is None:
        return None
    erp = lookups.get("invoice_erp", {}).get(str(invoice["id"]))
    currency = lookups["currency"].get(str(invoice.get("currency_id")))
    if erp and erp["currency"] == BASE_CURRENCY and currency != BASE_CURRENCY:
        return erp
    return None


def starting_owing(invoice, lookups):
    """What an invoice owes before any new receipt, in the currency its receipts settle it in.

    ERPNext's outstanding is what the receipts already loaded left, so their allocations go back on it: a rerun maps
    those receipts from the same start as the first run, and the new ones see only what is really left.
    """
    if invoice is None:
        return ZERO
    erp = chf_in_erpnext(invoice, lookups)
    if erp is None:
        return invoice_total(invoice)
    return erp["outstanding"] + lookups.get("loaded_allocated", {}).get(erp["name"], ZERO)


def _entry(row, customer, bank, paid_from, currency, paid, received, rate):
    """A Payment Entry of a receipt, before its references; the paid amount is in the paid_from account's currency.

    A receipt into a bank account needs a reference number and date: bexio gives none, so the receipt's bexio id and its date stand in.
    """
    return {
        "doctype": "Payment Entry", "company": im.COMPANY, "payment_type": "Receive",
        "party_type": "Customer", "party": customer, "posting_date": row["date"],
        "paid_from": paid_from, "paid_from_account_currency": currency,
        "paid_to": bank["account"], "paid_to_account_currency": BASE_CURRENCY,
        "paid_amount": float(paid), "received_amount": float(received),
        "source_exchange_rate": float(rate), "target_exchange_rate": 1.0,
        "reference_no": "bexio payment {}".format(row["id"]), "reference_date": row["date"],
        "bexio_id": str(row["id"]),
    }


def map_payment(row, invoice, lookups, owing):
    """(Payment Entry dict, CHF received) for one bexio payment row; raises Unmapped.

    `invoice` is the export record of the payment's invoice, or None; `owing` is what that
    invoice still owes, in the currency its receipts settle it in, after the payments mapped before this one.
    """
    for flag, reason in NOT_A_RECEIPT:
        if row.get(flag):
            raise Unmapped(reason)
    if invoice is None:
        raise Unmapped("the payment's invoice is not in the export")
    customer = lookups["customer"].get(str(invoice.get("contact_id")))
    if customer is None:
        raise Unmapped("the invoice's contact has no Customer")
    if not row.get("bank_account_id"):
        # the export names no bank, and a payment row carries no booking account of its own
        raise Unmapped("no bank account and no booking account in the export")
    bank = lookups["bank"].get(str(row["bank_account_id"]))
    if bank is None:
        raise Unmapped("the bank account has no ERPNext Bank Account")
    if bank["currency"] != BASE_CURRENCY:
        raise Unmapped("the bank account is not in CHF")
    erp = chf_in_erpnext(invoice, lookups)
    if erp is not None:
        return _map_chf_booked(row, erp, customer, bank, lookups, owing)
    currency = lookups["currency"].get(str(invoice.get("currency_id")))
    if currency is None:
        raise Unmapped("the invoice's currency is not in the export")
    receivable = lookups["receivable"].get(currency)
    if receivable is None:
        raise Unmapped("no receivable account in {}".format(currency))
    rate = rate_on(lookups, currency, row["date"])
    if rate is None:
        raise Unmapped("no Currency Exchange {} to CHF on or before the payment date".format(currency))
    value = _money(row["value"])
    received = _cents(value * rate)
    if value > owing:
        if owing > 0 or currency != BASE_CURRENCY:
            raise Unmapped(OVER_ALLOCATED)
        # money received before the invoice owes anything: unallocated, on 2030 as bexio books it
        advance = lookups["gl"].get(ADVANCE)
        if advance is None:
            raise Unmapped("no Erhaltene Anzahlungen account")
        return _entry(row, customer, bank, advance, currency, value, received, rate), received
    name = lookups["invoice"].get(str(invoice["id"])) or invoice.get("document_nr")
    if not name:
        raise Unmapped("the invoice has no document number")
    doc = _entry(row, customer, bank, receivable, currency, value, received, rate)
    doc["references"] = [{
        "reference_doctype": "Sales Invoice", "reference_name": name,
        "allocated_amount": float(value), "total_amount": float(invoice_total(invoice)),
        "outstanding_amount": float(owing),
    }]
    return doc, received


def _map_chf_booked(row, erp, customer, bank, lookups, owing):
    """(Payment Entry dict, CHF received) for a receipt on an invoice that ERPNext holds in CHF; raises Unmapped.

    The receipt is the CHF bexio booked to the bank for it, and it settles the invoice's CHF outstanding. A gap of
    up to TOLERANCE is an exchange difference: a deduction on Kursdifferenzen, so the invoice is paid. A larger gap
    that bexio booked to another account on the receipt (a commission, say; bank_booked_deductions) is a deduction to
    that account, so the invoice is paid too. Any other larger gap is not booked: the receipt is allocated at its own
    amount and the rest stays open, for erp-fd93 to settle.
    """
    booked = lookups.get("booked_chf", {}).get(str(row["id"]))
    if booked is None:
        raise Unmapped("no CHF booking to the bank in the journal")
    paid = _cents(booked)
    gap = owing - paid
    if gap < -TOLERANCE:
        raise Unmapped(OVER_ALLOCATED)
    difference, deduct_to = (gap, EXCHANGE_DIFFERENCE) if abs(gap) <= TOLERANCE else (ZERO, None)
    deduction = lookups.get("booked_deductions", {}).get(str(row["id"]))
    if deduction and abs(gap) > TOLERANCE and abs(deduction[2] - gap) <= CENT:
        difference, deduct_to = gap, deduction[1]
    receivable = lookups["receivable"].get(BASE_CURRENCY)
    if receivable is None:
        raise Unmapped("no receivable account in CHF")
    doc = _entry(row, customer, bank, receivable, BASE_CURRENCY, paid, paid, Decimal("1"))
    doc["references"] = [{
        "reference_doctype": "Sales Invoice", "reference_name": erp["name"],
        "allocated_amount": float(paid + difference), "total_amount": float(erp["total"]),
        "outstanding_amount": float(owing),
    }]
    if difference:
        account = lookups["gl"].get(deduct_to)
        if account is None:
            raise Unmapped("no account {} for the deduction".format(deduct_to))
        doc["deductions"] = [{"account": account, "cost_center": lookups["cost_center"], "amount": float(difference)}]
    return doc, paid


def account_ids(accounts):
    """bexio's account id by its account number."""
    return {str(a["account_no"]): a["id"] for a in accounts}


def receipt_lines(journal, row_ids):
    """The journal lines booked against a receipt: a bexio client account entry whose ref_id is a payment row of the export."""
    return [line for line in journal
            if line.get("ref_class") == "KbClientAccountEntry" and line.get("ref_id") is not None and str(line["ref_id"]) in row_ids]


def bank_booked_chf(journal, accounts, row_ids):
    """The CHF each receipt booked to a bank: payment row id -> amount, on the debit of a bank and the credit of the receivable."""
    ids = account_ids(accounts)
    banks = {ids[n] for n in BANK_ACCOUNTS if n in ids}
    booked = collections.defaultdict(lambda: ZERO)
    for line in receipt_lines(journal, row_ids):
        if line["debit_account_id"] in banks and line["credit_account_id"] == ids[RECEIVABLE]:
            booked[str(line["ref_id"])] += _dec(line["base_currency_amount"])
    return dict(booked)


def bank_booked_deductions(journal, accounts, row_ids):
    """What bexio took off each receipt beyond the bank: payment row id -> (journal line id, account number, CHF).

    A receipt line that is not the bank against the receivable, nor one against the advance, nor VAT on payment, is money
    bexio moved from the receivable to another account, a debit there and a credit on the receivable. That account is
    where the gap of such a receipt goes, as a deduction on its Payment Entry.
    """
    ids = account_ids(accounts)
    numbers = {v: str(k) for k, v in ids.items()}
    deductions = {}
    for line in left_lines(journal, accounts, row_ids):
        amount = _money(_dec(line["base_currency_amount"]))
        # a zero line moves nothing, so it is no deduction, whichever line of the receipt comes first
        if line["credit_account_id"] == ids[RECEIVABLE] and line["debit_account_id"] in numbers and amount != ZERO:
            deductions.setdefault(str(line["ref_id"]), (line["id"], numbers[line["debit_account_id"]], amount))
    return deductions


def vat_lines(journal, accounts, row_ids):
    """The journal lines that move VAT on payment, 2202 to 2200, on a receipt: every one, zero amounts included."""
    ids = account_ids(accounts)
    return [line for line in receipt_lines(journal, row_ids)
            if line["debit_account_id"] == ids[VAT_FROM] and line["credit_account_id"] == ids[VAT_TO]]


def left_lines(journal, accounts, row_ids):
    """The receipt lines this import does not book: not a bank against the receivable, not one against the advance, not VAT on payment."""
    ids = account_ids(accounts)
    banks = [ids[n] for n in BANK_ACCOUNTS if n in ids]
    covered = {(b, ids[RECEIVABLE]) for b in banks} | {(b, ids[ADVANCE]) for b in banks} | {(ids[VAT_FROM], ids[VAT_TO])}
    return [line for line in receipt_lines(journal, row_ids) if (line["debit_account_id"], line["credit_account_id"]) not in covered]


def plan(data, lookups):
    """Map every payment of the export, in date order; one result per payment row."""
    invoices = {str(i["id"]): i for i in data["invoices"]}
    rows = sorted((row for p in data["payments"] for row in p["rows"]), key=lambda r: (r["date"], r["id"]))
    row_ids = {str(row["id"]) for row in rows}
    allocated = collections.defaultdict(lambda: ZERO)
    for references in loaded_here(lookups, row_ids).values():
        for name, amount in references:
            allocated[name] += amount
    lookups = dict(lookups, loaded_allocated=dict(allocated))
    if "journal" in data:
        lookups = dict(lookups, booked_chf=bank_booked_chf(data["journal"], data["accounts"], row_ids),
                       booked_deductions=bank_booked_deductions(data["journal"], data["accounts"], row_ids))
    owing = {}  # bexio invoice id -> what it still owes, after the mapped payments
    results = []
    for row in rows:
        key = str(row["kb_invoice_id"])
        invoice = invoices.get(key)
        remaining = owing.get(key, starting_owing(invoice, lookups))
        result = {"bexio_id": str(row["id"]), "invoice": key, "year": row["date"][:4],
                  "invoice_name": (lookups["invoice"].get(key) or (invoice or {}).get("document_nr")),
                  "currency": lookups["currency"].get(str(invoice.get("currency_id"))) if invoice else None,
                  "value": _money(row["value"]), "doc": None, "chf": None, "error": None}
        try:
            doc, received = map_payment(row, invoice, lookups, remaining)
        except Unmapped as err:
            result["error"] = str(err)
        else:
            settled = sum((_dec(ref["allocated_amount"]) for ref in doc.get("references", [])), ZERO)
            owing[key] = remaining - settled
            result.update(doc=doc, chf=received)
            deduction = lookups.get("booked_deductions", {}).get(result["bexio_id"])
            if deduction and doc.get("deductions") and doc["deductions"][0]["account"] == lookups["gl"].get(deduction[1]):
                # the journal line this deduction books: not left for erp-fd93 to import a second time
                result["journal_line"] = deduction[0]
            if chf_in_erpnext(invoice, lookups) and remaining - settled > TOLERANCE:
                # a gap beyond TOLERANCE is not booked here: the invoice stays open, listed for erp-fd93
                result["note"] = "CHF {} of the invoice stays open after this receipt; not an exchange difference".format(
                    remaining - settled)
        results.append(result)
    return results


def vat_document(line, result, gl):
    """The Journal Entry that moves a receipt's VAT from 2202 to 2200 on the receipt date; the line's own id is its bexio id."""
    amount = _cents(_dec(line["base_currency_amount"]))
    debit, credit = (VAT_FROM, VAT_TO) if amount > 0 else (VAT_TO, VAT_FROM)
    value = float(abs(amount))
    return {"doctype": "Journal Entry", "name": None, "bexio_id": str(line["id"]), "values": {
        "company": im.COMPANY, "voucher_type": "Journal Entry", "bexio_id": str(line["id"]),
        "posting_date": result["doc"]["posting_date"],
        "user_remark": "VAT on payment: bexio receipt {} on {}".format(result["bexio_id"], result["invoice_name"]),
        "accounts": [
            {"account": gl[debit], "debit_in_account_currency": value},
            {"account": gl[credit], "credit_in_account_currency": value},
        ],
    }}


def write_plan(results, submitted, vat=(), gl=None, loaded=()):
    """The mapped payments and their VAT moves as the loader takes them, and the bexio ids of those whose invoice is not submitted in ERPNext.

    A Payment Entry is named by ERPNext's own series (ACC-PAY-), so its name is None; it is keyed by bexio_id.
    A payment is allocated against a submitted Sales Invoice only: one whose invoice is still a draft (or not
    in ERPNext) is skipped and counted, so the loader never books against an invoice that has no GL yet. A receipt
    with no invoice to settle (an advance) is handed over with its invoice's submission as the same test.
    A receipt whose bexio id is in `loaded` is already in ERPNext from an earlier run: it is neither handed over nor skipped.
    The VAT lines of the receipts handed over become Journal Entries (vat_document); a zero line is none.
    """
    documents, skipped, handed = [], [], {}
    for r in results:
        if r["doc"] is None or r["bexio_id"] in loaded:
            continue
        if r["invoice_name"] not in submitted:
            skipped.append(r["bexio_id"])
            continue
        values = dict(r["doc"])
        values.pop("doctype")
        documents.append({"doctype": "Payment Entry", "name": None, "bexio_id": r["bexio_id"], "values": values})
        handed[r["bexio_id"]] = r
    for line in vat:
        r = handed.get(str(line["ref_id"]))
        if r is None or _cents(_dec(line["base_currency_amount"])) == ZERO:
            continue
        documents.append(vat_document(line, r, gl))
    return documents, skipped


def reconciliation(results, data):
    """Per invoice with a mapped payment: the mapped amount against bexio's own received total, where they differ."""
    invoices = {str(i["id"]): i for i in data["invoices"]}
    mapped = collections.defaultdict(lambda: ZERO)
    for r in results:
        if r["doc"] and r["doc"].get("references"):  # an advance settles no invoice
            mapped[r["invoice"]] += r["value"]
    lines = []
    for key in sorted(mapped):
        have = _dec(invoices[key]["total_received_payments"])
        if have != mapped[key]:
            lines.append("invoice {}: mapped {} against bexio's received {}".format(key, mapped[key], have))
    return lines


def unknown_fields(doc, metas):
    """Fields of the Payment Entry and its references that ERPNext's doctype does not have."""
    found = ["Payment Entry.{}".format(k) for k in doc if k not in ("doctype", "references", "deductions") and k not in metas["Payment Entry"]]
    for ref in doc.get("references", []):
        found.extend("Payment Entry Reference.{}".format(k) for k in ref if k not in metas["Payment Entry Reference"])
    for ded in doc.get("deductions", []):
        found.extend("Payment Entry Deduction.{}".format(k) for k in ded if k not in metas["Payment Entry Deduction"])
    return sorted(set(found))


def loaded_allocations(erp):
    """The submitted receipts this loader made: bexio payment row id -> [(invoice name, allocated amount)], read only.

    An advance has no reference, so its list is empty; it is still a receipt already loaded. One GET per entry: the
    references are a child table of the Payment Entry.
    """
    entries = erp.list("Payment Entry", [["docstatus", "=", 1], ["payment_type", "=", "Receive"], ["bexio_id", "is", "set"]],
                       ["name", "bexio_id"])
    loaded = {}
    for entry in entries:
        refs = erp.get("Payment Entry", entry["name"]).get("references", [])
        loaded[entry["bexio_id"]] = [(ref["reference_name"], _dec(ref["allocated_amount"]))
                                     for ref in refs if ref["reference_doctype"] == "Sales Invoice"]
    return loaded


def loaded_here(lookups, row_ids):
    """The receipts already loaded that are payment rows of this export: bexio payment row id -> [(invoice name, allocated)]."""
    return {k: v for k, v in lookups.get("loaded", {}).items() if k in row_ids}


def lookups_from_erp(erp, data):
    """What the mapping needs from ERPNext, read only: customers, bank accounts, accounts, rates, invoices."""
    def bexio_names(doctype):
        return {r["bexio_id"]: r["name"] for r in erp.list(doctype, [["bexio_id", "is", "set"]], ["name", "bexio_id"])}

    currency = {str(c["id"]): c["name"] for c in data["currencies"]}
    gl_bank = {r["bexio_id"]: r["account"] for r in erp.list("Bank Account", [["bexio_id", "is", "set"]], ["name", "bexio_id", "account"])}
    bank = {}
    for b in data["bank_accounts"]:
        if str(b["id"]) in gl_bank:
            bank[str(b["id"])] = {"account": gl_bank[str(b["id"])], "currency": currency.get(str(b["currency_id"]))}
    accounts = erp.list("Account", [["company", "=", im.COMPANY], ["is_group", "=", 0]], ["name", "account_number", "account_type", "account_currency"])
    # the receivable of a currency: the lowest-named one (1100 for CHF; the KMU chart has 1102 as a second CHF one)
    receivable = {}
    for r in sorted((a for a in accounts if a["account_type"] == "Receivable"), key=lambda r: r["name"]):
        receivable.setdefault(r["account_currency"], r["name"])
    exchange = collections.defaultdict(list)
    for r in erp.list("Currency Exchange", [], ["from_currency", "to_currency", "date", "exchange_rate"]):
        exchange[(r["from_currency"], r["to_currency"])].append((str(r["date"]), _dec(r["exchange_rate"])))
    for rates in exchange.values():
        rates.sort(reverse=True)
    centers = [c["name"] for c in erp.list("Cost Center", [["company", "=", im.COMPANY], ["is_group", "=", 0]], ["name"])]
    return {
        "currency": currency,
        "loaded": loaded_allocations(erp),
        "customer": bexio_names("Customer"),
        "invoice": bexio_names("Sales Invoice"),
        "invoice_erp": {r["bexio_id"]: {"name": r["name"], "currency": r["currency"], "total": _dec(r["grand_total"]),
                                        "outstanding": _dec(r["outstanding_amount"])}
                        for r in erp.list("Sales Invoice", [["bexio_id", "is", "set"]],
                                          ["name", "bexio_id", "currency", "grand_total", "outstanding_amount"])},
        "submitted": {r["name"] for r in erp.list("Sales Invoice", [["docstatus", "=", 1]], ["name"])},
        "bank": bank,
        "receivable": receivable,
        "gl": {a["account_number"]: a["name"] for a in accounts},
        "cost_center": centers[0] if len(centers) == 1 else None,
        "exchange": dict(exchange),
    }


def load_payments(path):
    """The export's payments, invoices, bank accounts, currencies, accounts and journal."""
    data = {}
    for name in ("invoice_payments", "invoices", "bank_accounts", "currencies", "accounts", "journal"):
        with open(os.path.join(path, name + ".json"), encoding="utf-8") as f:
            data[name] = json.load(f)
    data["payments"] = data.pop("invoice_payments")
    return data


def summary(results):
    """Totals per year of the payment date, and the reasons; no names, no bexio ids."""
    lines = ["{:<8}{:>10}{:>8}{:>10}{:>18}".format("year", "payments", "mapped", "unmapped", "CHF received")]
    per_year = collections.OrderedDict()
    for r in results:
        acc = per_year.setdefault(r["year"], [0, 0, ZERO])
        acc[0] += 1
        if r["doc"]:
            acc[1] += 1
            acc[2] += r["chf"]
    for year, (count, mapped, chf) in per_year.items():
        lines.append("{:<8}{:>10}{:>8}{:>10}{:>18,.2f}".format(year, count, mapped, count - mapped, chf))
    lines.append("{:<8}{:>10}{:>8}{:>10}{:>18,.2f}".format("total", len(results), sum(a[1] for a in per_year.values()),
                                                           sum(a[0] - a[1] for a in per_year.values()), sum(a[2] for a in per_year.values())))

    unmapped = [r for r in results if r["error"]]
    if unmapped:
        lines.append("")
        lines.append("unmapped by reason:")
        for reason, count in collections.Counter(r["error"] for r in unmapped).most_common():
            lines.append("  {:>4}  {}".format(count, reason))
        # not converted: the amounts stay in the invoice's currency, so nothing is hidden in CHF
        lines.append("unmapped, in the invoice's currency, by year:")
        by_currency = collections.OrderedDict()
        for r in unmapped:
            key = (r["year"], r["currency"] or "?")
            by_currency[key] = by_currency.get(key, ZERO) + r["value"]
        for (year, currency), value in by_currency.items():
            lines.append("  {:<8}{:<5}{:>16,.2f}".format(year, currency, value))
    return "\n".join(lines)


def detail_lines(results, reconciled):
    """The unmapped payments by bexio id, and the reconciliation lines, for the private report file."""
    lines = ["payment {} (invoice {}): unmapped: {}".format(r["bexio_id"], r["invoice"], r["error"]) for r in results if r["error"]]
    lines += ["payment {} (invoice {}): {}".format(r["bexio_id"], r["invoice"], r["note"]) for r in results if r.get("note")]
    return lines + reconciled


def main(argv):
    parser = argparse.ArgumentParser(description="Map the bexio payments on invoices to ERPNext Payment Entries.")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals")
    parser.add_argument("--write", metavar="FILE", default=None,
                        help="write the Payment Entries and their VAT Journal Entries to a private file for the loader "
                             "(bexio-drafts.sh FILE submit); nothing goes into ERPNext here. Stops, writing nothing, if a "
                             "payment would over-allocate")
    parser.add_argument("--report", default=os.path.join(im.PRIVATE, PROBLEMS_FILE),
                        help="private file for the unmapped payments and the reconciliation, by bexio id")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if args.dry_run == (args.write is not None):
        parser.error("exactly one of --dry-run and --write FILE")

    export_dir = args.export or im.newest_export()
    data = load_payments(export_dir)
    erp = im.Erp.from_file(args.token_file)
    try:
        lookups = lookups_from_erp(erp, data)
        metas = {dt: isl.doctype_fields(erp, dt) for dt in ("Payment Entry", "Payment Entry Reference", "Payment Entry Deduction")}
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2

    results = plan(data, lookups)
    for r in results:
        if r["doc"]:
            r["unknown"] = unknown_fields(r["doc"], metas)
    lines = detail_lines(results, reconciliation(results, data))
    row_ids = {r["bexio_id"] for r in results}
    covered = {r["journal_line"] for r in results if r.get("journal_line")}
    lines += ["journal line {} on receipt {}: not booked here, for erp-fd93: {} CHF".format(line["id"], line["ref_id"], line["base_currency_amount"])
              for line in left_lines(data["journal"], data["accounts"], row_ids) if line["id"] not in covered]
    lines += ["journal line {} on receipt {}: booked as the deduction of its Payment Entry; erp-fd93 must not import it".format(
        r["journal_line"], r["bexio_id"]) for r in results if r.get("journal_line")]
    lines += ["Payment Entry {}: ERPNext has no field {}".format(r["bexio_id"], f)
              for r in results if r["doc"] for f in r["unknown"]]
    print("payments from {}".format(export_dir))
    print(summary(results))
    fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    over = [r["bexio_id"] for r in results if r["error"] == OVER_ALLOCATED]
    if args.write and over:
        # the rule of the live run: a payment that would over-allocate stops the whole run, nothing partial
        print("stop: {} payment(s) would over-allocate their invoice, by bexio id: {}; nothing written".format(
            len(over), ", ".join(over)))
        return 1
    if args.write:
        vat = vat_lines(data["journal"], data["accounts"], row_ids)
        here = loaded_here(lookups, row_ids)
        documents, skipped = write_plan(results, lookups["submitted"], vat, lookups["gl"], loaded=set(here))
        isl.write_private(args.write, json.dumps({"documents": documents, "exchange_rates": []}, indent=1))
        loaded = {d["bexio_id"] for d in documents if d["doctype"] == "Payment Entry"}
        # the VAT lines of every receipt mapped on a submitted invoice, handed over now or loaded by an earlier run, zero ones
        # too: erp-fd93 must not import these again, and a rerun hands none over, so the file must not shrink to nothing
        kept = {r["bexio_id"] for r in results if r["doc"] and r["invoice_name"] in lookups["submitted"]}
        ids = [line for line in vat if str(line["ref_id"]) in kept]
        isl.write_private(os.path.join(im.PRIVATE, VAT_IDS_FILE), "\n".join(
            "journal {} receipt {}".format(line["id"], line["ref_id"]) for line in ids))
        journal_entries = sum(d["doctype"] == "Journal Entry" for d in documents)
        print("{} documents handed to the loader: {} Payment Entries, {} VAT Journal Entries; {} VAT lines of "
              "{} receipts in {}; {} skipped: invoice not submitted in ERPNext, by bexio id: {}; {} already loaded by an "
              "earlier run, not handed over".format(
                  len(documents), len(loaded), journal_entries, len(ids), len(kept), VAT_IDS_FILE,
                  len(skipped), ", ".join(skipped) or "none", len(kept & set(here))))
        print("{} lines of unmapped payments or reconciliation in {}".format(len(lines), args.report))
    else:
        print("dry run: nothing was written; {} lines of unmapped payments or reconciliation in {}".format(len(lines), args.report))
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
