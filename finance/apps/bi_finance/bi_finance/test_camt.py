"""Offline tests for camt.py: the four message versions, the transaction fields, the dedupe rules and the balance check.

Invented data only (an example IBAN, made-up names, references and round amounts), no network, no frappe:

    python3 -m unittest -v bi_finance.test_camt        (from finance/apps/bi_finance)
"""

import unittest
from decimal import Decimal

from bi_finance import camt

IBAN = "CH56 0483 5012 3456 7800 9"
NS = "urn:iso:std:iso:20022:tech:xsd:"


def status(version, code="BOOK"):
    return "<Sts><Cd>{}</Cd></Sts>".format(code) if version.endswith(".08") else "<Sts>{}</Sts>".format(code)


def entry(version, amount, direction, booked, value, refs="", details="", info="", code="BOOK"):
    return ('<Ntry><Amt Ccy="CHF">{}</Amt><CdtDbtInd>{}</CdtDbtInd>{}<BookgDt><Dt>{}</Dt></BookgDt>'
            '<ValDt><Dt>{}</Dt></ValDt>{}{}{}</Ntry>').format(amount, direction, status(version, code), booked, value, refs, details, info)


def detail(amount, direction, refs, party, name, party_iban, remittance=""):
    side = "Dbtr" if direction == "CRDT" else "Cdtr"
    return ('<NtryDtls><TxDtls><Amt Ccy="CHF">{}</Amt><CdtDbtInd>{}</CdtDbtInd><Refs>{}</Refs>'
            '<RltdPties><{s}><Nm>{}</Nm></{s}><{s}Acct><Id><IBAN>{}</IBAN></Id></{s}Acct></RltdPties>'
            '<RmtInf><Ustrd>{}</Ustrd></RmtInf></TxDtls></NtryDtls>').format(
        amount, direction, refs, name, party_iban, remittance, s=side)


def batch(version, amount="300.00"):
    """Three hundred francs paid to two creditors: the entry is the sum, its two details the parts."""
    details = (detail("100.00", "DBIT", "<TxId>TX-B1</TxId>", "Cdtr", "Lieferant Eins AG", "CH93 0076 2011 6238 5295 7", "Rechnung 1001")
               + detail("100.00", "DBIT", "<TxId>TX-B2</TxId>", "Cdtr", "Lieferant Zwei GmbH", "CH44 3199 9123 0008 8901 2", "Rechnung 1002"))
    return entry(version, amount, "DBIT", "2026-10-03", "2026-10-03", "<AcctSvcrRef>AS-B</AcctSvcrRef>", details)


def qr_payment(version):
    """Eighty francs from a debtor with a QRR reference; booked on the 5th, valued on the 6th."""
    remittance = "<Strd><CdtrRefInf><Tp><CdOrPrtry><Prtry>QRR</Prtry></CdOrPrtry></Tp><Ref>210000000003139471430009017</Ref></CdtrRefInf></Strd>"
    details = ('<NtryDtls><TxDtls><Amt Ccy="CHF">80.00</Amt><CdtDbtInd>CRDT</CdtDbtInd>'
               '<Refs><UETR>3f1c7e2a-1b2c-4d5e-8f90-123456789abc</UETR></Refs>'
               '<RltdPties><Dbtr><Nm>Muster GmbH</Nm></Dbtr><DbtrAcct><Id><IBAN>CH21 0900 0000 2500 9779 8</IBAN></Id></DbtrAcct></RltdPties>'
               '<RmtInf>{}<Ustrd>Rechnung 1003</Ustrd></RmtInf></TxDtls></NtryDtls>').format(remittance)
    return entry(version, "80.00", "CRDT", "2026-10-05", "2026-10-06", "", details)


def plain(version):
    return entry(version, "250.00", "CRDT", "2026-10-02", "2026-10-02", "<AcctSvcrRef>AS-1</AcctSvcrRef>", "", "<AddtlNtryInf>Rechnung 42</AddtlNtryInf>")


def pending(version):
    return entry(version, "5.00", "DBIT", "2026-10-04", "2026-10-04", "<AcctSvcrRef>AS-P</AcctSvcrRef>", code="PDNG")


