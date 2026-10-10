"""PayPal sync: the business account's transactions as Bank Transactions, one Bank Account per currency, through bank_feed.

Run by the hourly scheduler (sync_scheduled) and by the Test connection, Dry run and Sync now buttons of PayPal Settings.
A dry run writes nothing: it reports what each account would get. The client secret is read with get_password; it is
never logged or returned to the page.
"""

import collections
import datetime

import frappe
from frappe import _

from bi_finance import bank_feed, paypal_client

BANK = "PAYPAL"

PREFIX = "PayPal"

# an account opened for a new currency takes the first free number of this range, below the Wise balances' 1022-1028
ACCOUNT_NUMBERS = range(1030, 1040)

# the bank group of the accounts: the parent of the UBS account, next to the other bank balances
BANK_GROUP_ACCOUNT = "UBS Kontokorrent - UBS Switzerland AG"

# statements are read from a week before the last sync, so a movement PayPal posts late is not missed
OVERLAP = datetime.timedelta(days=7)


def _settings():
    settings = frappe.get_single("PayPal Settings")
    secret = settings.get_password("client_secret", raise_exception=False)
    if not secret or not settings.client_id:
        frappe.throw(_("Enter the client id and the secret in PayPal Settings first."))
    return settings, secret


@frappe.whitelist()
def test_connection():
    """The token and the currencies the account holds, as a message. Reads only."""
    settings = frappe.get_single("PayPal Settings")
    secret = settings.get_password("client_secret", raise_exception=False)
    if not secret or not settings.client_id:
        frappe.throw(_("Enter the client id and the secret in PayPal Settings first."))
    try:
        token = paypal_client.access_token(settings.client_id, secret)
        found = paypal_client.balances(token)
    except paypal_client.PayPalError as err:
        frappe.throw(str(err))
    frappe.msgprint(_("Connected. Currencies: {0}").format(", ".join(sorted(found)) or _("none")))
    return sorted(found)


@frappe.whitelist()
def sync_now(dry_run=True):
    """Sync every currency now. dry_run, the default, reports what would be written; a live run writes it."""
    dry_run = frappe.parse_json(dry_run) if isinstance(dry_run, str) else bool(dry_run)
    report = run(dry_run=dry_run)
    frappe.msgprint(_("{0} account(s) {1}.").format(len(report["accounts"]), _("planned") if dry_run else _("synced")))
    frappe.msgprint("<br>".join(_summary_lines(report)))
    return report


def sync_scheduled():
    """The hourly job: a live run, only when PayPal Settings has sync on and a client id and secret."""
    settings = frappe.get_single("PayPal Settings")
    if not settings.sync_enabled:
        return
    run(dry_run=False)


def run(dry_run):
    """One pass over the account's transactions and balances. Returns {"accounts": one row per currency}.

    Nothing is written when dry_run. A currency the account holds or moved is an account; the balance PayPal reports
    is set against the account's net in ERPNext after the run.
    """
    settings, secret = _settings()
    token = paypal_client.access_token(settings.client_id, secret)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    start = _start(settings, now)
    rows_by_currency = collections.defaultdict(list)
    for window_start, window_end in paypal_client.windows(start, now):
        details = paypal_client.transactions(token, window_start, window_end)
        for row in paypal_client.feed_rows(details):
            rows_by_currency[row["currency"]].append(row)
    balances = paypal_client.balances(token)
    accounts = []
    for currency in sorted(set(rows_by_currency) | set(balances)):
        bank_account = bank_feed.bank_account_name(PREFIX, currency, BANK)
        create, summary = bank_feed.plan(bank_account, rows_by_currency.get(currency, []))
        if not dry_run:
            ensure_bank_account(currency)
            bank_feed.write(bank_account, create)
            frappe.db.commit()
        accounts.append(_account_report(currency, bank_account, create, summary, balances.get(currency, 0.0)))
    if not dry_run:
        # one field, not settings.save(): a save would write back the secret and switches as read at the start of the run
        frappe.db.set_single_value("PayPal Settings", "last_sync", now)
        frappe.db.commit()
    return {"accounts": accounts}


def ensure_bank_account(currency):
    """The Bank Account of a currency, with its GL account, made when missing. An existing one is left as it is."""
    group = frappe.db.get_value("Bank Account", BANK_GROUP_ACCOUNT, ["account", "company"], as_dict=True)
    parent = frappe.db.get_value("Account", group["account"], "parent_account")
    return bank_feed.open_bank_account(PREFIX, BANK, currency, parent, group["company"], ACCOUNT_NUMBERS)


def _account_report(currency, bank_account, create, summary, balance):
    """One currency's row: the counts, and PayPal's balance against ERPNext's net once the run's rows are in."""
    erp_net = _erp_net(bank_account) + sum(row["deposit"] - row["withdrawal"] for row in create)
    return {
        "currency": currency,
        "bank_account": bank_account,
        "create": len(create),
        "create_by_month": _by_month(create),
        **summary,
        "paypal_balance": balance,
        "erp_net": round(erp_net, 2),
        # the gap is the history before PayPal's three years, which no run can read: zero when the feed holds it all
        "gap": round(balance - erp_net, 2),
    }


def _summary_lines(report):
    """One line per currency for the message: the counts, then PayPal's balance and ERPNext's net."""
    lines = []
    for a in report["accounts"]:
        months = ", ".join("{} {}".format(month, n) for month, n in sorted(a["create_by_month"].items()))
        lines.append(
            "{}: {} to create [{}]; PayPal {}; ERPNext {}; gap {}; {} already fed, {} already imported".format(
                a["currency"],
                a["create"],
                months or "none",
                a["paypal_balance"],
                a["erp_net"],
                a["gap"],
                a["already_fed"],
                a["already_imported"],
            )
        )
    return lines


def _by_month(rows):
    """The count of feed rows per month (YYYY-MM)."""
    return dict(collections.Counter(row["date"][:7] for row in rows))


def _erp_net(bank_account):
    """The account's submitted or draft Bank Transactions, deposits less withdrawals. Opening balances are not in it."""
    if not frappe.db.exists("Bank Account", bank_account):
        return 0.0
    net = frappe.db.sql(
        "select coalesce(sum(deposit - withdrawal), 0) from `tabBank Transaction` where bank_account = %s and docstatus < 2",
        bank_account,
    )[0][0]
    return float(net)


def _start(settings, now):
    """The first moment to read: the last sync less the overlap, else the backfill date, never before PayPal's history."""
    earliest = now - datetime.timedelta(days=paypal_client.HISTORY_DAYS)
    if settings.last_sync:
        return max(settings.last_sync - OVERLAP, earliest)
    return max(datetime.datetime.combine(settings.backfill_from, datetime.time.min), earliest)
