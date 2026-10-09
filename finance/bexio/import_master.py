"""Import the exported bexio master data into ERPNext, repeatable.

Step two of the master-data pipeline; export.py wrote the files. Every record
is keyed by the bexio id in the custom field `bexio_id`: it is looked up
there, updated when a field differs, created when it is missing, and never
matched by name. A second run on the same export therefore changes nothing.
One exception needs a first-run bridge: the accounts of the KMU chart carry
no bexio_id yet and are adopted by account number.

Runs as api-agent@finance.local through the REST API; the key and secret are
read from the token file and never printed. Run it as:

    python3 finance/bexio/import_master.py [--export DIR] [--dry-run]

--export defaults to the newest directory under <private>/bexio-export/.
--dry-run reads ERPNext and writes nothing; it prints the totals a real run
would produce. The output is totals per doctype only, no company data.

Left out because the API user may not write them: Bank (made by
`swiss-setup.sh banks`, which takes the names from --print-banks) and Designation (the role text of a contact relation
stays in the export). Order: groups, accounts, items, parties (Customer, Supplier), contacts,
addresses, bank accounts. Currencies are set by `swiss-setup.sh currencies`
(the API user may not write Currency); this script only checks them.
Standard library only.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PRIVATE = "/Users/bsaladin/ws_yardr_finance/private"
TOKEN_FILE = "/Users/bsaladin/ws_yardr_finance/.erpnext-api"
COMPANY = "BI Concepts"
ABBR = "bic"

# bexio account_type -> ERPNext root type (5 = closing accounts, kept in Equity)
ROOT_TYPES = {1: "Income", 2: "Expense", 3: "Asset", 4: "Liability", 5: "Equity"}
# Roots of ERPNext's KMU chart that hold a group of each root type. A bexio
# account whose root type differs from its nearest group's goes under one of
# these (ERPNext keeps the root type of a tree uniform; the KMU chart types
# class 8 as Income although it holds expenses too).
FALLBACK_GROUPS = {
    "Income": ("7E", "Übriger Ertrag (bexio)", "7"),
    "Expense": ("6E", "Übriger Aufwand (bexio)", "6"),
}
# KMU accounts whose currency follows bexio: 1020 is the CHF account of the bank
CURRENCY_OF_ACCOUNT = {"1020": "CHF"}
BANK_GL_ACCOUNTS = {"1020", "1021"}

# bexio unit name -> ERPNext UOM
UOMS = {"Stk": "Nos", "h": "Hour", "Tag": "Day", "d": "Day"}
SALUTATIONS = {"Herr": "Mr", "Frau": "Ms"}

# bexio VAT code id -> (rate, kind) of the Item Tax Template made by swiss-setup.py
VAT_OF_TAX_ID = {
    16: (7.7, "Normal"), 28: (8.1, "Normal"), 17: (2.5, "Reduziert"), 29: (2.6, "Reduziert"),
    18: (3.7, "Beherbergung"), 30: (3.8, "Beherbergung"),
    22: (7.7, "Normal"), 35: (8.1, "Normal"), 8: (2.5, "Reduziert"), 34: (2.6, "Reduziert"),
    21: (3.7, "Beherbergung"), 36: (3.8, "Beherbergung"),
    24: (7.7, "Normal"), 38: (8.1, "Normal"), 12: (2.5, "Reduziert"), 37: (2.6, "Reduziert"),
    23: (3.7, "Beherbergung"), 39: (3.8, "Beherbergung"),
}

DOCTYPES = [
    "Currency", "Customer Group", "Supplier Group", "Account", "Item", "Item Price",
    "Customer", "Supplier", "Contact", "Address", "Bank Account", "Purchase Invoice",
]


class ErpError(Exception):
    def __init__(self, status, what):
        super().__init__("ERPNext {} failed with HTTP {}".format(what, status))
        self.status = status


class Erp:
    """The few REST calls the import needs. The secret never leaves this object."""

    def __init__(self, url, key, secret):
        self._url = url.rstrip("/")
        self._auth = "token {}:{}".format(key, secret)

    @classmethod
    def from_file(cls, path=TOKEN_FILE):
        values = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    values[k] = v
        return cls(values["url"], values["api_key"], values["api_secret"])

    def _request(self, method, path, body=None):
        req = urllib.request.Request(
            self._url + path, method=method,
            data=None if body is None else json.dumps(body).encode("utf-8"),
        )
        req.add_header("Authorization", self._auth)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raise ErpError(err.code, "{} {}".format(method, path.split("?")[0]) + _reason(err)) from None
        except urllib.error.URLError:
            raise ErpError("network", "{} {}".format(method, path.split("?")[0])) from None

    def list(self, doctype, filters=None, fields=("name",)):
        query = urllib.parse.urlencode({
            "filters": json.dumps(filters or []),
            "fields": json.dumps(list(fields)),
            "limit_page_length": 100000,
        })
        return self._request("GET", "/resource/{}?{}".format(urllib.parse.quote(doctype), query))["data"]

    def get(self, doctype, name):
        return self._request("GET", "/resource/{}/{}".format(urllib.parse.quote(doctype), urllib.parse.quote(name, safe="")))["data"]

    def insert(self, doctype, doc):
        return self._request("POST", "/resource/" + urllib.parse.quote(doctype), dict(doc, doctype=doctype))["data"]

    def update(self, doctype, name, values):
        return self._request("PUT", "/resource/{}/{}".format(urllib.parse.quote(doctype), urllib.parse.quote(name, safe="")), values)["data"]

    def call(self, method, **args):
        return self._request("POST", "/method/" + method, args)["message"]


def _reason(err):
    """The first line of ERPNext's message for an error, for the operator's eyes."""
    try:
        body = json.loads(err.read().decode("utf-8"))
        text = body.get("exception") or body.get("exc_type") or ""
    except (ValueError, OSError):
        return ""
    return ": " + text.split("\n")[0][:160] if text else ""


def newest_export(root=os.path.join(PRIVATE, "bexio-export")):
    dirs = sorted(d for d in os.listdir(root) if os.path.isfile(os.path.join(root, d, "manifest.json")))
    if not dirs:
        raise SystemExit("no export under {}; run export.py first".format(root))
    return os.path.join(root, dirs[-1])


def load_export(path):
    data = {}
    for name in ("contacts", "contact_groups", "contact_relations", "articles", "accounts", "currencies",
                 "bank_accounts", "invoices", "bills", "countries", "units", "salutations"):
        with open(os.path.join(path, name + ".json"), encoding="utf-8") as f:
            data[name] = json.load(f)
    return data


def norm(value):
    """Compare ERPNext's answer with what we send: None, '' and 0 of a number are not told apart."""
    if value is None or value == "":
        return ""
    if isinstance(value, (bool, int, float)):
        return repr(float(value))
    # ERPNext stores line breaks of text fields as \n
    return str(value).replace("\r\n", "\n").strip()


