"""Swiss QR-bill for Sales Invoice, rendered on our own server.

The payment part of the BI Sales Invoice QR print format calls
sales_invoice_qr_uri(). Nothing leaves the server: the payload is built from
the invoice and the QR code is drawn with pyqrcode, which ships with Frappe.
erpnextswiss's QR format sends the same data to data.libracore.ch, which is
why it stays off (finance/docs/swiss.md, Risks 1).

Payload per SIX Implementation Guidelines QR-bill v2.3: one data element per
line, structured addresses (type S), no QR reference (a QR-IBAN is refused
until a QRR path with bank reconciliation exists).
"""

import base64
import re
from decimal import ROUND_HALF_UP, Decimal

import frappe
from frappe import _

# Field limits from the guidelines: name and street 70, building number and
# postal code 16, town 35, message 140.
NAME_MAX, STREET_MAX, BUILDING_MAX, POSTAL_MAX, TOWN_MAX, MESSAGE_MAX = 70, 70, 16, 16, 35, 140
CURRENCIES = ("CHF", "EUR")
EOL = "\r\n"
EMPTY_ADDRESS = [""] * 7

# The cross is 7 mm on a 46 mm code, on a white square, with error level M.
QR_MM, CROSS_MM, QUIET_MODULES = 46, 7, 4

_STREET = re.compile(r"^(?P<street>.+?)\s+(?P<building>\d+[\w\-/]{0,9})$")


def _clip(value, limit):
    # Line breaks would split a data element, so they become spaces.
    return " ".join((value or "").split())[:limit]


def split_street(line):
    """Split 'Musterstrasse 15a' into street and building number; no number means street only."""
    m = _STREET.match((line or "").strip())
    if not m:
        return (line or "").strip(), ""
    return m.group("street"), m.group("building")


def iban_text(iban):
    """The IBAN in groups of four, as the slip prints it (SIX); the QR text keeps it unspaced."""
    compact = iban.replace(" ", "").upper()
    return " ".join(compact[i:i + 4] for i in range(0, len(compact), 4))


def is_qr_iban(iban):
    # IID 30000 to 31999 at positions 5 to 9 marks a QR-IBAN.
    return 30000 <= int(iban[4:9]) <= 31999


def amount_text(amount, currency):
    if currency not in CURRENCIES:
        raise ValueError(f"currency {currency} is not allowed on a QR-bill; use CHF or EUR")
    value = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if not Decimal("0.01") <= value <= Decimal("999999999.99"):
        raise ValueError(f"amount {value} is outside the QR-bill range")
    return f"{value:.2f}"


def address_lines(name="", street="", building="", postal="", town="", country="CH"):
    return [
        "S",
        _clip(name, NAME_MAX),
        _clip(street, STREET_MAX),
        _clip(building, BUILDING_MAX),
        _clip(postal, POSTAL_MAX),
        _clip(town, TOWN_MAX),
        _clip(country, 2).upper(),
    ]


def payload(iban, creditor, amount, currency, debtor=None, message=""):
    """The 31 data elements of the QR text, joined with CRLF.

    creditor and debtor are address_lines() results; debtor may be None.
    """
    iban = iban.replace(" ", "").upper()
    if is_qr_iban(iban):
        raise ValueError("QR-IBAN needs a QR reference, which is not supported yet")
    lines = [
        "SPC", "0200", "1", iban,
        *creditor,
        *EMPTY_ADDRESS,  # ultimate creditor, not used
        amount_text(amount, currency), currency,
        *(debtor or EMPTY_ADDRESS),
        "NON", "",  # no reference
        _clip(message, MESSAGE_MAX),
        "EPD",
    ]
    return EOL.join(lines)


def _matrix(text):
    """Module matrix of the QR code (1 is dark), without quiet zone, with the cross area cleared."""
    import pyqrcode

    code = pyqrcode.create(text, error="M", encoding="utf-8").code
    n = len(code)
    # The cross is centred; clear the modules under its white square.
    side = round(CROSS_MM / QR_MM * n)
    lo = (n - side) // 2
    for y in range(lo, lo + side):
        for x in range(lo, lo + side):
            code[y][x] = 0
    return code


