"""Reads a UBS camt.053 (account statement) or camt.054 (booking notification) file, and plans its import.

Pure Python, no frappe: camt_import.py does the ERPNext side (the Bank Account, the
Bank Transactions, the ledger balances). Versions .04 and .08 of both messages are read;
the namespace of the file says which one it is. Elements are found by their local name,
so the same version under another namespace prefix reads the same.

One Bank Transaction per entry. An entry with transaction details (a batch booking, or a
camt.054 notification) gives one per detail instead, and the entry itself is not counted:
the entry carries the sum, the details the parts. The entry's reference is then covered,
and a plain entry with that reference in the same file is skipped.
"""

import hashlib
import re
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

ISO = "urn:iso:std:iso:20022:tech:xsd:"
VERSIONS = ("camt.053.001.04", "camt.053.001.08", "camt.054.001.04", "camt.054.001.08")
BODY = {"camt.053": "BkToCstmrStmt", "camt.054": "BkToCstmrDbtCdtNtfctn"}
STATEMENT = {"camt.053": "Stmt", "camt.054": "Ntfctn"}
DESCRIPTION_LENGTH = 140


class CamtError(ValueError):
    """The file is not a camt.053 or camt.054 this importer reads, or does not hold one account."""


def parse(content):
    """The statement of a camt file: its account, balances and one dict per Bank Transaction."""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as err:
        raise CamtError("not XML: {}".format(err))
    match = re.fullmatch(r"\{" + re.escape(ISO) + r"(camt\.05[34]\.001\.\d\d)\}Document", root.tag)
    if not match or match.group(1) not in VERSIONS:
        raise CamtError("not a camt.053 or camt.054 file of version .04 or .08 (root {})".format(root.tag))
    version = match.group(1)
    kind = version[:8]

    bodies = _kids(root, BODY[kind])
    if len(bodies) != 1:
        raise CamtError("{} is not in the file".format(BODY[kind]))
    statements = _kids(bodies[0], STATEMENT[kind])
    if len(statements) != 1:
        raise CamtError("{} statements in the file; one account per file".format(len(statements)))
    statement = statements[0]

    iban = _iban(_text(statement, "Acct", "Id", "IBAN"))
    if not iban:
        raise CamtError("no account IBAN in the file")
    balances = {}
    for bal in _kids(statement, "Bal"):
        code = _text(bal, "Tp", "CdOrPrtry", "Cd")
        if code in ("OPBD", "CLBD"):
            amount, currency = _money(bal)
            balances[code] = {"date": _date(_first(bal, "Dt")), "amount": _signed(amount, _text(bal, "CdtDbtInd")), "currency": currency}

    skipped = {"not booked": 0, "covered": 0}
    booked = []
    for entry in _kids(statement, "Ntry"):
        status = _text(entry, "Sts") or _text(entry, "Sts", "Cd")
        if status not in (None, "BOOK"):
            skipped["not booked"] += 1
        else:
            booked.append(entry)
    # Found before any entry is read: a plain entry with a covered reference is skipped, wherever it comes in the file.
    covered = {_entry_reference(entry) for entry in booked if _txdtls(entry)} - {None}

    transactions = []
    for entry in booked:
        txds = _txdtls(entry)
        if not txds and _entry_reference(entry) in covered:
            skipped["covered"] += 1
            continue
        if txds:
            transactions.extend(_transaction(entry, txd, len(txds) == 1) for txd in txds)
        else:
            transactions.append(_transaction(entry, None, True))
    _fill_references(transactions)

    currency = _text(statement, "Acct", "Ccy") or (transactions[0]["currency"] if transactions else None)
    return {
        "version": version, "kind": kind, "iban": iban, "currency": currency,
        "balances": balances, "transactions": transactions, "skipped": skipped,
    }


def plan(statement, existing_ids, bexio_lines):
    """Each transaction of the statement with the rule that decides it, in this order:

    "id": its transaction_id is already on the account, or came earlier in this file.
    "bexio": a bexio line of the account (no transaction_id yet) has its date and amount;
        the line is taken once, and its name is given so the import stamps the transaction_id on it.
    "new": neither; it is imported.

    Nothing is written. existing_ids: the transaction_ids on the account. bexio_lines: dicts with
    name, date, deposit and withdrawal, the lines of the account without a transaction_id.
    """
    seen = set(existing_ids)
    lines = sorted(bexio_lines, key=lambda line: line["name"])
    decisions = []
    for tx in statement["transactions"]:
        line = None
        if tx["reference"] in seen:
            rule = "id"
        else:
            line = next((c for c in lines if _same_line(c, tx)), None)
            if line is not None:
                lines.remove(line)
                rule = "bexio"
            else:
                rule = "new"
            seen.add(tx["reference"])
        decisions.append(dict(tx, rule=rule, bexio=line["name"] if line else None))
    return decisions


def summary(decisions, skipped):
    """Counts per rule, and per booking month. The skipped entries are counted apart."""
    months = {}
    totals = {"new": 0, "id": 0, "bexio": 0}
    for tx in decisions:
        totals[tx["rule"]] += 1
        month = months.setdefault(tx["booking_date"][:7], {"new": 0, "id": 0, "bexio": 0})
        month[tx["rule"]] += 1
    return dict(totals, skipped=dict(skipped), by_month=dict(sorted(months.items())))


