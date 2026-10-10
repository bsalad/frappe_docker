"""Wise sync: each balance as a Bank Account, its statements as Bank Transactions, through bank_feed.

Run by the hourly scheduler (sync_scheduled) and by the Test connection, Dry run and Sync now buttons of Wise Settings.
A dry run writes nothing: it reports what each account would get. The token is read with get_password; it is never
logged or returned to the page.
"""

import datetime

import frappe
from frappe import _

from bi_finance import bank_feed, wise_client

BANK = "WISE PAYMENTS LIMITED"

# an account opened for a new currency takes the first free number of this range, next to Wise Kontokorrent (1021)
ACCOUNT_NUMBERS = range(1022, 1029)

# statements are read from a week before the last sync, so a movement Wise posts late is not missed
OVERLAP = datetime.timedelta(days=7)


def _settings():
    settings = frappe.get_single("Wise Settings")
    token = settings.get_password("api_token", raise_exception=False)
    if not token or not settings.profile_id:
        frappe.throw(_("Enter the API token and the profile id in Wise Settings first."))
    return settings, token


@frappe.whitelist()
def test_connection():
    """The profiles the token can see, as a message. Reads only."""
    settings = frappe.get_single("Wise Settings")
    token = settings.get_password("api_token", raise_exception=False)
    if not token:
        frappe.throw(_("Enter the API token in Wise Settings first."))
    try:
        found = wise_client.profiles(token)
    except wise_client.WiseError as err:
        frappe.throw(str(err))
    lines = ["{} {}".format(p.get("type"), p.get("id")) for p in found]
    frappe.msgprint(_("Connected. Profiles: {0}").format(", ".join(lines) or _("none")))
    return lines


@frappe.whitelist()
def sync_now(dry_run=True):
    """Sync every balance now. dry_run, the default, reports what would be written; a live run writes it."""
    dry_run = frappe.parse_json(dry_run) if isinstance(dry_run, str) else bool(dry_run)
    report = run(dry_run=dry_run)
    frappe.msgprint(_("{0} account(s) {1}.").format(len(report), _("planned") if dry_run else _("synced")))
    return report


def sync_scheduled():
    """The hourly job: a live run, only when Wise Settings has sync on and a token and profile."""
    settings = frappe.get_single("Wise Settings")
    if not settings.sync_enabled:
        return
    run(dry_run=False)


def run(dry_run):
    """One pass over the profile's balances. Returns one report row per currency; nothing is written when dry_run."""
    settings, token = _settings()
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    start = _start(settings, now)
    report = []
    for balance in wise_client.balances(token, settings.profile_id):
        currency = balance["currency"]
        # the balance id is needed for the statement; the account name is planned before it exists on a dry run
        bank_account = _bank_account_name(currency)
        rows = []
        for window_start, window_end in wise_client.windows(start, now):
            body = wise_client.statement(token, settings.profile_id, balance["id"], currency, window_start, window_end)
            rows += wise_client.statement_rows(body, balance["id"], currency)
        create, summary = bank_feed.plan(bank_account, rows) if frappe.db.exists("Bank Account", bank_account) else _unseen(rows)
        if not dry_run:
            ensure_bank_account(currency)
            bank_feed.write(bank_account, create)
            frappe.db.commit()
        report.append(
            {
                "currency": currency,
                "bank_account": bank_account,
                "create": len(create),
                **summary,
                "wise_balance": _amount(balance),
                "erp_net": _erp_net(bank_account),
            }
        )
    if not dry_run:
        settings.last_sync = now
        settings.save(ignore_permissions=True)
        frappe.db.commit()
    return report


def ensure_bank_account(currency):
    """The Bank Account of a currency, with its GL account, made when missing. An existing one is left as it is."""
    name = _bank_account_name(currency)
    if frappe.db.exists("Bank Account", name):
        return name
    company = _company()
    chf = frappe.db.get_value("Bank Account", "Wise CHF - " + BANK, ["account", "company"], as_dict=True)
    parent = frappe.db.get_value("Account", chf["account"], "parent_account")
    account = frappe.get_doc(
        {
            "doctype": "Account",
            "account_number": _free_number(),
            "account_name": "Wise Kontokorrent" if currency == "CHF" else "Wise Kontokorrent " + currency,
            "parent_account": parent,
            "company": company,
            "account_currency": currency,
            "account_type": "Bank",
            "is_group": 0,
        }
    ).insert()
    frappe.get_doc(
        {
            "doctype": "Bank Account",
            "account_name": "Wise " + currency,
            "bank": BANK,
            "account": account.name,
            "company": company,
            "is_company_account": 1,
        }
    ).insert()
    return name


def _bank_account_name(currency):
    return "Wise {} - {}".format(currency, BANK)


def _company():
    return frappe.db.get_value("Bank Account", "Wise CHF - " + BANK, "company")


def _free_number():
    used = {str(n) for n in frappe.get_all("Account", filters={"company": _company()}, pluck="account_number") if n}
    for number in ACCOUNT_NUMBERS:
        if str(number) not in used:
            return str(number)
    frappe.throw(_("No free account number in 1022 to 1028 for a Wise balance."))


def _start(settings, now):
    if settings.last_sync:
        return settings.last_sync - OVERLAP
    return datetime.datetime.combine(settings.backfill_from, datetime.time.min)


def _amount(balance):
    return float((balance.get("amount") or {}).get("value") or 0)


def _erp_net(bank_account):
    """The sum of the account's submitted or draft Bank Transactions, deposits less withdrawals. Opening balances are not in it."""
    if not frappe.db.exists("Bank Account", bank_account):
        return 0.0
    net = frappe.db.sql(
        "select coalesce(sum(deposit - withdrawal), 0) from `tabBank Transaction` where bank_account = %s and docstatus < 2",
        bank_account,
    )[0][0]
    return float(net)


def _unseen(rows):
    """The plan for an account that does not exist yet: every row is new, none is imported."""
    return rows, {"rows": len(rows), "already_fed": 0, "already_imported": 0, "create": len(rows)}
