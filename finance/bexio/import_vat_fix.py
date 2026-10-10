"""Move the VAT of the imported history onto bexio's transitory accounts, with Journal Entries keyed by bexio ids.

The sales and purchase loaders first booked the VAT where bexio moves it on payment (2200, 1171 and 1170), not
where bexio books it: 2202 at the invoice date, 1172 at the bill date. The submitted documents are not changed or
cancelled. Each correction is a Journal Entry on the document's date, built from bexio's own journal lines, so
ERPNext's VAT accounts come to equal bexio's, document by document:

- invoice: a Sales Invoice's VAT (bexio's KbInvoice lines, 2202) at the invoice date. key vatfix-invoice-<bexio id>;
  vatfix-credit-<bexio id> for a credit note
- bill: a Purchase Invoice's VAT (bexio's KbBill lines: 1172, and on a reverse-charge bill 2202 and 2203) at the bill
  date. key vatfix-bill-<bill uuid>
- payment VAT: the VAT bexio moves when a bill is paid (1171 or 1170 against 1172, and 2202 against 2203), one Journal
  Entry per bexio journal line, on the payment date. key = the journal line id
- rounding: ERPNext rounds a CHF bill to the rappen step, so a bill can stay open by a cent, or a payment stay
  unallocated by a cent. The gap goes to 6940 against the supplier's payable. key rounding-bill-<bill uuid> or
  rounding-payment-<payment uuid>

A correction takes exactly the difference between bexio's net amount and ERPNext's on each VAT account of its
document, so a document that bexio books the same way is left alone, and a second run inserts nothing: a finished
document has no difference left, and the loader finds each entry by its bexio id. A document whose correction does
not balance is not written; it is listed by bexio id.

The dry run reads ERPNext and prints totals only, per year and account: bexio's journal, ERPNext as it is, the
corrections, and ERPNext after them. The live run is --write FILE, which hands the Journal Entries to the loader
(finance/scripts/bexio-drafts.sh FILE submit). Nothing goes into ERPNext here.

    python3 finance/bexio/import_vat_fix.py --dry-run [--export DIR]
    python3 finance/bexio/import_vat_fix.py --write FILE [--export DIR]

--export defaults to the newest directory under <private>/bexio-export/. Standard library only, apart from
import_master and import_purchase. The bexio ids of what is not written go to <private>, never to the screen.
"""

import argparse
import collections
import json
import os
import sys
from decimal import Decimal, ROUND_HALF_UP

import import_master as im
import import_purchase as ip

CENT = Decimal("0.01")
ZERO = Decimal("0")
PAYABLE = "2000"            # Verbindlichkeiten aus Lieferungen und Leistungen: what a bill settles
ROUNDING_ACCOUNT = "6940"   # Bankspesen: where a bill's rounding goes, as the payments of erp-9h1k book it
SALES_ROUNDING_ACCOUNT = "6945"  # Rundungsdifferenzen Ertrag: where a sales invoice's rappen of VAT goes, as the posting plan has it
# the accounts a VAT correction may touch: sales VAT on 2202 (2200 before), purchase VAT on 1172 (1170 and 1171 before
# payment), and the reverse-charge pair 2202 and 2203
VAT_ACCOUNTS = ("1170", "1171", "1172", "2200", "2202", "2203")
# the VAT bexio moves when a bill is paid, (debit, credit): the same pairs as import_payments_out.VAT_MOVES
VAT_MOVES = (("1170", "1172"), ("1171", "1172"), ("2202", "2203"))
# the bexio journal lines of a document: an invoice, a bill, a credit voucher, a bill payment or a receipt. The others
# are the bank and manual entries, the banking entries and the VAT settlements, which are not documents
DOCUMENT_CLASSES = ("KbInvoice", "KbBill", "KbCreditVoucher", "KbClientAccountEntry")
# the accounts the check compares, per year, with bexio's journal
CHECK_ACCOUNTS = ("2200", "2202", "1170", "1171", "1172", "2203", "6940", "2000", "1100", "1020")
# what a bill or a payment may be off by and still be a rounding (the rappen of ERPNext's rounded total)
ROUNDING_MAX = Decimal("0.05")
CORRECTION_PREFIXES = ("vatfix-invoice-", "vatfix-credit-", "vatfix-bill-")
PROBLEMS_FILE = "bexio-vat-fix-problems.txt"
DETAILS_FILE = "bexio-vat-fix-documents.txt"
VAT_IDS_FILE = "bexio-vat-on-payment-out-ids.txt"