def statement_xml(version, entries, opening="1000.00", closing="1030.00"):
    """A camt.053 with opening and closing balance, or a camt.054 notification (no balances in that message)."""
    if version.startswith("camt.053"):
        bal = ("<Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy=\"CHF\">{}</Amt><CdtDbtInd>CRDT</CdtDbtInd>"
               "<Dt><Dt>2026-10-01</Dt></Dt></Bal>"
               "<Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy=\"CHF\">{}</Amt><CdtDbtInd>CRDT</CdtDbtInd>"
               "<Dt><Dt>2026-10-06</Dt></Dt></Bal>").format(opening, closing)
        body = "BkToCstmrStmt"
        statement = "Stmt"
    else:
        bal = ""
        body = "BkToCstmrDbtCdtNtfctn"
        statement = "Ntfctn"
    account = "<Acct><Id><IBAN>{}</IBAN></Id><Ccy>CHF</Ccy></Acct>".format(IBAN)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<Document xmlns="{}{}"><{b}><GrpHdr><MsgId>MSG-1</MsgId></GrpHdr>'
            '<{s}><Id>STMT-1</Id>{a}{bal}{entries}</{s}></{b}></Document>').format(
        NS, version, b=body, s=statement, a=account, bal=bal, entries="".join(entries)).encode("utf-8")


def standard(version):
    """The file all four versions share: a plain entry, a pending one, a batch, a QR payment."""
    return statement_xml(version, [plain(version), pending(version), batch(version), qr_payment(version)])


VERSIONS = ("camt.053.001.04", "camt.053.001.08", "camt.054.001.04", "camt.054.001.08")


class Versions(unittest.TestCase):
    def test_each_version_is_read_and_reported(self):
        for version in VERSIONS:
            with self.subTest(version=version):
                st = camt.parse(standard(version))
                self.assertEqual(st["version"], version)
                self.assertEqual(st["kind"], version[:8])
                self.assertEqual(st["iban"], "CH5604835012345678009")
                self.assertEqual(st["currency"], "CHF")

    def test_balances_only_in_the_statement(self):
        self.assertEqual(sorted(camt.parse(standard("camt.053.001.08"))["balances"]), ["CLBD", "OPBD"])
        self.assertEqual(camt.parse(standard("camt.054.001.04"))["balances"], {})

    def test_balance_signs_and_dates(self):
        balances = camt.parse(standard("camt.053.001.04"))["balances"]
        self.assertEqual(balances["OPBD"], {"date": "2026-10-01", "amount": Decimal("1000.00"), "currency": "CHF"})
        self.assertEqual(balances["CLBD"]["date"], "2026-10-06")
        debit = statement_xml("camt.053.001.08", [], opening="10.00").replace(b"CRDT</CdtDbtInd><Dt><Dt>2026-10-01", b"DBIT</CdtDbtInd><Dt><Dt>2026-10-01")
        self.assertEqual(camt.parse(debit)["balances"]["OPBD"]["amount"], Decimal("-10.00"))

    def test_other_files_are_refused(self):
        for bad, why in (
            (statement_xml("camt.053.001.02", []), "version .02"),
            (statement_xml("camt.054.001.10", []), "version .10"),
            (b"<html></html>", "not camt"),
            (b"not xml at all", "not XML"),
        ):
            with self.subTest(why=why), self.assertRaises(camt.CamtError):
                camt.parse(bad)

    def test_two_statements_in_one_file_are_refused(self):
        xml = statement_xml("camt.053.001.08", [])
        twice = xml.replace(b"</Stmt>", b"</Stmt><Stmt><Id>X</Id><Acct><Id><IBAN>CH21 0900 0000 2500 9779 8</IBAN></Id></Acct></Stmt>")
        with self.assertRaises(camt.CamtError):
            camt.parse(twice)

    def test_iban_without_value_is_refused(self):
        with self.assertRaises(camt.CamtError):
            camt.parse(statement_xml("camt.053.001.08", []).replace(IBAN.encode(), b""))