def differs(have, want):
    """True when a field in `want` is not as in `have`; child tables compare row by row on the keys sent."""
    for key, wanted in want.items():
        current = have.get(key)
        if isinstance(wanted, list):
            current = current or []
            if len(current) != len(wanted):
                return True
            for row_have, row_want in zip(current, wanted):
                if any(norm(row_have.get(k)) != norm(v) for k, v in row_want.items()):
                    return True
        elif norm(current) != norm(wanted):
            return True
    return False


class Stats:
    def __init__(self):
        self.rows = {d: {"created": 0, "updated": 0, "unchanged": 0, "failed": 0, "skipped": 0} for d in DOCTYPES}
        self.problems = []

    def count(self, doctype, what):
        self.rows[doctype][what] += 1

    def problem(self, doctype, bexio_id, err):
        self.count(doctype, "failed")
        self.problems.append("{} {}: {}".format(doctype, bexio_id, str(err)[:200]))

    def changed(self):
        return sum(r["created"] + r["updated"] for r in self.rows.values())

    def failed(self):
        return sum(r["failed"] for r in self.rows.values())

    def report(self, dry_run):
        lines = ["{:<16}{:>9}{:>9}{:>10}{:>8}{:>9}".format("doctype", "created", "updated", "unchanged", "failed", "skipped")]
        for d, r in self.rows.items():
            if any(r.values()):
                lines.append("{:<16}{:>9}{:>9}{:>10}{:>8}{:>9}".format(d, r["created"], r["updated"], r["unchanged"], r["failed"], r["skipped"]))
        lines.append("dry run: nothing was written" if dry_run else "written to ERPNext")
        return "\n".join(lines)