class Lookups:
    """What the correction reads from ERPNext: the submitted documents by bexio id, the accounts by number, the VAT
    GL of each document, the GL of the check accounts per year, and the bexio ids of the Journal Entries loaded."""

    def __init__(self, accounts, sales, bills, payments, gl_vat, gl_check, loaded, gl_correction=None):
        self.accounts = accounts        # account number -> Account name of the company
        self.sales = sales              # submitted Sales Invoices with a bexio_id: name, bexio_id, posting_date
        self.bills = bills              # submitted Purchase Invoices with a bexio_id: name, bexio_id, posting_date, supplier, outstanding_amount
        self.payments = payments        # submitted Payment Entries (pay) with a bexio_id: name, bexio_id, posting_date, party, unallocated_amount
        self.gl_vat = gl_vat            # voucher name -> {account number: debit minus credit}, on the VAT accounts
        self.gl_check = gl_check        # account number -> year -> debit minus credit, on the CHECK_ACCOUNTS, every voucher
        self.loaded = set(loaded)       # bexio ids of the submitted Journal Entries of an earlier run
        self.gl_correction = gl_correction or {}  # (kind, document key) -> {account number: debit minus credit} of its correction entries

    @classmethod
    def from_erp(cls, erp):
        company = [["company", "=", im.COMPANY]]
        accounts = erp.list("Account", company + [["is_group", "=", 0]], ["name", "account_number"])
        names = {r["name"]: r["account_number"] for r in accounts}
        sales = erp.list("Sales Invoice", company + [["docstatus", "=", 1], ["bexio_id", "is", "set"]],
                         ["name", "bexio_id", "posting_date"])
        bills = erp.list("Purchase Invoice", company + [["docstatus", "=", 1], ["bexio_id", "is", "set"]],
                         ["name", "bexio_id", "posting_date", "supplier", "outstanding_amount"])
        payments = erp.list("Payment Entry", company + [["payment_type", "=", "Pay"], ["docstatus", "=", 1],
                                                        ["bexio_id", "is", "set"]],
                            ["name", "bexio_id", "posting_date", "party", "unallocated_amount"])
        entries = erp.list("Journal Entry", company + [["docstatus", "=", 1], ["bexio_id", "is", "set"]], ["name", "bexio_id"])
        corrections = {r["name"]: correction_key(r["bexio_id"]) for r in entries if correction_key(r["bexio_id"])}
        vat_names = [n for n, number in names.items() if number in VAT_ACCOUNTS]
        check_names = [n for n, number in names.items() if number in CHECK_ACCOUNTS]
        gl_vat = collections.defaultdict(lambda: collections.defaultdict(lambda: ZERO))
        for row in erp.list("GL Entry", company + [["is_cancelled", "=", 0], ["account", "in", vat_names],
                                                   ["voucher_type", "in", ["Sales Invoice", "Purchase Invoice"]]],
                            ["voucher_no", "account", "debit", "credit"]):
            gl_vat[row["voucher_no"]][names[row["account"]]] += _money(row["debit"]) - _money(row["credit"])
        gl_correction = collections.defaultdict(lambda: collections.defaultdict(lambda: ZERO))
        for row in erp.list("GL Entry", company + [["is_cancelled", "=", 0], ["account", "in", vat_names],
                                                   ["voucher_type", "=", "Journal Entry"]],
                            ["voucher_no", "account", "debit", "credit"]):
            key = corrections.get(row["voucher_no"])
            if key is not None:
                gl_correction[key][names[row["account"]]] += _money(row["debit"]) - _money(row["credit"])
        gl_check = collections.defaultdict(lambda: collections.defaultdict(lambda: ZERO))
        for row in erp.list("GL Entry", company + [["is_cancelled", "=", 0], ["account", "in", check_names]],
                            ["account", "debit", "credit", "posting_date"]):
            gl_check[names[row["account"]]][row["posting_date"][:4]] += _money(row["debit"]) - _money(row["credit"])
        return cls(
            accounts={r["account_number"]: r["name"] for r in accounts},
            sales=sales,
            bills=bills,
            payments=payments,
            gl_vat=gl_vat,
            gl_check=gl_check,
            loaded={r["bexio_id"] for r in entries},
            gl_correction=gl_correction,
        )