class Transactions(unittest.TestCase):
    def test_plain_entry(self):
        tx = camt.parse(standard("camt.053.001.08"))["transactions"][0]
        self.assertEqual(tx["reference"], "AS-1")
        self.assertEqual((tx["booking_date"], tx["value_date"]), ("2026-10-02", "2026-10-02"))
        self.assertEqual((tx["deposit"], tx["withdrawal"]), (Decimal("250.00"), Decimal("0")))
        self.assertEqual(tx["description"], "Rechnung 42")
        self.assertEqual(tx["currency"], "CHF")

    def test_pending_entry_is_not_imported(self):
        st = camt.parse(standard("camt.053.001.04"))
        self.assertEqual(st["skipped"], {"not booked": 1, "covered": 0})
        self.assertNotIn("AS-P", [tx["reference"] for tx in st["transactions"]])

    def test_batch_gives_one_transaction_per_detail_and_not_the_sum(self):
        for version in VERSIONS:
            with self.subTest(version=version):
                txs = camt.parse(standard(version))["transactions"]
                parts = [tx for tx in txs if tx["reference"] in ("TX-B1", "TX-B2")]
                self.assertEqual([tx["withdrawal"] for tx in parts], [Decimal("100.00"), Decimal("100.00")])
                self.assertEqual([tx["bank_party_name"] for tx in parts], ["Lieferant Eins AG", "Lieferant Zwei GmbH"])
                self.assertEqual(parts[0]["bank_party_iban"], "CH9300762011623852957")
                self.assertEqual(parts[1]["description"], "Rechnung 1002")
                self.assertNotIn(Decimal("300.00"), [tx["withdrawal"] for tx in txs])
                self.assertEqual(len(txs), 4)

    def test_qr_payment_reference_value_date_and_party(self):
        txs = camt.parse(standard("camt.054.001.08"))["transactions"]
        tx = next(tx for tx in txs if tx["reference"] == "3f1c7e2a-1b2c-4d5e-8f90-123456789abc")
        self.assertEqual(tx["reference_number"], "210000000003139471430009017")
        self.assertEqual((tx["booking_date"], tx["value_date"]), ("2026-10-05", "2026-10-06"))
        self.assertEqual(tx["description"], "Valuta 2026-10-06 Rechnung 1003")
        self.assertEqual((tx["deposit"], tx["withdrawal"]), (Decimal("80.00"), Decimal("0")))
        self.assertEqual((tx["bank_party_name"], tx["bank_party_iban"]), ("Muster GmbH", "CH2109000000250097798"))

    def test_camt054_gives_the_same_transactions_as_camt053(self):
        for kind in ("camt.053.001.04", "camt.054.001.04", "camt.054.001.08"):
            with self.subTest(kind=kind):
                self.assertEqual(camt.parse(standard(kind))["transactions"], camt.parse(standard("camt.053.001.08"))["transactions"])

    def test_entry_covered_by_a_details_entry_is_skipped(self):
        xml = statement_xml("camt.053.001.08", [batch("camt.053.001.08"), plain_as("AS-B", "camt.053.001.08")])
        st = camt.parse(xml)
        self.assertEqual(st["skipped"]["covered"], 1)
        self.assertEqual(len(st["transactions"]), 2)

    def test_entry_without_reference_gets_a_stable_hash_and_a_count(self):
        twice = statement_xml("camt.053.001.08", [no_ref("2026-10-07"), no_ref("2026-10-07")])
        first = camt.parse(twice)["transactions"]
        self.assertEqual(len({tx["reference"] for tx in first}), 2)
        self.assertTrue(first[0]["reference"].startswith("camt-"))
        self.assertEqual([tx["reference"] for tx in first], [tx["reference"] for tx in camt.parse(twice)["transactions"]])

    def test_entry_number_alone_does_not_repeat_across_statements(self):
        def numbered(booked):
            return statement_xml("camt.053.001.08", [entry("camt.053.001.08", "9.00", "DBIT", booked, booked, "<NtryRef>1</NtryRef>")])
        first = camt.parse(numbered("2026-09-01"))["transactions"][0]["reference"]
        second = camt.parse(numbered("2026-10-01"))["transactions"][0]["reference"]
        self.assertNotEqual(first, second)
        self.assertEqual(first, camt.parse(numbered("2026-09-01"))["transactions"][0]["reference"])

    def test_description_is_cut(self):
        long = entry("camt.053.001.08", "1.00", "CRDT", "2026-10-02", "2026-10-02", "<AcctSvcrRef>AS-L</AcctSvcrRef>", "", "<AddtlNtryInf>{}</AddtlNtryInf>".format("x" * 300))
        tx = camt.parse(statement_xml("camt.053.001.08", [long]))["transactions"][0]
        self.assertEqual(len(tx["description"]), camt.DESCRIPTION_LENGTH)


def plain_as(reference, version):
    return entry(version, "300.00", "DBIT", "2026-10-03", "2026-10-03", "<AcctSvcrRef>{}</AcctSvcrRef>".format(reference), "", "<AddtlNtryInf>Sum</AddtlNtryInf>")


def no_ref(booked):
    return entry("camt.053.001.08", "7.00", "DBIT", booked, booked, "", "", "<AddtlNtryInf>Gebühr</AddtlNtryInf>")


def bexio(name, date, deposit="0", withdrawal="0"):
    return {"name": name, "date": date, "deposit": Decimal(deposit), "withdrawal": Decimal(withdrawal)}