class Importer:
    def __init__(self, erp, data, dry_run=False):
        self.erp = erp
        self.data = data
        self.dry_run = dry_run
        self.stats = Stats()
        self._index = {}      # doctype -> {bexio_id: name}
        self._taken = {}      # doctype -> {name: bexio_id}, for party names
        self.names = {}       # (doctype, bexio_id) -> name of the document, planned in a dry run
        self.country_name = {}

    # ---- the upsert ----

    def index(self, doctype):
        if doctype not in self._index:
            rows = self.erp.list(doctype, [["bexio_id", "is", "set"]], ["name", "bexio_id"])
            self._index[doctype] = {r["bexio_id"]: r["name"] for r in rows}
        return self._index[doctype]

    def upsert(self, doctype, bexio_id, want, plan_name):
        """Create or update the document keyed by bexio_id; returns its name.

        `plan_name` is the name a new document is expected to get, used when
        nothing is written (dry run) so that links to it can be formed.
        """
        bexio_id = str(bexio_id)
        want = dict(want, bexio_id=bexio_id)
        known = self.index(doctype)
        try:
            if bexio_id in known:
                name = known[bexio_id]
                if differs(self.erp.get(doctype, name), want):
                    if not self.dry_run:
                        self.erp.update(doctype, name, want)
                    self.stats.count(doctype, "updated")
                else:
                    self.stats.count(doctype, "unchanged")
            else:
                name = plan_name
                if not self.dry_run:
                    name = self.erp.insert(doctype, want)["name"]
                known[bexio_id] = name
                self.stats.count(doctype, "created")
        except ErpError as err:
            self.stats.problem(doctype, bexio_id, err)
            return None
        self.names[(doctype, bexio_id)] = name
        return name

    # ---- currencies ----

    def check_currencies(self):
        enabled = {r["name"] for r in self.erp.list("Currency", [["enabled", "=", 1]])}
        missing = [c["name"] for c in self.data["currencies"] if c["name"] not in enabled]
        for currency in self.data["currencies"]:
            self.stats.count("Currency", "failed" if currency["name"] in missing else "unchanged")
        if missing:
            self.stats.problems.append("Currency not enabled: {}; run finance/scripts/swiss-setup.sh currencies".format(", ".join(missing)))

    # ---- groups ----

    def import_groups(self):
        for group in self.data["contact_groups"]:
            for doctype, parent_field, name_field, parent in (
                ("Customer Group", "parent_customer_group", "customer_group_name", "All Customer Groups"),
                ("Supplier Group", "parent_supplier_group", "supplier_group_name", "All Supplier Groups"),
            ):
                self.upsert(doctype, group["id"], {name_field: group["name"], parent_field: parent, "is_group": 0}, group["name"])

    # ---- accounts ----

    def import_accounts(self):
        rows = self.erp.list("Account", [["company", "=", COMPANY]],
                             ["name", "account_number", "account_name", "parent_account", "root_type", "is_group",
                              "report_type", "disabled", "account_currency", "bexio_id"])
        by_number = {r["account_number"]: r for r in rows if r["account_number"]}
        by_bexio = {r["bexio_id"]: r for r in rows if r["bexio_id"]}
        groups = {n: r for n, r in by_number.items() if r["is_group"]}
        self._index["Account"] = {r["bexio_id"]: r["name"] for r in rows if r["bexio_id"]}

        for acc in sorted(self.data["accounts"], key=lambda a: a["account_no"]):
            number, bexio_id = acc["account_no"], str(acc["id"])
            root = ROOT_TYPES.get(acc["account_type"])
            try:
                if root is None:
                    raise ErpError("-", "account {} has unknown type {}".format(number, acc["account_type"]))
                have = by_bexio.get(bexio_id) or by_number.get(number)
                if have:
                    self._update_account(have, acc, root, groups, by_number)
                else:
                    self._create_account(acc, root, groups, by_number)
            except ErpError as err:
                self.stats.problem("Account", bexio_id, err)

    def _fallback_group(self, root, groups, by_number):
        number, title, parent = FALLBACK_GROUPS[root]
        if number in groups:
            return groups[number]["name"]
        if parent not in groups:
            raise ErpError("-", "root group {} of the KMU chart is missing".format(parent))
        doc = {"company": COMPANY, "account_name": title, "account_number": number,
               "parent_account": groups[parent]["name"], "is_group": 1, "root_type": root,
               "report_type": "Profit and Loss"}
        name = "{} - {} - {}".format(number, title, ABBR)
        if not self.dry_run:
            name = self.erp.insert("Account", doc)["name"]
        self.stats.count("Account", "created")
        groups[number] = by_number[number] = dict(doc, name=name)
        return name

    def _parent_for(self, number, root, groups, by_number):
        """The nearest KMU group by number prefix; a group of another root type sends the account to the fallback group."""
        for size in range(len(number) - 1, 0, -1):
            group = groups.get(number[:size])
            if group:
                if group["root_type"] == root:
                    return group["name"]
                break
        return self._fallback_group(root, groups, by_number)

    def _create_account(self, acc, root, groups, by_number):
        number = acc["account_no"]
        doc = {
            "company": COMPANY, "account_name": acc["name"], "account_number": number,
            "parent_account": self._parent_for(number, root, groups, by_number),
            "is_group": 0, "root_type": root,
            "report_type": "Balance Sheet" if root in ("Asset", "Liability", "Equity") else "Profit and Loss",
            "disabled": 0 if acc["is_active"] else 1, "bexio_id": str(acc["id"]),
        }
        if number in CURRENCY_OF_ACCOUNT:
            doc["account_currency"] = CURRENCY_OF_ACCOUNT[number]
        if number in BANK_GL_ACCOUNTS:
            doc["account_type"] = "Bank"
        name = "{} - {} - {}".format(number, acc["name"], ABBR)
        if not self.dry_run:
            name = self.erp.insert("Account", doc)["name"]
        self.stats.count("Account", "created")
        self.names[("Account", str(acc["id"]))] = name
        self._index["Account"][str(acc["id"])] = name
        by_number[number] = dict(doc, name=name)

    def _update_account(self, have, acc, root, groups, by_number):
        number, name = acc["account_no"], have["name"]
        want = {"bexio_id": str(acc["id"]), "disabled": 0 if acc["is_active"] else 1}
        if have["root_type"] != root:
            want["parent_account"] = self._fallback_group(root, groups, by_number)
            want["root_type"] = root
            want["report_type"] = "Balance Sheet" if root in ("Asset", "Liability", "Equity") else "Profit and Loss"
        if number in CURRENCY_OF_ACCOUNT:
            want["account_currency"] = CURRENCY_OF_ACCOUNT[number]
        renamed = have["account_name"] != acc["name"]
        if differs(have, want) or renamed:
            if not self.dry_run:
                if renamed:
                    # the account's document name carries its title; only this call renames it
                    name = self.erp.call("erpnext.accounts.doctype.account.account.update_account_number",
                                         name=name, account_name=acc["name"], account_number=number)
                if differs(have, want):
                    self.erp.update("Account", name, want)
            self.stats.count("Account", "updated")
        else:
            self.stats.count("Account", "unchanged")
        self.names[("Account", str(acc["id"]))] = name
        self._index["Account"][str(acc["id"])] = name

    # ---- items ----

    def import_items(self):
        units = {u["id"]: UOMS.get(u["name"], "Nos") for u in self.data["units"]}
        currency = {c["id"]: c["name"] for c in self.data["currencies"]}
        templates = {}
        for r in self.erp.list("Item Tax Template", [["company", "=", COMPANY]], ["name", "title"]):
            templates[r["title"].split(" (")[0]] = r["name"]
        for art in self.data["articles"]:
            code = art["intern_code"] or "bexio-{}".format(art["id"])
            want = {
                "item_code": code, "item_name": art["intern_name"] or code,
                "description": art["intern_description"] or art["intern_name"] or code,
                "item_group": "Services", "stock_uom": units.get(art["unit_id"], "Nos"),
                "is_stock_item": 0, "is_sales_item": 1, "is_purchase_item": 1,
            }
            vat = VAT_OF_TAX_ID.get(art.get("tax_income_id")) or VAT_OF_TAX_ID.get(art.get("tax_expense_id"))
            template = vat and templates.get("MWST {}% {}".format(*vat))
            if template:
                want["taxes"] = [{"item_tax_template": template}]
            item = self.upsert("Item", art["id"], want, code)
            if not item:
                continue
            for suffix, field, price_list in (("", "sale_price", "Standard Selling"), ("-buy", "purchase_price", "Standard Buying")):
                if art.get(field) in (None, ""):
                    continue
                self.upsert("Item Price", "{}{}".format(art["id"], suffix), {
                    "item_code": item, "price_list": price_list, "price_list_rate": float(art[field]),
                    "currency": currency.get(art["currency_id"], "CHF"),
                }, "-")

    # ---- parties, contacts, addresses ----

    def _classify(self):
        """For every bexio contact: is it a Customer, a Supplier, a Contact only."""
        contacts = {c["id"]: c for c in self.data["contacts"]}
        invoiced = {i["contact_id"] for i in self.data["invoices"]}
        vendors = {b["vendor"].strip().lower() for b in self.data["bills"] if b.get("vendor")}
        has_company = {r["contact_sub_id"] for r in self.data["contact_relations"] if r["contact_id"] in contacts}
        kinds = {}
        for c in self.data["contacts"]:
            billed = display_name(c).lower() in vendors or c["name_1"].strip().lower() in vendors
            if c["contact_type_id"] == 2 and c["id"] in has_company and c["id"] not in invoiced and not billed:
                kinds[c["id"]] = set()
                continue
            kinds[c["id"]] = {k for k, on in (("Customer", c["id"] in invoiced), ("Supplier", billed)) if on} or {"Customer"}
        return kinds

    def import_parties(self):
        groups = {str(g["id"]): g["name"] for g in self.data["contact_groups"]}
        countries = {c["id"]: c["iso_3166_alpha2"].lower() for c in self.data["countries"]}
        self.kinds = self._classify()
        for c in self.data["contacts"]:
            for kind in sorted(self.kinds[c["id"]]):
                prefix = kind.lower()
                title = self._party_name(kind, c)
                primary = (c["contact_group_ids"] or "").split(",")[0].strip()
                want = {
                    prefix + "_name": title,
                    prefix + "_type": "Company" if c["contact_type_id"] == 1 else "Individual",
                    "website": c["url"] or "",
                    prefix + "_details": "bexio Nr. {}".format(c["nr"]) + ("\n" + c["remarks"] if c["remarks"] else ""),
                }
                if primary in groups:
                    want[prefix + "_group"] = groups[primary]
                if kind == "Customer":
                    want["territory"] = "Switzerland" if countries.get(c["country_id"]) == "ch" else "Rest Of The World"
                self.upsert(kind, c["id"], want, title)
        for kind in ("Customer", "Supplier"):
            self._taken.pop(kind, None)

    def _party_name(self, kind, contact):
        """The display name; when another bexio contact already has it, the bexio number tells them apart."""
        base = display_name(contact)
        known = self.index(kind)
        if kind not in self._taken:
            self._taken[kind] = {name: bid for bid, name in known.items()}
        owner = self._taken[kind].get(base)
        if owner is not None and owner != str(contact["id"]):
            base = "{} ({})".format(base, contact["nr"])
        self._taken[kind][base] = str(contact["id"])
        return base

    def _links(self, contact_id):
        """The Customers and Suppliers a contact belongs to: its own, and its companies'."""
        owners = [contact_id] + [r["contact_id"] for r in self.data["contact_relations"] if r["contact_sub_id"] == contact_id]
        links = []
        for owner in owners:
            for kind in ("Customer", "Supplier"):
                name = self.names.get((kind, str(owner)))
                if name and kind in self.kinds.get(owner, ()):
                    links.append({"link_doctype": kind, "link_name": name})
        return links

    def import_contacts(self):
        salutations = {s["id"]: SALUTATIONS.get(s["name"]) for s in self.data["salutations"]}
        for c in self.data["contacts"]:
            person = c["contact_type_id"] == 2
            first, last = (c["name_2"], c["name_1"]) if person else (c["name_1"], c["name_2"] or "")
            want = {
                # the document name is first-last-<party name> in a 140 character column; the full name stays in company_name
                "first_name": (first or last)[:18], "last_name": (last if first else "")[:18],
                "company_name": "" if person else c["name_1"],
                "salutation": (salutations.get(c["salutation_id"]) or "") if person else "",
                "email_ids": [{"email_id": m, "is_primary": 1 if i == 0 else 0} for i, m in enumerate(_mails(c))],
                "phone_nos": _phones(c),
                "links": self._links(c["id"]),
            }
            self.upsert("Contact", c["id"], want, "-")

    def import_addresses(self):
        countries = {c["id"]: c["iso_3166_alpha2"].lower() for c in self.data["countries"]}
        for c in self.data["contacts"]:
            line1 = " ".join(filter(None, (c["street_name"], c["house_number"]))) or c["address"] or ""
            code = countries.get(c["country_id"])
            country = code and self.country(code)
            if not (line1 or c["city"]) or not country:
                self.stats.count("Address", "skipped")
                continue
            self.upsert("Address", c["id"], {
                "address_title": display_name(c), "address_type": "Office",
                "address_line1": line1 or c["city"], "address_line2": c["address_addition"] or "",
                "city": c["city"] or "", "pincode": c["postcode"] or "", "country": country,
                "links": self._links(c["id"]),
            }, "-")

    def country(self, code):
        if code not in self.country_name:
            rows = self.erp.list("Country", [["code", "=", code]])
            self.country_name[code] = rows[0]["name"] if rows else None
        return self.country_name[code]

    # ---- bank accounts ----

    def import_bank_accounts(self):
        gl = {a["id"]: a for a in self.data["accounts"]}
        for b in self.data["bank_accounts"]:
            account = gl.get(b["account_id"])
            gl_name = account and self.names.get(("Account", str(account["id"])))
            if not gl_name:
                self.stats.count("Bank Account", "failed")
                self.stats.problems.append("Bank Account {}: GL account missing".format(b["id"]))
                continue
            title = b["name"]
            bank = bank_name(b)
            if not self.erp.list("Bank", [["name", "=", bank]]) and not self.dry_run:
                self.stats.count("Bank Account", "failed")
                self.stats.problems.append("Bank Account {}: Bank record missing; run swiss-setup.sh banks (see README)".format(b["id"]))
                continue
            self.upsert("Bank Account", b["id"], {
                "account_name": title, "bank": bank, "account": gl_name, "is_company_account": 1,
                "company": COMPANY, "iban": b["iban_nr"] or "", "bank_account_no": b["bank_account_nr"] or "",
                "branch_code": b["bc_nr"] or "",
            }, "{} - {}".format(title, bank))

    def run(self):
        self.check_currencies()
        self.import_groups()
        self.import_accounts()
        self.import_items()
        self.import_parties()
        self.import_contacts()
        self.import_addresses()
        self.import_bank_accounts()
        return self.stats