def _money(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def correction_key(bexio_id):
    """The document a VAT correction entry belongs to, as (kind, key), from its bexio id (None for any other entry):
    vatfix-invoice-7 is ('invoice', '7'), vatfix-credit-4 is ('credit', '4'), vatfix-bill-<uuid> is ('bill', '<uuid>').
    The kind is part of the key: a sales invoice and a credit note can carry the same bexio number."""
    for prefix in CORRECTION_PREFIXES:
        if bexio_id.startswith(prefix):
            return prefix[len("vatfix-"):-1], bexio_id[len(prefix):]
    return None


def _plus(*accounts):
    """The sum of several {account number: amount} dicts."""
    total = collections.defaultdict(lambda: ZERO)
    for part in accounts:
        for number, amount in part.items():
            total[number] += amount
    return dict(total)


def account_numbers(accounts):
    """bexio's account id -> account number."""
    return {str(a["id"]): str(a["account_no"]) for a in accounts}


def bexio_vat(journal, numbers, ref_class, key):
    """What bexio's journal books on the VAT accounts per document: {document key: {account number: debit minus credit}}.

    The key is the line's ref_id for an invoice and a credit voucher, its ref_uuid for a bill. A line with no
    document (a carried forward balance, a banking entry) is not a document's VAT.
    """
    net = collections.defaultdict(lambda: collections.defaultdict(lambda: ZERO))
    for line in journal:
        if line.get("ref_class") != ref_class or line.get(key) is None:
            continue
        amount = Decimal(str(line["base_currency_amount"]))
        debit, credit = numbers.get(str(line["debit_account_id"])), numbers.get(str(line["credit_account_id"]))
        if debit in VAT_ACCOUNTS:
            net[str(line[key])][debit] += amount
        if credit in VAT_ACCOUNTS:
            net[str(line[key])][credit] -= amount
    return {k: {acc: _money(v) for acc, v in accounts.items() if _money(v) != ZERO} for k, accounts in net.items()}


def payment_moves(journal, numbers):
    """The VAT bexio moves on payment, one entry per journal line: (line id, date, debit number, credit number, amount, bill uuid).

    The line is one of VAT_MOVES, from a bill payment (KbClientAccountEntry with a uuid). A line of amount zero is kept
    in the list (erp-fd93 must not import it again) but books nothing.
    """
    moves = []
    for line in journal:
        if line.get("ref_class") != "KbClientAccountEntry" or not line.get("ref_uuid"):
            continue
        pair = (numbers.get(str(line["debit_account_id"])), numbers.get(str(line["credit_account_id"])))
        if pair in VAT_MOVES:
            moves.append((line["id"], line["date"][:10], pair[0], pair[1],
                          _money(line["base_currency_amount"]), line["ref_uuid"]))
    return moves


def fix_lines(want, have):
    """The corrections that take ERPNext's VAT of a document to bexio's: {account number: debit minus credit}, and
    whether they balance. Only the accounts that differ are in the result."""
    lines = {}
    for number in set(want) | set(have):
        diff = _money(want.get(number, ZERO) - have.get(number, ZERO))
        if diff != ZERO:
            lines[number] = diff
    return lines, sum(lines.values(), ZERO) == ZERO


def je_accounts(lines, gl, party=None, reference=None):
    """The Journal Entry rows of a set of corrections, in account number order. A payable row carries the party, and
    the reference to its bill when given, so the bill's outstanding moves with it."""
    rows = []
    for number in sorted(lines):
        amount = lines[number]
        row = {"account": gl[number]}
        if amount > ZERO:
            row["debit_in_account_currency"] = float(amount)
        else:
            row["credit_in_account_currency"] = float(-amount)
        if party is not None and number == PAYABLE:
            row.update(party_type="Supplier", party=party)
            if reference is not None:
                row.update(reference_type="Purchase Invoice", reference_name=reference)
        rows.append(row)
    return rows


def journal_document(key, day, remark, rows):
    """A Journal Entry for the loader: keyed by its bexio id, named by ERPNext's own series (ACC-JV-)."""
    return {"doctype": "Journal Entry", "name": None, "bexio_id": key, "values": {
        "company": im.COMPANY, "voucher_type": "Journal Entry", "bexio_id": key,
        "posting_date": day, "user_remark": remark, "accounts": rows,
    }}


def document_fixes(found, want, lookups, problems, details, kind):
    """The VAT corrections of the submitted invoices of one kind, and the bexio keys of those found in ERPNext.

    found: the Sales Invoices or the Purchase Invoices. want(doc) gives the document's key, the kind of its correction
    (invoice, credit or bill) and the bexio VAT of the document, by account number. A document with no difference gets
    none. Each document's bexio VAT, ERPNext's and the correction go to details, by bexio id, for <private>.
    """
    documents, keys = [], set()
    for doc in found:
        key, correction, remark, bexio_vat_of = want(doc)
        keys.add(key)
        # ERPNext's VAT of the document: its own GL, and the correction entries an earlier run submitted
        have = _plus(lookups.gl_vat.get(doc["name"], {}), lookups.gl_correction.get((correction, key), {}))
        lines, balanced = fix_lines(bexio_vat_of, have)
        if not balanced and kind == "sales" and abs(sum(lines.values(), ZERO)) <= ROUNDING_MAX:
            # the sales importer books a document's VAT to the rappen as ERPNext computes it, so bexio's VAT can differ by
            # a rappen; the rappen goes to the sales rounding account, and the VAT itself is bexio's
            lines[SALES_ROUNDING_ACCOUNT] = -sum(lines.values(), ZERO)
            balanced = True
        details.append("{} {} ({}): bexio {} ERPNext {} correction {}".format(
            kind, doc["bexio_id"], doc["posting_date"], _listed(bexio_vat_of), _listed(have), _listed(lines)))
        if not lines:
            continue
        if not balanced:
            problems.append((kind, doc["bexio_id"], "the VAT correction does not balance: {}".format(_listed(lines))))
            continue
        documents.append(journal_document("vatfix-{}-".format(correction) + key, doc["posting_date"],
                                          remark + " " + doc["bexio_id"], je_accounts(lines, lookups.accounts)))
    return documents, keys


def _listed(accounts):
    """{account number: amount} as text, by account number."""
    return "{" + ", ".join("{}: {}".format(n, accounts[n]) for n in sorted(accounts)) + "}" if accounts else "{}"


def plan(data, lookups):
    """The Journal Entries that take ERPNext's VAT to bexio's, and what is not written.

    Returns (documents, problems, listing): documents for the loader; problems (kind, bexio id, reason) for <private>;
    listing, the journal line ids of the payment VAT, zero ones too, for erp-fd93.
    """
    journal, numbers = data["journal"], account_numbers(data["accounts"])
    problems = []
    invoice_vat = bexio_vat(journal, numbers, "KbInvoice", "ref_id")
    credit_vat = bexio_vat(journal, numbers, "KbCreditVoucher", "ref_id")
    bill_vat = bexio_vat(journal, numbers, "KbBill", "ref_uuid")

    def sales_document(doc):
        if doc["bexio_id"].startswith("credit-"):
            key = doc["bexio_id"][len("credit-"):]
            return key, "credit", "VAT on the transitory account: credit note", credit_vat.get(key, {})
        return doc["bexio_id"], "invoice", "VAT on the transitory account: invoice", invoice_vat.get(doc["bexio_id"], {})

    def purchase_document(doc):
        return doc["bexio_id"], "bill", "VAT on the transitory account: bill", bill_vat.get(doc["bexio_id"], {})

    details = []
    documents, sales_keys = document_fixes(lookups.sales, sales_document, lookups, problems, details, "sales")
    bill_documents, bill_keys = document_fixes(lookups.bills, purchase_document, lookups, problems, details, "purchase")
    documents += bill_documents
    # bexio books VAT on a document that ERPNext does not hold as a submitted one: nothing is booked for it here
    found_sales = {d["bexio_id"] for d in lookups.sales}
    for key in invoice_vat:
        if key not in found_sales:
            problems.append(("sales", key, "bexio invoice with VAT is not a submitted Sales Invoice in ERPNext"))
    for key in credit_vat:
        if "credit-" + key not in found_sales:
            problems.append(("sales", "credit-" + key, "bexio credit note with VAT is not a submitted Sales Invoice in ERPNext"))
    for key in bill_vat:
        if key not in bill_keys:
            problems.append(("purchase", key, "bexio bill with VAT is not a submitted Purchase Invoice in ERPNext"))
    # the VAT bexio moves when a bill is paid: one Journal Entry per journal line
    listing = []
    for line_id, day, debit, credit, amount, uuid in payment_moves(journal, numbers):
        listing.append("journal {} group {}: booked by a VAT Journal Entry of import_vat_fix".format(line_id, uuid))
        if amount == ZERO or str(line_id) in lookups.loaded:
            continue
        debit_no, credit_no = (debit, credit) if amount > ZERO else (credit, debit)
        lines = {debit_no: abs(amount), credit_no: -abs(amount)}
        documents.append(journal_document(str(line_id), day, "VAT on payment: bexio bill payment group {}".format(uuid),
                                          je_accounts(lines, lookups.accounts)))
    # rounding: a bill open by a rappen, or a payment unallocated by one; the supplier's payable is taken to zero
    for bill in lookups.bills:
        owing = _money(bill["outstanding_amount"])
        if owing == ZERO or "rounding-bill-" + bill["bexio_id"] in lookups.loaded:
            continue
        if ZERO < owing <= ROUNDING_MAX:
            lines = {PAYABLE: owing, ROUNDING_ACCOUNT: -owing}
            documents.append(journal_document(
                "rounding-bill-" + bill["bexio_id"], bill["posting_date"],
                "rounding of bexio bill {}: ERPNext's rounded total".format(bill["bexio_id"]),
                je_accounts(lines, lookups.accounts, party=bill["supplier"], reference=bill["name"])))
        else:
            problems.append(("purchase", bill["bexio_id"], "open {} in ERPNext, not a rounding".format(owing)))
    for payment in lookups.payments:
        left = _money(payment["unallocated_amount"])
        # a rounding entry does not allocate the payment: it stays unallocated, so an earlier run's entry is looked up by its key
        if left == ZERO or "rounding-payment-" + payment["bexio_id"] in lookups.loaded:
            continue
        if ZERO < left <= ROUNDING_MAX:
            lines = {ROUNDING_ACCOUNT: left, PAYABLE: -left}
            documents.append(journal_document(
                "rounding-payment-" + payment["bexio_id"], payment["posting_date"],
                "rounding of bexio payment {}: unallocated on the Payment Entry".format(payment["bexio_id"]),
                je_accounts(lines, lookups.accounts, party=payment["party"])))
        else:
            problems.append(("payment", payment["bexio_id"], "unallocated {} in ERPNext, not a rounding".format(left)))
    return documents, problems, listing, details


def correction_totals(documents, accounts):
    """The corrections per year and account number: {(year, account number): debit minus credit}."""
    numbers = {name: number for number, name in accounts.items()}
    totals = collections.defaultdict(lambda: ZERO)
    for document in documents:
        year = document["values"]["posting_date"][:4]
        for row in document["values"]["accounts"]:
            amount = _money(row.get("debit_in_account_currency", 0) - row.get("credit_in_account_currency", 0))
            totals[(year, numbers[row["account"]])] += amount
    return totals


def bexio_totals(journal, numbers, documents):
    """bexio's journal per year and account on the CHECK_ACCOUNTS, debit minus credit: the lines of the documents
    (documents=True), or the others, the bank and manual entries that ERPNext does not hold yet. A carried forward
    balance is a line on 9100 and is left out: it copies the balance the earlier postings already make."""
    totals = collections.defaultdict(lambda: ZERO)
    for line in journal:
        debit, credit = numbers.get(str(line["debit_account_id"])), numbers.get(str(line["credit_account_id"]))
        if "9100" in (debit, credit):
            continue
        if (line.get("ref_class") in DOCUMENT_CLASSES) != documents:
            continue
        amount = _money(line["base_currency_amount"])
        year = line["date"][:4]
        if debit in CHECK_ACCOUNTS:
            totals[(year, debit)] += amount
        if credit in CHECK_ACCOUNTS:
            totals[(year, credit)] -= amount
    return totals


def summary(journal, numbers, lookups, documents):
    """Per year and account: bexio's documents and bexio's other lines (bank and manual, not in ERPNext yet), ERPNext
    now, the corrections, ERPNext after them, and the difference left against the documents."""
    docs = bexio_totals(journal, numbers, True)
    other = bexio_totals(journal, numbers, False)
    fixed = correction_totals(documents, lookups.accounts)
    years = sorted({year for year, _ in docs} | {year for per_year in lookups.gl_check.values() for year in per_year})
    lines = ["{:<6}{:<6}{:>14}{:>14}{:>14}{:>14}{:>14}{:>14}".format(
        "year", "acc", "bexio docs", "bexio other", "erpnext now", "corrections", "erpnext after", "after - docs")]
    for number in CHECK_ACCOUNTS:
        for year in years:
            now = lookups.gl_check.get(number, {}).get(year, ZERO)
            fix = fixed.get((year, number), ZERO)
            want = docs.get((year, number), ZERO)
            rest = other.get((year, number), ZERO)
            if not (now or fix or want or rest):
                continue
            lines.append("{:<6}{:<6}{:>14,.2f}{:>14,.2f}{:>14,.2f}{:>14,.2f}{:>14,.2f}{:>14,.2f}".format(
                year, number, want, rest, now, fix, now + fix, now + fix - want))
    return "\n".join(lines)


def load_export(path):
    """The journal and the accounts of the export."""
    data = {}
    for name in ("journal", "accounts"):
        with open(os.path.join(path, name + ".json"), encoding="utf-8") as f:
            data[name] = json.load(f)
    return data


def main(argv):
    parser = argparse.ArgumentParser(description="Move the VAT of the imported history to bexio's transitory accounts.")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals")
    parser.add_argument("--write", metavar="FILE", default=None,
                        help="write the Journal Entries to a private file for the loader (bexio-drafts.sh FILE submit)")
    parser.add_argument("--token-file", default=im.TOKEN_FILE)
    args = parser.parse_args(argv)
    if args.dry_run == (args.write is not None):
        parser.error("exactly one of --dry-run and --write FILE")

    export_dir = args.export or im.newest_export()
    data = load_export(export_dir)
    try:
        lookups = Lookups.from_erp(im.Erp.from_file(args.token_file))
    except im.ErpError as err:
        print("aborted: {}".format(err), file=sys.stderr)
        return 2

    if SALES_ROUNDING_ACCOUNT not in lookups.accounts or ROUNDING_ACCOUNT not in lookups.accounts:
        print("aborted: the chart has no {} or {}".format(SALES_ROUNDING_ACCOUNT, ROUNDING_ACCOUNT), file=sys.stderr)
        return 2
    documents, problems, listing, details = plan(data, lookups)
    print("VAT corrections from {}".format(export_dir))
    print(summary(data["journal"], account_numbers(data["accounts"]), lookups, documents))
    kinds = collections.Counter(d["bexio_id"].split("-")[0] for d in documents)
    print("Journal Entries: {}".format(", ".join("{} {}".format(k, n) for k, n in sorted(kinds.items())) or "none"))
    print("payment VAT lines listed for erp-fd93: {}".format(len(listing)))
    print("not written: {} (by bexio id in {})".format(len(problems), os.path.join(im.PRIVATE, PROBLEMS_FILE)))
    ip.write_private(os.path.join(im.PRIVATE, PROBLEMS_FILE), [
        "{} {}: {}".format(kind, bexio_id, reason) for kind, bexio_id, reason in problems] or ["none"])
    ip.write_private(os.path.join(im.PRIVATE, VAT_IDS_FILE), listing or ["none"])
    ip.write_private(os.path.join(im.PRIVATE, DETAILS_FILE), details or ["none"])
    if args.write:
        ip.write_private(args.write, [json.dumps({"documents": documents, "exchange_rates": []}, indent=1)])
        print("{} Journal Entries handed to the loader in {}".format(len(documents), args.write))
    else:
        print("dry run: nothing was written to ERPNext")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
