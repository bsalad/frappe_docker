"""Offline tests for qrbill.py: payload field by field, and the QR code decodes back to it.

Invented data only (SIX sample IBANs, made-up names and addresses), no network.
The module imports frappe, so run it in the image, where pyqrcode, pypng and cv2
are installed (finance/scripts/build-image.sh builds it):

    docker run --rm -v "$PWD/finance/apps/bi_finance:/home/frappe/bi_finance_src:ro" \
        frappe-finance-custom:v16.50.0-swiss-bi3 \
        sh -c 'cd /home/frappe/bi_finance_src && ../frappe-bench/env/bin/python -m unittest -v bi_finance.test_qrbill'
"""

import unittest

from bi_finance import qrbill

IBAN = "CH21 0900 0000 2500 9779 8"  # SIX sample, plain IBAN (IID 09000)
QR_IBAN = "CH44 3199 9123 0008 8901 2"  # SIX sample, IID 31999: a QR-IBAN

CREDITOR = qrbill.address_lines(name="Beispiel AG", street="Musterweg", building="7", postal="8000", town="Zürich", country="ch")
DEBTOR = qrbill.address_lines(name="Muster GmbH", street="Teststrasse", building="15a", postal="3000", town="Bern")


class Payload(unittest.TestCase):
    def lines(self, **kw):
        args = dict(iban=IBAN, creditor=CREDITOR, amount=1234.5, currency="CHF", debtor=DEBTOR, message="Rechnung 1")
        args.update(kw)
        return qrbill.payload(**args).split("\r\n")

    def test_field_order_and_values(self):
        lines = self.lines()
        self.assertEqual(len(lines), 31)
        self.assertEqual(lines[0:4], ["SPC", "0200", "1", "CH2109000000250097798"])
        self.assertEqual(lines[4:11], ["S", "Beispiel AG", "Musterweg", "7", "8000", "Zürich", "CH"])
        self.assertEqual(lines[11:18], [""] * 7)  # ultimate creditor
        self.assertEqual(lines[18:20], ["1234.50", "CHF"])
        self.assertEqual(lines[20:27], ["S", "Muster GmbH", "Teststrasse", "15a", "3000", "Bern", "CH"])
        self.assertEqual(lines[27:30], ["NON", "", "Rechnung 1"])
        self.assertEqual(lines[30], "EPD")

    def test_no_debtor_leaves_seven_empty_lines(self):
        self.assertEqual(self.lines(debtor=None)[20:27], [""] * 7)

    def test_crlf_separates_elements(self):
        self.assertNotIn("\n", qrbill.payload(IBAN, CREDITOR, 1, "CHF").replace("\r\n", ""))

    def test_amount_format(self):
        self.assertEqual(qrbill.amount_text(0.1 + 0.2, "CHF"), "0.30")
        self.assertEqual(qrbill.amount_text(12.345, "CHF"), "12.35")  # half up
        self.assertEqual(qrbill.amount_text("999999999.99", "EUR"), "999999999.99")

    def test_amount_out_of_range_is_refused(self):
        for bad in (0, 0.001, 1000000000):
            with self.subTest(amount=bad), self.assertRaises(ValueError):
                qrbill.amount_text(bad, "CHF")

    def test_only_chf_and_eur(self):
        self.assertEqual(self.lines(currency="EUR")[18:20], ["1234.50", "EUR"])
        with self.assertRaises(ValueError):
            self.lines(currency="USD")

    def test_address_truncation(self):
        long = qrbill.address_lines(name="N" * 80, street="S" * 80, building="1" * 20, postal="P" * 20, town="T" * 40)
        self.assertEqual(long[1:], ["N" * 70, "S" * 70, "1" * 16, "P" * 16, "T" * 35, "CH"])

    def test_message_truncation_and_line_breaks(self):
        lines = self.lines(message="x" * 200)
        self.assertEqual(lines[29], "x" * 140)
        self.assertEqual(qrbill.address_lines(name="Muster\nAG")[1], "Muster AG")

    def test_qr_iban_is_refused(self):
        with self.assertRaises(ValueError):
            self.lines(iban=QR_IBAN)

    def test_split_street(self):
        self.assertEqual(qrbill.split_street("Musterstrasse 15a"), ("Musterstrasse", "15a"))
        self.assertEqual(qrbill.split_street("Bahnhofplatz"), ("Bahnhofplatz", ""))
        self.assertEqual(qrbill.split_street("Postfach 12 3"), ("Postfach 12", "3"))

    def test_iban_printed_in_groups_of_four(self):
        self.assertEqual(qrbill.iban_text("CH2109000000250097798"), "CH21 0900 0000 2500 9779 8")
        self.assertEqual(qrbill.iban_text(IBAN), "CH21 0900 0000 2500 9779 8")
        # the QR text keeps the IBAN unspaced
        self.assertEqual(self.lines()[3], "CH2109000000250097798")


