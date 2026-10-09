"""Offline tests for import_master.py against an in-memory ERPNext. Invented data only, no network.

Run with: python3 -m unittest discover -s finance/bexio -p 'test_*.py'
"""

import copy
import os
import tempfile
import unittest

import import_master as im


class FakeErp:
    """Just enough of ERPNext: documents per doctype, filters by equality or 'is set', rename."""

    def __init__(self, existing=None):
        self.docs = {}
        self.writes = 0
        self.counter = 0
        for doctype, docs in (existing or {}).items():
            for doc in docs:
                self.docs.setdefault(doctype, {})[doc["name"]] = copy.deepcopy(doc)

    def _name(self, doctype, doc):
        if doctype == "Account":
            return "{} - {} - bic".format(doc["account_number"], doc["account_name"])
        for field in () if doctype == "Item Price" else ("customer_name", "supplier_name", "item_code", "customer_group_name",
                      "supplier_group_name", "bank_name", "designation_name"):
            if field in doc:
                return doc[field]
        self.counter += 1
        return "{}-{}".format(doctype, self.counter)

    def list(self, doctype, filters=None, fields=("name",)):
        rows = []
        for doc in self.docs.get(doctype, {}).values():
            if all(self._match(doc, f) for f in filters or []):
                rows.append({k: doc.get(k) for k in fields})
        return rows

    @staticmethod
    def _match(doc, flt):
        field, op, value = flt
        if op == "is":
            return bool(doc.get(field))
        if op == "in":
            return doc.get(field) in value
        return doc.get(field) == value

    def get(self, doctype, name):
        return copy.deepcopy(self.docs[doctype][name])

    def insert(self, doctype, doc):
        self.writes += 1
        doc = {k: v for k, v in copy.deepcopy(doc).items() if k != "doctype"}
        doc["name"] = self._name(doctype, doc)
        if doc["name"] in self.docs.get(doctype, {}):
            raise im.ErpError(409, "POST " + doctype)
        self.docs.setdefault(doctype, {})[doc["name"]] = doc
        return copy.deepcopy(doc)

    def update(self, doctype, name, values):
        self.writes += 1
        self.docs[doctype][name].update(copy.deepcopy(values))
        return copy.deepcopy(self.docs[doctype][name])

    def call(self, method, name, account_name, account_number):
        self.writes += 1
        doc = self.docs["Account"].pop(name)
        doc.update(account_name=account_name, account_number=account_number)
        doc["name"] = "{} - {} - bic".format(account_number, account_name)
        self.docs["Account"][doc["name"]] = doc
        return doc["name"]


def acct(number, name, root, group=0, parent="", **extra):
    return dict({"name": "{} - {} - bic".format(number, name), "account_number": number, "account_name": name,
                 "root_type": root, "is_group": group, "parent_account": parent,
                 "report_type": "Balance Sheet" if root in ("Asset", "Liability", "Equity") else "Profit and Loss",
                 "disabled": 0, "account_currency": "CHF", "bexio_id": None, "company": im.COMPANY}, **extra)


def chart():
    """A small invented KMU chart: roots 1, 3, 6, 7, 8 (8 typed Income as in the template) and a few groups."""
    return {"Account": [
        acct("1", "Aktiven", "Asset", 1),
        acct("10", "Umlauf", "Asset", 1, "1 - Aktiven - bic"),
        acct("1020", "UBS", "Asset", 0, "10 - Umlauf - bic", account_currency="EUR"),
        acct("3", "Ertrag", "Income", 1),
        acct("34", "Dienstleistungen", "Income", 1, "3 - Ertrag - bic"),
        acct("6", "Betriebsaufwand", "Expense", 1),
        acct("65", "Verwaltung", "Expense", 1, "6 - Betriebsaufwand - bic"),
        acct("6500", "Büromaterial", "Expense", 0, "65 - Verwaltung - bic"),
        acct("6950", "Finanzertrag alt", "Expense", 0, "65 - Verwaltung - bic"),
        acct("7", "Nebenerfolg", "Income", 1),
        acct("8", "Ausserordentlich", "Income", 1),
        acct("8000", "Betriebsfremd", "Income", 0, "8 - Ausserordentlich - bic"),
    ]}