def svg(text):
    """Inline SVG of the payload with the Swiss cross; sized to 46 mm by the print format."""
    code = _matrix(text)
    n = len(code)
    size = n + 2 * QUIET_MODULES
    dark = "".join(
        f"M{x + QUIET_MODULES},{y + QUIET_MODULES}h1v1h-1z"
        for y, row in enumerate(code) for x, v in enumerate(row) if v
    )
    # The Swiss cross of the guidelines: a 7 mm box with a white border, a black square
    # in it and a white cross on the square. Measured on a 19.8 unit box: square 0.7 to
    # 19.2, arms 3.3 wide and 11 long.
    side = CROSS_MM / QR_MM * n
    u = side / 19.8
    x0 = size / 2 - side / 2

    def rect(x, y, w, h, fill):
        return f'<rect x="{x0 + x * u:.3f}" y="{x0 + y * u:.3f}" width="{w * u:.3f}" height="{h * u:.3f}" fill="{fill}"/>'

    cross = (
        rect(0, 0, 19.8, 19.8, "#fff")
        + rect(0.7, 0.7, 18.5, 18.5, "#000")
        + rect(8.25, 4.4, 3.3, 11, "#fff")
        + rect(4.4, 8.25, 11, 3.3, "#fff")
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
        f'width="{QR_MM}mm" height="{QR_MM}mm" shape-rendering="crispEdges">'
        f'<rect width="{size}" height="{size}" fill="#fff"/>'
        f'<path fill="#000" d="{dark}"/>{cross}</svg>'
    )


def _country_code(country):
    code = frappe.db.get_value("Country", country, "code") if country else None
    return (code or "CH").upper()


def _address(name_address):
    """address_lines() of an Address document, or None when the document is missing."""
    if not name_address:
        return None
    addr = frappe.get_doc("Address", name_address)
    street, building = split_street(addr.address_line1)
    return address_lines(
        name=addr.address_title or "",
        street=street,
        building=building,
        postal=addr.pincode,
        town=addr.city,
        country=_country_code(addr.country),
    )


def _company_account(company, currency):
    """The company bank account with an IBAN in the invoice's currency.

    When several qualify, the company's default bank account is the one; otherwise
    the choice is ambiguous and the print fails.
    """
    rows = frappe.get_all(
        "Bank Account",
        filters={"company": company, "is_company_account": 1, "disabled": 0, "iban": ["is", "set"]},
        fields=["name", "iban", "account"],
    )
    rows = [r for r in rows if frappe.db.get_value("Account", r.account, "account_currency") == currency]
    if len(rows) > 1:
        default = frappe.db.get_value("Company", company, "default_bank_account")
        rows = [r for r in rows if r.account == default] or rows
    if len(rows) != 1:
        frappe.throw(_("Expected one company bank account with an IBAN in {0}, found {1}").format(currency, len(rows)))
    return rows[0]


def sales_invoice_iban(inv):
    """The IBAN the QR-bill pays to, grouped for the print; the same account as the QR code."""
    return iban_text(_company_account(inv.company, inv.currency).iban)


def sales_invoice_payload(inv):
    """QR text of a Sales Invoice: the bank account, the company as creditor, the customer as debtor."""
    account = _company_account(inv.company, inv.currency)
    creditor = _address(inv.company_address)
    if not creditor or not (creditor[4] and creditor[5]):
        frappe.throw(_("The company address needs a postal code and a town for the QR-bill"))
    creditor[1] = _clip(frappe.db.get_value("Company", inv.company, "company_name") or inv.company, NAME_MAX)
    debtor = _address(inv.customer_address)
    if debtor:
        debtor[1] = _clip(inv.customer_name or debtor[1], NAME_MAX)
    return payload(account.iban, creditor, inv.rounded_total or inv.grand_total, inv.currency, debtor, f"Rechnung {inv.name}")


def sales_invoice_qr_uri(inv):
    """Swiss cross QR code of a Sales Invoice as a data URI, for the print format's <img>.

    A jinja method (hooks.py), so the image is embedded in the page: wkhtmltopdf runs
    in the backend container and cannot fetch the site's own /api URLs.
    """
    try:
        text = sales_invoice_payload(inv)
    except ValueError as e:
        frappe.throw(str(e))
    return "data:image/svg+xml;base64," + base64.b64encode(svg(text).encode("utf-8")).decode("ascii")