def bank_name(bank_account):
    return bank_account["bank_name"] or bank_account["name"]


def display_name(contact):
    if contact["contact_type_id"] == 2:
        return " ".join(filter(None, (contact["name_2"], contact["name_1"]))).strip()
    return " ".join(filter(None, (contact["name_1"], contact["name_2"]))).strip()


def _is_phone(value):
    return bool(value) and all(ch.isdigit() or ch in " +()-./" for ch in value)


def _mails(c):
    """Mail fields, plus a mail address that was typed into a phone field (ERPNext refuses it there)."""
    found = [m for m in (c["mail"], c["mail_second"]) if m]
    for field in ("phone_fixed", "phone_fixed_second", "phone_mobile"):
        value = c.get(field)
        if value and "@" in value and value not in found:
            found.append(value)
    return found


def _phones(c):
    rows = []
    for field, flag in (("phone_fixed", "is_primary_phone"), ("phone_fixed_second", None), ("phone_mobile", "is_primary_mobile_no")):
        if _is_phone(c.get(field)):
            row = {"phone": c[field]}
            if flag:
                row[flag] = 1
            rows.append(row)
    return rows


def main(argv):
    parser = argparse.ArgumentParser(description="Import the exported bexio master data into ERPNext.")
    parser.add_argument("--export", default=None, help="export directory (default: the newest under <private>/bexio-export/)")
    parser.add_argument("--dry-run", action="store_true", help="read ERPNext, write nothing, print the totals")
    parser.add_argument("--print-banks", action="store_true", help="print the bank names to create with swiss-setup.sh banks, then exit")
    parser.add_argument("--token-file", default=TOKEN_FILE)
    args = parser.parse_args(argv)
    export_dir = args.export or newest_export()
    if args.print_banks:
        print("\n".join(sorted({bank_name(b) for b in load_export(export_dir)["bank_accounts"]})))
        return 0
    importer = Importer(Erp.from_file(args.token_file), load_export(export_dir), dry_run=args.dry_run)
    try:
        stats = importer.run()
    except ErpError as err:
        # a lookup that every record depends on failed (permission, field missing): stop, do not count it per record
        print("aborted: {}".format(err), file=sys.stderr)
        return 2
    print("import from {}".format(export_dir))
    print(stats.report(args.dry_run))
    for line in stats.problems:
        print("  problem: " + line, file=sys.stderr)
    return 1 if stats.failed() else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