def export_data():
    return {
        "currencies": [{"id": 1, "name": "CHF"}, {"id": 2, "name": "EUR"}],
        "contact_groups": [{"id": 1, "name": "Kunden"}, {"id": 2, "name": "Lieferanten"}],
        "accounts": [
            {"id": 11, "account_no": "1020", "name": "Testbank", "account_type": 3, "is_active": True},
            {"id": 12, "account_no": "1021", "name": "Zweitbank", "account_type": 3, "is_active": True},
            {"id": 13, "account_no": "3400", "name": "Beratung", "account_type": 1, "is_active": True},
            {"id": 14, "account_no": "6500", "name": "Büro neu", "account_type": 2, "is_active": False},
            {"id": 15, "account_no": "6950", "name": "Zinsertrag", "account_type": 1, "is_active": True},
            {"id": 16, "account_no": "8000", "name": "Fremder Aufwand", "account_type": 2, "is_active": True},
            {"id": 17, "account_no": "8600", "name": "Einmaliger Aufwand", "account_type": 2, "is_active": True},
            {"id": 18, "account_no": "6999", "name": "Kursgewinne", "account_type": 1, "is_active": True},
        ],
        "units": [{"id": 1, "name": "Stk"}, {"id": 3, "name": "Tag"}],
        "salutations": [{"id": 1, "name": "Herr"}, {"id": 2, "name": "Frau"}],
        "countries": [{"id": 1, "iso_3166_alpha2": "CH"}, {"id": 2, "iso_3166_alpha2": "DE"}],
        "articles": [
            {"id": 1, "intern_code": "BER", "intern_name": "Beratung", "intern_description": "pro Tag", "unit_id": 3,
             "currency_id": 1, "sale_price": "1000.00", "purchase_price": None, "tax_income_id": 28, "tax_expense_id": 38},
            {"id": 2, "intern_code": "LIZ", "intern_name": "Lizenz", "intern_description": None, "unit_id": 1,
             "currency_id": 2, "sale_price": "10.50", "purchase_price": "4.00", "tax_income_id": 99, "tax_expense_id": None},
        ],
        "contacts": [
            contact(1, 1, "Muster AG", None, nr="1", groups="1,2"),
            contact(2, 1, "Lieferant GmbH", "Zürich", nr="2", groups="2"),
            contact(3, 1, "Neutral AG", None, nr="3", groups=None, country=2),
            contact(4, 2, "Meier", "Anna", nr="4", groups=None, salutation=2),
            contact(5, 2, "Einzel", "Hans", nr="5", groups=None),
            contact(6, 1, "Doppel AG", None, nr="6", groups=None, street="", city="", country=None),
        ],
        "contact_relations": [{"id": 1, "contact_id": 1, "contact_sub_id": 4, "description": "Einkauf"}],
        "invoices": [{"id": 1, "contact_id": 1}],
        "bills": [{"id": "u1", "vendor": "lieferant gmbh zürich"}, {"id": "u2", "vendor": "Unbekannt GmbH"}],
        "bank_accounts": [{"id": 1, "name": "Hauptkonto", "bank_name": "Testbank AG", "account_id": 11,
                           "iban_nr": "CH00 0000 0000 0000 0000 0", "bank_account_nr": "1-2", "bc_nr": "123"}],
    }


def contact(cid, ctype, name1, name2, nr, groups, salutation=None, country=1, street="Teststrasse", city="Testort"):
    return {"id": cid, "contact_type_id": ctype, "name_1": name1, "name_2": name2, "nr": nr,
            "contact_group_ids": groups, "salutation_id": salutation, "country_id": country,
            "mail": "k{}@example.invalid".format(cid), "mail_second": None, "phone_fixed": "000", "phone_fixed_second": None,
            "phone_mobile": None, "url": "", "remarks": None, "street_name": street, "house_number": "1",
            "address": "", "address_addition": None, "city": city, "postcode": "0000"}


def erp_with_chart():
    erp = FakeErp(chart())
    erp.docs["Currency"] = {"CHF": {"name": "CHF", "enabled": 1}, "EUR": {"name": "EUR", "enabled": 1}}
    erp.docs["Bank"] = {"Testbank AG": {"name": "Testbank AG"}}
    erp.docs["Country"] = {"Switzerland": {"name": "Switzerland", "code": "ch"}, "Germany": {"name": "Germany", "code": "de"}}
    erp.docs["Item Tax Template"] = {"MWST 8.1% Normal (ab 2024) - bic": {
        "name": "MWST 8.1% Normal (ab 2024) - bic", "title": "MWST 8.1% Normal (ab 2024)", "company": im.COMPANY}}
    return erp