class Dedupe(unittest.TestCase):
    def setUp(self):
        self.st = camt.parse(standard("camt.053.001.08"))

    def rules(self, decisions):
        return {tx["reference"]: (tx["rule"], tx["bexio"]) for tx in decisions}

    def test_new_file_is_all_new(self):
        decisions = camt.plan(self.st, [], [])
        self.assertEqual([tx["rule"] for tx in decisions], ["new"] * 4)
        self.assertEqual(camt.summary(decisions, self.st["skipped"])["new"], 4)

    def test_second_upload_of_the_same_file_adds_nothing(self):
        ids = [tx["reference"] for tx in self.st["transactions"]]
        decisions = camt.plan(self.st, ids, [])
        counts = camt.summary(decisions, self.st["skipped"])
        self.assertEqual((counts["new"], counts["id"], counts["bexio"]), (0, 4, 0))

    def test_camt054_of_the_same_bookings_after_camt053_adds_nothing(self):
        ids = [tx["reference"] for tx in camt.parse(standard("camt.053.001.04"))["transactions"]]
        st = camt.parse(standard("camt.054.001.08"))
        counts = camt.summary(camt.plan(st, ids, []), st["skipped"])
        self.assertEqual((counts["new"], counts["id"]), (0, 4))

    def test_id_rule_comes_before_bexio(self):
        lines = [bexio("BX-1", "2026-10-02", deposit="250")]
        decisions = camt.plan(self.st, ["AS-1"], lines)
        self.assertEqual(self.rules(decisions)["AS-1"], ("id", None))

    def test_bexio_line_matched_by_booking_or_value_date_and_amount(self):
        lines = [bexio("BX-1", "2026-10-02", deposit="250"), bexio("BX-2", "2026-10-06", deposit="80")]
        rules = self.rules(camt.plan(self.st, [], lines))
        self.assertEqual(rules["AS-1"], ("bexio", "BX-1"))
        self.assertEqual(rules["3f1c7e2a-1b2c-4d5e-8f90-123456789abc"], ("bexio", "BX-2"))

    def test_bexio_line_is_taken_once(self):
        lines = [bexio("BX-1", "2026-10-03", withdrawal="100")]
        rules = self.rules(camt.plan(self.st, [], lines))
        self.assertEqual(sorted(rule for rule, _ in rules.values()), ["bexio", "new", "new", "new"])
        self.assertEqual([name for rule, name in rules.values() if name], ["BX-1"])

    def test_two_bexio_lines_for_two_details(self):
        lines = [bexio("BX-2", "2026-10-03", withdrawal="100"), bexio("BX-1", "2026-10-03", withdrawal="100")]
        rules = self.rules(camt.plan(self.st, [], lines))
        self.assertEqual({rules["TX-B1"][1], rules["TX-B2"][1]}, {"BX-1", "BX-2"})

    def test_bexio_line_needs_same_direction_and_date(self):
        lines = [bexio("BX-1", "2026-10-02", withdrawal="250"), bexio("BX-2", "2026-10-09", deposit="80")]
        self.assertEqual([tx["rule"] for tx in camt.plan(self.st, [], lines) if tx["reference"] == "AS-1"], ["new"])
        self.assertEqual([tx["rule"] for tx in camt.plan(self.st, [], lines) if tx["reference"].startswith("3f1c")], ["new"])

    def test_summary_by_month_and_skipped(self):
        decisions = camt.plan(self.st, ["AS-1"], [bexio("BX-2", "2026-10-03", withdrawal="100")])
        counts = camt.summary(decisions, self.st["skipped"])
        self.assertEqual(counts["by_month"], {"2026-10": {"new": 2, "id": 1, "bexio": 1}})
        self.assertEqual(counts["skipped"], {"not booked": 1, "covered": 0})


class Balances(unittest.TestCase):
    def test_file_against_ledger_reported_not_refused(self):
        balances = camt.parse(standard("camt.053.001.08"))["balances"]
        checks = camt.check_balances(balances, {"OPBD": Decimal("1000.00"), "CLBD": Decimal("1027.50")})
        self.assertEqual(checks["OPBD"]["diff"], Decimal("0.00"))
        self.assertEqual(checks["CLBD"]["file"], Decimal("1030.00"))
        self.assertEqual(checks["CLBD"]["diff"], Decimal("2.50"))

    def test_no_balances_no_checks(self):
        self.assertEqual(camt.check_balances({}, {}), {})


if __name__ == "__main__":
    unittest.main()