class QrCode(unittest.TestCase):
    def test_matrix_decodes_to_payload(self):
        try:
            import cv2
            import numpy as np
            import png
        except ImportError as e:
            self.skipTest(str(e))
        import io

        text = qrbill.payload(IBAN, CREDITOR, 1234.5, "CHF", DEBTOR, "Rechnung 1")
        code = qrbill._matrix(text)
        scale, quiet = 8, 4
        size = (len(code) + 2 * quiet) * scale
        rows = []
        for py in range(size):
            my = py // scale - quiet
            row = []
            for px in range(size):
                mx = px // scale - quiet
                dark = 0 <= my < len(code) and 0 <= mx < len(code) and code[my][mx]
                row.append(0 if dark else 255)
            rows.append(bytes(row))
        buf = io.BytesIO()
        png.Writer(size, size, greyscale=True, bitdepth=8).write(buf, rows)
        img = cv2.imdecode(np.frombuffer(buf.getvalue(), np.uint8), cv2.IMREAD_GRAYSCALE)
        decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
        self.assertEqual(decoded, text)

    def test_svg_has_cross_and_code(self):
        out = qrbill.svg(qrbill.payload(IBAN, CREDITOR, 1, "CHF"))
        self.assertIn('viewBox="0 0 ', out)
        self.assertIn('width="46mm"', out)
        self.assertEqual(out.count("<path"), 1)  # the modules
        # white box, black square, two white bars: a white cross on black, as in the guidelines
        self.assertEqual(out.count("<rect"), 5)
        self.assertEqual(out.count('fill="#000"'), 2)  # modules and the cross square


class CompanyAccount(unittest.TestCase):
    # Invented names: two CHF accounts, one EUR account; the company's default is the first CHF one.
    ROWS = [
        {"name": "BA-1", "iban": IBAN, "account": "1020 - Bank A", "currency": "CHF"},
        {"name": "BA-2", "iban": QR_IBAN, "account": "1021 - Bank B", "currency": "CHF"},
        {"name": "BA-3", "iban": IBAN, "account": "1022 - Bank C", "currency": "EUR"},
    ]

    def pick(self, currency="CHF", default="1020 - Bank A"):
        from types import SimpleNamespace
        from unittest import mock

        rows = [SimpleNamespace(name=r["name"], iban=r["iban"], account=r["account"]) for r in self.ROWS]
        currencies = {r["account"]: r["currency"] for r in self.ROWS}

        def get_value(doctype, name, field):
            return currencies[name] if doctype == "Account" else default

        # the translation call needs a site; the message itself is not under test
        with mock.patch.object(qrbill, "frappe") as fake, mock.patch.object(qrbill, "_", new=lambda s: s):
            fake.get_all.return_value = rows
            fake.db.get_value.side_effect = get_value
            fake.throw.side_effect = ValueError
            return qrbill._company_account("Test Co", currency).name

    def test_company_default_wins_among_several(self):
        self.assertEqual(self.pick(), "BA-1")

    def test_currency_filters_accounts(self):
        self.assertEqual(self.pick(currency="EUR", default="1022 - Bank C"), "BA-3")

    def test_no_default_among_several_is_refused(self):
        with self.assertRaises(ValueError):
            self.pick(default="9999 - Other")


class DataUri(unittest.TestCase):
    def test_print_format_gets_the_svg_inline(self):
        import base64
        from unittest import mock

        text = qrbill.payload(IBAN, CREDITOR, 1, "CHF")
        with mock.patch.object(qrbill, "sales_invoice_payload", return_value=text):
            uri = qrbill.sales_invoice_qr_uri(object())
        prefix = "data:image/svg+xml;base64,"
        self.assertTrue(uri.startswith(prefix))
        self.assertEqual(base64.b64decode(uri[len(prefix):]).decode("utf-8"), qrbill.svg(text))


if __name__ == "__main__":
    unittest.main()