def run(erp, dry_run=False, data=None):
    importer = im.Importer(erp, data or export_data(), dry_run=dry_run)
    importer.run()
    return importer


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.erp = erp_with_chart()
        self.importer = run(self.erp)
        self.accounts = self.erp.docs["Account"]

    def by_number(self, number):
        return next(a for a in self.accounts.values() if a["account_number"] == number)

    def test_existing_account_is_adopted_by_number_and_renamed(self):
        a = self.by_number("6500")
        self.assertEqual((a["account_name"], a["bexio_id"], a["disabled"]), ("Büro neu", "14", 1))
        self.assertEqual(a["name"], "6500 - Büro neu - bic")

    def test_account_currency_follows_bexio_for_1020(self):
        self.assertEqual(self.by_number("1020")["account_currency"], "CHF")

    def test_new_account_goes_under_the_nearest_group(self):
        self.assertEqual(self.by_number("3400")["parent_account"], "34 - Dienstleistungen - bic")
        self.assertEqual(self.by_number("1021")["parent_account"], "10 - Umlauf - bic")
        self.assertEqual(self.by_number("1021")["account_type"], "Bank")

    def test_root_type_follows_bexio_and_moves_the_account(self):
        moved = self.by_number("6950")
        self.assertEqual(moved["root_type"], "Income")
        self.assertEqual(moved["parent_account"], "7E - Übriger Ertrag (bexio) - bic")
        expense = self.by_number("8000")
        self.assertEqual((expense["root_type"], expense["report_type"]), ("Expense", "Profit and Loss"))
        self.assertEqual(expense["parent_account"], "6E - Übriger Aufwand (bexio) - bic")

    def test_new_account_of_the_wrong_root_goes_to_the_fallback_group(self):
        self.assertEqual(self.by_number("8600")["parent_account"], "6E - Übriger Aufwand (bexio) - bic")
        self.assertEqual(self.by_number("6999")["parent_account"], "7E - Übriger Ertrag (bexio) - bic")

    def test_the_fallback_group_is_made_once(self):
        groups = [a for a in self.accounts.values() if a["account_number"] in ("6E", "7E")]
        self.assertEqual(len(groups), 2)

    def test_every_bexio_account_has_its_id(self):
        self.assertEqual(sorted(a["bexio_id"] for a in self.accounts.values() if a.get("bexio_id")),
                         sorted(str(a["id"]) for a in export_data()["accounts"]))