def check_balances(balances, ledger):
    """The file's OPBD and CLBD against the ledger's balance of the account: ledger maps each code to a Decimal.

    Reported only: the ledger may lag the bank, so a difference never stops an import.
    """
    checks = {}
    for code in ("OPBD", "CLBD"):
        if code in balances:
            filed = balances[code]["amount"]
            checks[code] = {"date": balances[code]["date"], "file": filed, "ledger": ledger[code], "diff": filed - ledger[code]}
    return checks


def _same_line(line, tx):
    if line["deposit"] != tx["deposit"] or line["withdrawal"] != tx["withdrawal"]:
        return False
    return bool(line["date"]) and line["date"] in (tx["value_date"], tx["booking_date"])


def _transaction(entry, txd, single):
    """One Bank Transaction's fields, from an entry, or from one of its transaction details (txd).

    The detail's own amount, direction, parties and remittance win; without a detail, those of the entry are used.
    Its reference: the detail's UETR, AcctSvcrRef, TxId, PmtInfId; an entry with one detail or none also takes the
    entry's AcctSvcrRef, then NtryRef. Empty leaves it to _fill_references.
    """
    amount, currency = _money(txd if _first(txd, "Amt") is not None else entry)
    direction = _text(txd, "CdtDbtInd") or _text(entry, "CdtDbtInd")
    if direction not in ("CRDT", "DBIT"):
        raise CamtError("entry without CRDT or DBIT")
    booking = _date(_first(entry, "BookgDt"))
    if not booking:
        raise CamtError("entry without a booking date")
    value = _date(_first(entry, "ValDt"))

    refs = _first(txd, "Refs")
    reference = next((ref for ref in (_text(refs, name) for name in ("UETR", "AcctSvcrRef", "TxId", "PmtInfId")) if ref), None)
    if reference is None and single:
        reference = _entry_reference(entry)

    remittance = _first(txd, "RmtInf")
    text = " ".join(_texts(remittance, "Ustrd")) or " ".join(_texts(entry, "AddtlNtryInf"))
    parties = _first(txd, "RltdPties")
    side = "Dbtr" if direction == "CRDT" else "Cdtr"
    if value and value != booking:
        text = " ".join(part for part in ("Valuta {}".format(value), text) if part)
    deposit, withdrawal = (amount, Decimal("0")) if direction == "CRDT" else (Decimal("0"), amount)
    return {
        "reference": reference, "booking_date": booking, "value_date": value, "currency": currency,
        "deposit": deposit, "withdrawal": withdrawal,
        "bank_party_name": _text(parties, side, "Nm"), "bank_party_iban": _iban(_text(parties, side + "Acct", "Id", "IBAN")),
        "reference_number": _text(remittance, "Strd", "CdtrRefInf", "Ref"),
        "description": text[:DESCRIPTION_LENGTH] or None,
    }


def _fill_references(transactions):
    """A transaction without a unique reference gets a hash of its content; the same content twice gets a count.

    The count is by order in the file, so the same file gives the same references on every upload.
    """
    counts = {}
    for tx in transactions:
        if tx["reference"] is not None:
            continue
        key = "|".join(str(tx[field]) for field in (
            "booking_date", "value_date", "deposit", "withdrawal", "currency",
            "bank_party_name", "bank_party_iban", "reference_number", "description"))
        digest = "camt-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        counts[digest] = counts.get(digest, 0) + 1
        tx["reference"] = digest if counts[digest] == 1 else "{}-{}".format(digest, counts[digest])


def _entry_reference(entry):
    return _text(entry, "AcctSvcrRef") or _text(entry, "NtryRef")


def _txdtls(entry):
    return [txd for details in _kids(entry, "NtryDtls") for txd in _kids(details, "TxDtls")]


def _money(el):
    amt = _first(el, "Amt")
    if amt is None or not amt.text or not amt.text.strip():
        raise CamtError("amount without a value")
    try:
        return Decimal(amt.text.strip()), amt.get("Ccy")
    except InvalidOperation:
        raise CamtError("amount {!r} is not a number".format(amt.text))


def _signed(amount, direction):
    return amount if direction == "CRDT" else -amount


def _iban(value):
    return re.sub(r"\s", "", value).upper() if value else None


def _date(el):
    """The date of a Dt or DtTm element (in its own child: Bal/Dt/Dt, BookgDt/DtTm)."""
    if el is None:
        return None
    return _text(el, "Dt") or ((_text(el, "DtTm") or "")[:10] or None)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _kids(el, name):
    return [child for child in el if _local(child.tag) == name] if el is not None else []


def _first(el, *path):
    for name in path:
        el = next(iter(_kids(el, name)), None)
    return el


def _text(el, *path):
    found = _first(el, *path) if path else el
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _texts(el, name):
    return [child.text.strip() for child in _kids(el, name) if child.text and child.text.strip()]