class MasterDataTest(unittest.TestCase):
    def setUp(self):
        self.erp = erp_with_chart()
        self.importer = run(self.erp)
        self.docs = self.erp.docs

    def find(self, doctype, bexio_id):
        return next(d for d in self.docs[doctype].values() if d.get("bexio_id") == str(bexio_id))

    def test_groups_are_made_for_customers_and_suppliers(self):
        self.assertEqual(len(self.docs["Customer Group"]), 2)
        self.assertEqual(len(self.docs["Supplier Group"]), 2)
        self.assertEqual(self.find("Customer Group", 1)["parent_customer_group"], "All Customer Groups")

    def test_company_with_invoice_is_customer_with_primary_group(self):
        c = self.find("Customer", 1)
        self.assertEqual((c["customer_name"], c["customer_group"], c["customer_type"]), ("Muster AG", "Kunden", "Company"))
        self.assertEqual(c["territory"], "Switzerland")

    def test_company_with_bills_is_supplier_not_customer(self):
        s = self.find("Supplier", 2)
        self.assertEqual((s["supplier_name"], s["supplier_group"]), ("Lieferant GmbH Zürich", "Lieferanten"))
        self.assertFalse([c for c in self.docs["Customer"].values() if c["bexio_id"] == "2"])

    def test_a_bill_names_its_supplier_by_contact_id_when_the_vendor_name_is_shorter(self):
        data = export_data()
        data["contacts"].append(contact(9, 1, "Steueramt Testort", None, nr="009", groups=None))
        data["bills"].append({"id": "u3", "vendor": "Steueramt", "supplier_id": 9})
        erp = erp_with_chart()
        run(erp, data=data)
        self.assertEqual([s["supplier_name"] for s in erp.docs["Supplier"].values() if s["bexio_id"] == "9"],
                         ["Steueramt Testort"])
        self.assertFalse([c for c in erp.docs.get("Customer", {}).values() if c["bexio_id"] == "9"])

    def test_contact_without_documents_becomes_a_customer(self):
        n = self.find("Customer", 3)
        self.assertEqual(n["territory"], "Rest Of The World")
        self.assertNotIn("customer_group", n)

    def test_person_with_a_company_is_only_a_contact_linked_to_it(self):
        self.assertFalse([c for c in self.docs["Customer"].values() if c["bexio_id"] == "4"])
        contact = self.find("Contact", 4)
        self.assertEqual((contact["first_name"], contact["last_name"], contact["salutation"]), ("Anna", "Meier", "Ms"))
        self.assertEqual(contact["links"], [{"link_doctype": "Customer", "link_name": "Muster AG"}])

    def test_person_without_a_company_is_an_individual_customer_and_a_contact(self):
        self.assertEqual(self.find("Customer", 5)["customer_type"], "Individual")
        self.assertEqual(self.find("Contact", 5)["links"], [{"link_doctype": "Customer", "link_name": "Hans Einzel"}])

    def test_all_contacts_are_imported_as_contacts(self):
        self.assertEqual(len(self.docs["Contact"]), 6)

    def test_address_is_linked_to_the_party_and_skipped_without_data(self):
        a = self.find("Address", 1)
        self.assertEqual((a["address_line1"], a["city"], a["country"]), ("Teststrasse 1", "Testort", "Switzerland"))
        self.assertEqual(a["links"], [{"link_doctype": "Customer", "link_name": "Muster AG"}])
        self.assertEqual(len(self.docs["Address"]), 5)
        self.assertEqual(self.importer.stats.rows["Address"]["skipped"], 1)

    def test_items_prices_and_tax_template(self):
        item = self.find("Item", 1)
        self.assertEqual((item["item_code"], item["stock_uom"], item["is_stock_item"], item["item_group"]), ("BER", "Day", 0, "Services"))
        self.assertEqual(item["taxes"], [{"item_tax_template": "MWST 8.1% Normal (ab 2024) - bic"}])
        self.assertNotIn("taxes", self.find("Item", 2))
        prices = {p["bexio_id"]: p for p in self.docs["Item Price"].values()}
        self.assertEqual(sorted(prices), ["1", "2", "2-buy"])
        self.assertEqual((prices["1"]["price_list_rate"], prices["1"]["price_list"]), (1000.0, "Standard Selling"))
        self.assertEqual((prices["2"]["currency"], prices["2-buy"]["price_list"]), ("EUR", "Standard Buying"))

    def test_bank_account_links_bank_and_gl_account(self):
        b = self.find("Bank Account", 1)
        self.assertEqual((b["bank"], b["account"], b["is_company_account"]), ("Testbank AG", "1020 - Testbank - bic", 1))

    def test_same_name_gets_the_bexio_number(self):
        data = export_data()
        data["contacts"].append(contact(7, 1, "Muster AG", None, nr="77", groups=None))
        erp = erp_with_chart()
        run(erp, data=data)
        names = sorted(c["customer_name"] for c in erp.docs["Customer"].values() if c["bexio_id"] in ("1", "7"))
        self.assertEqual(names, ["Muster AG", "Muster AG (77)"])


class RepeatTest(unittest.TestCase):
    def test_second_run_creates_and_changes_nothing(self):
        erp = erp_with_chart()
        first = run(erp).stats
        self.assertGreater(first.changed(), 0)
        snapshot = copy.deepcopy(erp.docs)
        writes = erp.writes
        second = run(erp).stats
        self.assertEqual(second.changed(), 0)
        self.assertEqual(second.failed(), 0)
        self.assertEqual(erp.writes, writes)
        self.assertEqual(erp.docs, snapshot)

    def test_a_changed_field_in_bexio_updates_by_id_even_when_the_name_changed(self):
        erp = erp_with_chart()
        run(erp)
        data = export_data()
        data["contacts"][0]["name_1"] = "Muster Holding AG"
        data["articles"][0]["sale_price"] = "1200.00"
        stats = run(erp, data=data).stats
        self.assertEqual(stats.rows["Customer"]["updated"], 1)
        self.assertEqual(stats.rows["Customer"]["created"], 0)
        self.assertEqual(stats.rows["Item Price"]["updated"], 1)
        self.assertEqual(len([c for c in erp.docs["Customer"].values() if c["bexio_id"] == "1"]), 1)

    def test_an_account_renamed_in_bexio_is_renamed_in_erpnext(self):
        erp = erp_with_chart()
        run(erp)
        data = export_data()
        data["accounts"][2]["name"] = "Beratung und Training"
        stats = run(erp, data=data).stats
        self.assertEqual(stats.rows["Account"]["updated"], 1)
        self.assertIn("3400 - Beratung und Training - bic", erp.docs["Account"])
        self.assertNotIn("3400 - Beratung - bic", erp.docs["Account"])


class DryRunTest(unittest.TestCase):
    def test_dry_run_writes_nothing_and_reports_what_a_real_run_does(self):
        dry_erp, real_erp = erp_with_chart(), erp_with_chart()
        before = copy.deepcopy(dry_erp.docs)
        dry = run(dry_erp, dry_run=True).stats
        real = run(real_erp).stats
        self.assertEqual(dry_erp.writes, 0)
        self.assertEqual(dry_erp.docs, before)
        self.assertEqual(dry.rows, real.rows)

    def test_report_has_totals_only(self):
        report = run(erp_with_chart(), dry_run=True).stats.report(True)
        self.assertIn("Customer", report)
        self.assertIn("dry run", report)
        for secret in ("Muster", "Lieferant", "Testbank", "k1@example"):
            self.assertNotIn(secret, report)


class ProblemsTest(unittest.TestCase):
    def test_a_refused_record_is_counted_and_the_rest_goes_on(self):
        erp = erp_with_chart()
        original = erp.insert

        def refuse_items(doctype, doc):
            if doctype == "Item" and doc["item_code"] == "LIZ":
                raise im.ErpError(403, "POST Item")
            return original(doctype, doc)

        erp.insert = refuse_items
        stats = run(erp).stats
        self.assertEqual(stats.rows["Item"]["failed"], 1)
        # the article that was not refused, and the two generic items (bexio Position, bexio Aufwand)
        self.assertEqual(stats.rows["Item"]["created"], 3)
        self.assertEqual(stats.rows["Customer"]["created"], 4)
        self.assertEqual(stats.failed(), 1)
        self.assertTrue(any("Item 2" in p for p in stats.problems))

    def test_currency_not_enabled_is_reported(self):
        erp = erp_with_chart()
        erp.docs["Currency"]["EUR"]["enabled"] = 0
        stats = run(erp).stats
        self.assertEqual(stats.rows["Currency"]["failed"], 1)
        self.assertTrue(any("swiss-setup.sh currencies" in p for p in stats.problems))


class GenericItemsTest(unittest.TestCase):
    def test_the_free_text_items_are_created_once_and_found_by_code(self):
        erp = erp_with_chart()
        first = run(erp).stats
        self.assertEqual(erp.docs["Item"]["bexio Position"]["is_sales_item"], 1)
        self.assertEqual(erp.docs["Item"]["bexio Aufwand"]["is_purchase_item"], 1)
        self.assertEqual(first.rows["Item"]["created"], len(erp.docs["Item"]))
        second = run(erp).stats
        self.assertEqual(second.rows["Item"]["created"], 0)
        self.assertEqual(second.rows["Item"]["failed"], 0)

    def test_an_existing_generic_item_is_left_alone(self):
        erp = erp_with_chart()
        erp.docs.setdefault("Item", {})["bexio Position"] = {"name": "bexio Position", "item_code": "bexio Position"}
        writes = erp.writes
        stats = run(erp).stats
        self.assertEqual(erp.docs["Item"]["bexio Position"], {"name": "bexio Position", "item_code": "bexio Position"})
        self.assertEqual(stats.rows["Item"]["created"], len(erp.docs["Item"]) - 1)
        self.assertEqual(stats.rows["Item"]["unchanged"], 1)
        self.assertGreater(erp.writes, writes)


class UpsertTest(unittest.TestCase):
    """The keyed upsert the sales and purchase imports share: drafts are updated, submitted documents are not."""

    def test_a_submitted_document_is_skipped_and_counted_not_written(self):
        erp = FakeErp({"Sales Invoice": [{"name": "SINV-1", "bexio_id": "5", "docstatus": 1, "customer": "A"}]})
        importer = im.Importer(erp, {})
        self.assertEqual(importer.upsert("Sales Invoice", 5, {"customer": "B"}, "-"), "SINV-1")
        self.assertEqual(erp.docs["Sales Invoice"]["SINV-1"]["customer"], "A")
        self.assertEqual(erp.writes, 0)
        self.assertEqual(importer.stats.rows["Sales Invoice"]["skipped"], 1)

    def test_a_draft_is_updated_in_place_and_a_rerun_changes_nothing(self):
        erp = FakeErp({"Sales Invoice": [{"name": "SINV-1", "bexio_id": "5", "docstatus": 0, "customer": "A"}]})
        importer = im.Importer(erp, {})
        importer.upsert("Sales Invoice", 5, {"customer": "B"}, "-")
        self.assertEqual(erp.docs["Sales Invoice"]["SINV-1"]["customer"], "B")
        again = im.Importer(erp, {})
        again.upsert("Sales Invoice", 5, {"customer": "B"}, "-")
        self.assertEqual(again.stats.rows["Sales Invoice"]["unchanged"], 1)
        self.assertEqual(len(erp.docs["Sales Invoice"]), 1)


class ContactDataTest(unittest.TestCase):
    def test_long_company_name_fits_the_contact_name_and_a_mail_in_a_phone_field_moves(self):
        data = export_data()
        data["contacts"][2]["name_1"] = "Sehr " * 40 + "lang AG"
        data["contacts"][2]["phone_fixed"] = "kontakt@example.invalid"
        data["contacts"][2]["phone_mobile"] = "+41 79 000 00 00"
        erp = erp_with_chart()
        stats = run(erp, data=data).stats
        self.assertEqual(stats.failed(), 0)
        c = next(d for d in erp.docs["Contact"].values() if d["bexio_id"] == "3")
        self.assertLessEqual(len(c["first_name"]), 18)
        self.assertIn("kontakt@example.invalid", [m["email_id"] for m in c["email_ids"]])
        self.assertEqual([p["phone"] for p in c["phone_nos"]], ["+41 79 000 00 00"])


class BankTest(unittest.TestCase):
    def test_missing_bank_record_is_a_clear_problem_not_a_crash(self):
        erp = erp_with_chart()
        erp.docs["Bank"] = {}
        stats = run(erp).stats
        self.assertEqual(stats.rows["Bank Account"]["failed"], 1)
        self.assertTrue(any("swiss-setup.sh banks" in p for p in stats.problems))

    def test_print_banks_lists_the_names_to_create(self):
        self.assertEqual(sorted({im.bank_name(b) for b in export_data()["bank_accounts"]}), ["Testbank AG"])


class HelpersTest(unittest.TestCase):
    def test_differs_ignores_empty_and_number_forms(self):
        self.assertFalse(im.differs({"a": None, "b": 1000.0, "c": 0}, {"a": "", "b": "1000.0", "c": False}))
        self.assertTrue(im.differs({"a": "x"}, {"a": "y"}))

    def test_differs_ignores_windows_line_breaks(self):
        self.assertFalse(im.differs({"d": "<li>a</li>\n<li>b</li>"}, {"d": "<li>a</li>\r\n<li>b</li>"}))

    def test_differs_compares_child_rows_on_the_keys_sent(self):
        have = {"links": [{"link_doctype": "Customer", "link_name": "A", "idx": 1}]}
        self.assertFalse(im.differs(have, {"links": [{"link_doctype": "Customer", "link_name": "A"}]}))
        self.assertTrue(im.differs(have, {"links": [{"link_doctype": "Customer", "link_name": "B"}]}))
        self.assertTrue(im.differs(have, {"links": []}))

    def test_token_file_is_parsed_and_the_secret_is_not_in_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "api")
            with open(path, "w") as f:
                f.write("# header\nurl=http://127.0.0.1:1/api/\napi_key=KEYKEY\napi_secret=SECRETSECRET\n")
            erp = im.Erp.from_file(path)
            with self.assertRaises(im.ErpError) as ctx:
                erp.get("Item", "x")
            self.assertNotIn("SECRETSECRET", str(ctx.exception))
            self.assertNotIn("KEYKEY", str(ctx.exception))

    def test_newest_export_takes_the_latest_directory_with_a_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            for day, manifest in (("2026-01-01", True), ("2026-02-01", True), ("2026-03-01", False)):
                os.makedirs(os.path.join(tmp, day))
                if manifest:
                    open(os.path.join(tmp, day, "manifest.json"), "w").close()
            self.assertEqual(os.path.basename(im.newest_export(tmp)), "2026-02-01")

    def test_meta_reads_getdoctype_not_the_doctype_resource(self):
        erp = im.Erp("http://127.0.0.1:1/api", "k", "s")
        seen = []

        def request(method, path, body=None):
            seen.append((method, path))
            return {"docs": [{"name": "Sales Order Item", "fields": []}, {"name": "Sales Order", "fields": [{"fieldname": "bexio_id"}]}]}

        erp._request = request
        meta = erp.meta("Sales Order")
        self.assertEqual(meta["name"], "Sales Order")
        self.assertEqual(seen, [("GET", "/method/frappe.desk.form.load.getdoctype?doctype=Sales+Order")])


if __name__ == "__main__":
    unittest.main()
